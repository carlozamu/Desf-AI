from __future__ import annotations

import os
from collections import defaultdict
import re
from typing import Any, Awaitable, Callable

from game_client.client_agent import pick_dish_with_ai
from game_client.config import ClientConfig
from game_client.logging_utils import log, set_log_context
from game_client.state import GameState
from game_client.strategy import (
    get_client_id_from_event,
    pick_dish_for_order,
    resolve_client_id_from_meals,
)

_AUCTION_PURCHASE_LINE_RE = re.compile(
    r"^Restaurant\s+(?P<restaurant_id>\d+)\s+"
    r"try to buy:(?P<requested_qty>\d+)\s+(?P<ingredient>.+?)\s+"
    r"at single price of:\s*(?P<unit_price>-?\d+)\s+"
    r"result:Bought\s+(?P<bought_qty>\d+)\s+.+?\s+for\s+(?P<total_price>-?\d+)\s*$",
    re.IGNORECASE,
)


class EventRouter:
    def __init__(
        self,
        *,
        config: ClientConfig,
        state: GameState,
        api_client: Any,
        mcp_gateway: Any,
        phase_runner: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.api_client = api_client
        self.mcp_gateway = mcp_gateway
        self.phase_runner = phase_runner
        self.pending_orders: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.turn_purchases: list[dict[str, Any]] = []
        self.turn_orders_log: list[dict[str, Any]] = []
        self._seen_purchase_lines: set[str] = set()
        self._closed_for_client_overload: bool = False

        self.event_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "game_started": self.game_started,
            "game_phase_changed": self.game_phase_changed,
            "game_reset": self.game_reset,
            "client_spawned": self.client_spawned,
            "preparation_complete": self.preparation_complete,
            "message": self.message,
            "new_message": self.new_message,
            "heartbeat": self.heartbeat,
        }

    def _update_serving_log(self) -> None:
        turn_id = self.state.current_turn_id
        if not turn_id:
            return
            
        log_filename = f"serving_log_turn_{turn_id}.txt"
        log_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", log_filename)
        
        try:
            with open(log_filepath, "w", encoding="utf-8") as f:
                f.write(f"--- SERVING LOG (TURN {turn_id}) ---\n\n")
                if not self.turn_orders_log:
                    f.write("Nessun ordine ricevuto finora.\n")
                else:
                    total_revenue = 0
                    delivered_orders = []
                    
                    for info in self.turn_orders_log:
                        client_name = info.get("client_name", "Sconosciuto")
                        dish_name = info.get("dish_name", "Nessuno")
                        status = info.get("status", "PENDING")
                        reason = info.get("reason", "")
                        price = info.get("price", 0)
                        
                        line = f"Cliente: {client_name:<20} | Piatto: {dish_name:<40} | Stato: {status:<10}"
                        if status == "DELIVERED":
                            delivered_orders.append((client_name, dish_name, price))
                            total_revenue += price
                            if price > 0:
                                line += f" | Incasso: {price}cr"
                        if reason:
                            line += f" | Dettaglio: {reason}"
                        f.write(line + "\n")
                        
                    f.write("\n--- REPORT VENDITE DEL TURNO ---\n")
                    if not delivered_orders:
                        f.write("Nessun ordine consegnato in questo turno.\n")
                    else:
                        for client_name, dish_name, price in delivered_orders:
                            f.write(f"- {dish_name} servito a {client_name}: +{price}cr\n")
                        f.write(f"\nTOTALE INCASSO TURNO: {total_revenue}cr\n")
        except Exception:
            pass

    def _add_order_log(self, client_name: str, order_text: str) -> None:
        self.turn_orders_log.append({
            "client_name": client_name,
            "order_text": order_text,
            "dish_name": "Nessun piatto associato",
            "status": "PENDING",
            "reason": "",
            "price": 0
        })
        self._update_serving_log()

    def _update_order_log(self, client_name: str, status: str, dish_name: str = "", reason: str = "", price: int = 0) -> None:
        for order in reversed(self.turn_orders_log):
            if order["client_name"] == client_name:
                if status:
                    order["status"] = status
                if dish_name:
                    order["dish_name"] = dish_name
                if reason:
                    order["reason"] = reason
                if price > 0:
                    order["price"] = price
                break
        self._update_serving_log()

    def _reset_turn_purchase_tracking(self) -> None:
        self.turn_purchases = []
        self._seen_purchase_lines = set()
        self.state.auction_spend_total = 0

    async def _maybe_close_restaurant_for_client_overload(self) -> None:
        threshold = max(0, int(self.config.max_active_clients_before_close))
        if threshold <= 0:
            return
        if self.state.current_phase != "serving":
            return
        if self._closed_for_client_overload:
            return

        # Count only PENDING orders (not all-time seen clients)
        pending_count = sum(len(q) for q in self.pending_orders.values())
        clients_seen = pending_count
        if clients_seen <= threshold:
            return

        response = await self.mcp_gateway.call_tool(
            "update_restaurant_is_open",
            {"is_open": False},
        )
        if response is None:
            log(
                "SERVE",
                (
                    "Tentativo chiusura per overload fallito: "
                    f"clienti={clients_seen}, soglia={threshold}"
                ),
                event="serve.overload.close.failed",
                level="WARN",
                clients_seen=clients_seen,
                threshold=threshold,
            )
            return

        self._closed_for_client_overload = True
        log(
            "SERVE",
            f"Ristorante chiuso per overload clienti ({clients_seen} > soglia {threshold})",
            event="serve.overload.closed",
            level="WARN",
            clients_seen=clients_seen,
            threshold=threshold,
        )

    def _log_turn_sales_summary(self) -> None:
        total_revenue = 0
        delivered_orders = []
        for info in self.turn_orders_log:
            if info.get("status") == "DELIVERED":
                price = info.get("price", 0)
                delivered_orders.append(f"{info.get('dish_name')} a {info.get('client_name')} (+{price}cr)")
                total_revenue += price
                
        log(
            "SERVE",
            f"--- REPORT VENDITE TURNO {self.state.current_turn_id} ---",
            event="serve.sales.summary_header",
        )
        for order_str in delivered_orders:
            log("SERVE", f"  - {order_str}", event="serve.sales.item")
        log(
            "SERVE",
            f"TOTALE INCASSO TURNO: {total_revenue}cr ({len(delivered_orders)} ordini)",
            event="serve.sales.summary",
            delivered_count=len(delivered_orders),
            total_revenue=total_revenue
        )

    def _log_turn_purchase_summary(self, *, reason: str) -> None:
        if not self.turn_purchases:
            return

        by_ingredient: dict[str, dict[str, Any]] = {}
        total_qty = 0
        total_spend = 0
        total_requested = 0

        for row in self.turn_purchases:
            ingredient = str(row["ingredient"])
            requested = int(row["requested_qty"])
            qty = int(row["bought_qty"])
            total = int(row["total_price"])
            unit = int(row["unit_price"])
            total_requested += max(0, requested)
            total_qty += qty
            total_spend += total

            agg = by_ingredient.setdefault(
                ingredient,
                {"qty": 0, "total": 0, "unit_prices": set()},
            )
            agg["qty"] += qty
            agg["total"] += total
            agg["unit_prices"].add(unit)

        log(
            "BID",
            f"Riepilogo acquisti ({reason}): {len(self.turn_purchases)} righe, "
            f"{total_qty} unita, spesa totale {total_spend}cr",
            event="bid.result.summary",
            purchases_count=len(self.turn_purchases),
            total_quantity=total_qty,
            total_spend=total_spend,
        )

        if total_qty > 0:
            avg_unit_price = total_spend / total_qty
            prev_ema = float(self.state.observed_unit_price_ema or 0.0)
            if prev_ema > 0:
                ema_unit_price = (prev_ema * 0.65) + (avg_unit_price * 0.35)
            else:
                ema_unit_price = avg_unit_price

            self.state.observed_unit_price_avg = float(avg_unit_price)
            self.state.observed_unit_price_ema = float(ema_unit_price)
            self.state.observed_unit_price_turn_id = int(self.state.current_turn_id or 0)
            log(
                "BID",
                (
                    "Segnale prezzo mercato aggiornato: "
                    f"media_turno={avg_unit_price:.2f}cr, ema={ema_unit_price:.2f}cr"
                ),
                event="bid.market_price.signal.updated",
                avg_unit_price=round(avg_unit_price, 4),
                ema_unit_price=round(ema_unit_price, 4),
                source_turn_id=self.state.current_turn_id,
            )

        if total_requested > 0:
            fill_rate = max(0.0, min(1.0, float(total_qty) / float(total_requested)))
            prev_fill_ema = float(self.state.auction_fill_rate_ema or 1.0)
            fill_rate_ema = (prev_fill_ema * 0.60) + (fill_rate * 0.40)
            self.state.auction_fill_rate_last = fill_rate
            self.state.auction_fill_rate_ema = fill_rate_ema
            if fill_rate < 0.55:
                self.state.auction_shortage_streak = min(6, int(self.state.auction_shortage_streak) + 1)
            elif fill_rate > 0.80:
                self.state.auction_shortage_streak = max(0, int(self.state.auction_shortage_streak) - 1)
            log(
                "BID",
                (
                    "Fill-rate asta aggiornato: "
                    f"riempimento={fill_rate:.2%}, ema={fill_rate_ema:.2%}, "
                    f"streak_scarsita={self.state.auction_shortage_streak}"
                ),
                event="bid.fill_rate.updated",
                fill_rate=round(fill_rate, 6),
                fill_rate_ema=round(fill_rate_ema, 6),
                total_requested=total_requested,
                total_bought=total_qty,
                shortage_streak=int(self.state.auction_shortage_streak),
                source_turn_id=self.state.current_turn_id,
            )

        for ingredient in sorted(by_ingredient.keys()):
            agg = by_ingredient[ingredient]
            qty = int(agg["qty"])
            total = int(agg["total"])
            avg = (total / qty) if qty > 0 else 0.0
            unit_prices = sorted(int(p) for p in agg["unit_prices"])
            prices_str = ",".join(str(p) for p in unit_prices)
            log(
                "BID",
                f"  {ingredient}: qty={qty}, prezzi_unitari=[{prices_str}]cr, "
                f"media={avg:.2f}cr, totale={total}cr",
                event="bid.result.summary.item",
                ingredient=ingredient,
                quantity=qty,
                unit_prices=unit_prices,
                avg_unit_price=round(avg, 2),
                total_spend=total,
            )

    def _capture_own_purchases_from_message(self, *, sender: Any, payload: Any) -> None:
        if str(sender).strip().lower() != "server":
            return

        team_id = str(self.config.team_id).strip()
        if not team_id:
            return

        text = str(payload or "")
        if "try to buy:" not in text:
            return

        new_rows: list[dict[str, Any]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line in self._seen_purchase_lines:
                continue

            match = _AUCTION_PURCHASE_LINE_RE.match(line)
            if not match:
                continue

            self._seen_purchase_lines.add(line)
            if match.group("restaurant_id") != team_id:
                continue

            row = {
                "ingredient": match.group("ingredient").strip(),
                "requested_qty": int(match.group("requested_qty")),
                "bought_qty": int(match.group("bought_qty")),
                "unit_price": int(match.group("unit_price")),
                "total_price": int(match.group("total_price")),
            }
            self.turn_purchases.append(row)
            self.state.auction_spend_total += int(row["total_price"])
            new_rows.append(row)

        if not new_rows:
            return

        for row in new_rows:
            log(
                "BID",
                f"Acquisto: {row['ingredient']} x{row['bought_qty']} @ {row['unit_price']}cr "
                f"(tot {row['total_price']}cr)",
                event="bid.result.purchase",
                ingredient=row["ingredient"],
                quantity=row["bought_qty"],
                unit_price=row["unit_price"],
                total_price=row["total_price"],
            )

        self._log_turn_purchase_summary(reason="results_update")

    def _dish_price(self, dish_name: str) -> int | None:
        for item in self.state.active_menu:
            name = str(item.get("name", "")).strip()
            if name == dish_name:
                try:
                    return int(item.get("price", 0))
                except (TypeError, ValueError):
                    return None
        return None

    def _log_ready_dishes_summary(self, reason: str) -> None:
        ready: list[tuple[str, int]] = []
        for item in self.state.active_menu:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue
            if self.state.dishes_ready.get(dish_name) is not True:
                continue
            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                price = 0
            ready.append((dish_name, price))

        if not ready:
            log(
                "SERVE",
                f"Piatti pronti ({reason}): nessuno",
                event="dish.ready.summary",
                ready_count=0,
            )
            return

        summary = " | ".join(f"{dish} ({price}cr)" for dish, price in ready)
        log(
            "SERVE",
            f"Piatti pronti ({reason}): {summary}",
            event="dish.ready.summary",
            ready_count=len(ready),
            ready_dishes=[{"name": dish, "price": price} for dish, price in ready],
        )

    async def _prepare_on_demand(self, dish_name: str, *, reason: str) -> None:
        if dish_name in self.state.dishes_in_preparation:
            return

        # Reserve the slot immediately to avoid duplicate prepare calls for the
        # same dish while the MCP request is in flight.
        self.state.dishes_in_preparation.add(dish_name)
        self.state.dishes_preparing += 1
        self.state.dishes_ready[dish_name] = False

        response = await self.mcp_gateway.call_tool("prepare_dish", {"dish_name": dish_name})
        if response is None:
            self.state.dishes_in_preparation.discard(dish_name)
            self.state.dishes_preparing = max(0, self.state.dishes_preparing - 1)
            log(
                "SERVE",
                f"Preparazione '{dish_name}' fallita",
                event="dish.prepare.failed",
                level="ERROR",
                dish_name=dish_name,
                reason=reason,
            )
            return

        log(
            "SERVE",
            f"'{dish_name}' messo in preparazione",
            event="dish.prepare.submitted",
            dish_name=dish_name,
            reason=reason,
        )

    async def _serve_ready_dish(self, *, dish_name: str, client_id: str, client_name: str) -> bool:
        response = await self.mcp_gateway.call_tool("serve_dish", {"dish_name": dish_name, "client_id": client_id})
        if response is None:
            self._update_order_log(client_name, "FAILED", dish_name=dish_name, reason="Errore chiamata MCP serve_dish")
            log(
                "SERVE",
                f"Servizio fallito per '{dish_name}' verso '{client_name}'",
                event="serve.submit.failed",
                level="ERROR",
                client_id=client_id,
                dish_name=dish_name,
                client_name=client_name,
            )
            return False

        self.state.dishes_ready[dish_name] = False
        dish_price = self._dish_price(dish_name) or 0
        self._update_order_log(client_name, "DELIVERED", dish_name=dish_name, price=dish_price)
        log(
            "SERVE",
            f"Servito '{dish_name}' a '{client_name}' ({self._dish_price(dish_name) or 0}cr)",
            event="serve.submitted",
            client_id=client_id,
            dish_name=dish_name,
            client_name=client_name,
            dish_price=self._dish_price(dish_name),
        )
        self._log_ready_dishes_summary("post-serve")
        return True

    async def game_started(self, data: dict[str, Any]) -> None:
        turn_id = int(data.get("turn_id", self.state.current_turn_id or 0) or 0)
        self.state.reset_for_game_start(turn_id)
        self.pending_orders.clear()
        self.turn_orders_log.clear()
        self._closed_for_client_overload = False
        self._update_serving_log()
        self._reset_turn_purchase_tracking()
        set_log_context(turn_id=self.state.current_turn_id, phase=self.state.current_phase)
        log(
            "EVENT",
            f"Partita iniziata — turno {self.state.current_turn_id}",
            event="event.game_started",
            event_data=data,
        )
        await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": True})

    async def game_phase_changed(self, data: dict[str, Any]) -> None:
        if data.get("turn_id") is not None:
            self.state.current_turn_id = int(data["turn_id"])

        phase = data.get("phase", "unknown")
        self.state.current_phase = str(phase)
        if phase != "serving":
            self._closed_for_client_overload = False
        set_log_context(turn_id=self.state.current_turn_id, phase=self.state.current_phase)
        log(
            "PHASE",
            f"→ {phase}",
            event="phase.changed",
            event_data=data,
        )
        handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "speaking": self.phase_runner.speaking_phase_started,
            "closed_bid": lambda: self.phase_runner.closed_bid_phase_started(data),
            "waiting": self.phase_runner.waiting_phase_started,
            "serving": self.phase_runner.serving_phase_started,
            "stopped": self.phase_runner.end_turn,
        }

        if phase == "closed_bid":
            self._reset_turn_purchase_tracking()

        handler = handlers.get(phase)
        if handler:
            await handler()
            if phase == "waiting":
                self.pending_orders.clear()
                self.turn_orders_log.clear()
                self._update_serving_log()
                self._log_turn_purchase_summary(reason="waiting_start")
            elif phase == "stopped":
                self._log_turn_purchase_summary(reason="turn_end")
                self._log_turn_sales_summary()
        else:
            log(
                "PHASE",
                f"fase non gestita: {phase}",
                event="phase.unhandled",
                level="WARN",
            )

    async def game_reset(self, data: dict[str, Any]) -> None:
        self.state.current_phase = "stopped"
        self.pending_orders.clear()
        self.turn_orders_log.clear()
        self._closed_for_client_overload = False
        self._reset_turn_purchase_tracking()
        set_log_context(phase=self.state.current_phase)
        log("EVENT", "Partita resettata", event="event.game_reset", event_data=data)

    async def client_spawned(self, data: dict[str, Any]) -> None:
        client_name = str(data.get("clientName", data.get("name", "")))
        order_text = str(data.get("orderText", data.get("order", "")))
        self._add_order_log(client_name, order_text)
        await self._maybe_close_restaurant_for_client_overload()
        log(
            "SERVE",
            f"Cliente '{client_name}' ordina: {order_text}",
            event="serve.request.received",
            client_name=client_name,
            order_text=order_text,
        )

        # Fast heuristic match first (no LLM call)
        dish_name, intolerances = pick_dish_for_order(
            order_text, self.state.active_menu, self.state.all_recipes
        )

        # If heuristic fails, use AI agent to classify client and pick best dish
        if not dish_name and self.state.active_menu:
            log(
                "AGENT",
                f"Matching euristico fallito per '{client_name}' — uso AI agent",
                event="client_agent.fallback.triggered",
                client_name=client_name,
                order_text=order_text,
                menu_size=len(self.state.active_menu),
            )
            ai_dish, ai_intolerances = await pick_dish_with_ai(
                order_text,
                client_name,
                self.state.active_menu,
                all_recipes=self.state.all_recipes,
            )
            if ai_dish:
                dish_name = ai_dish
                # Merge intolerances from both sources
                intolerances = list(set(intolerances) | set(ai_intolerances))

        if not dish_name:
            if intolerances:
                self._update_order_log(client_name, "FAILED", reason=f"Intolleranza rilevata: {intolerances}")
                log(
                    "SERVE",
                    f"Intolleranza rilevata per '{client_name}': {intolerances} — piatto non servito",
                    event="serve.request.intolerance_conflict",
                    level="WARN",
                    client_name=client_name,
                    order_text=order_text,
                    intolerances=intolerances,
                )
            else:
                self._update_order_log(client_name, "FAILED", reason="Nessun piatto corrispondente trovato nel menu")
                log(
                    "SERVE",
                    f"Nessun piatto per '{client_name}' (cerca: {order_text}, menu_size={len(self.state.active_menu)})",
                    event="serve.request.no_matching_dish",
                    client_name=client_name,
                    order_text=order_text,
                    menu_size=len(self.state.active_menu),
                )
            return

        client_id = get_client_id_from_event(data)
        if not client_id:
            log(
                "SERVE",
                f"ID cliente mancante per '{client_name}', cerco nei meals...",
                event="serve.client_id.resolve_from_meals",
                client_name=client_name,
                dish_name=dish_name,
            )
            client_id = await resolve_client_id_from_meals(
                order_text,
                client_name,
                current_turn_id=self.state.current_turn_id,
                get_my_meals_fn=lambda turn_id: self.api_client.get_my_meals(
                    turn_id,
                    restaurant_id=self.config.team_id,
                ),
            )

        if not client_id:
            self._update_order_log(client_name, "FAILED", dish_name=dish_name, reason="Impossibile risolvere ID cliente")
            log(
                "SERVE",
                f"Impossibile identificare il cliente '{client_name}' (ordine: {order_text})",
                event="serve.client_id.resolution_failed",
                level="WARN",
                order_text=order_text,
                client_name=client_name,
                dish_name=dish_name,
            )
            return

        log(
            "SERVE",
            f"Cliente identificato: {client_id}",
            event="serve.client_id.resolved",
            client_id=client_id,
            dish_name=dish_name,
        )
        if self.state.dishes_ready.get(dish_name) is True:
            await self._serve_ready_dish(
                dish_name=dish_name,
                client_id=client_id,
                client_name=client_name,
            )
            return

        self.pending_orders[dish_name].append(
            {
                "client_id": client_id,
                "client_name": client_name,
            }
        )
        queue_len = len(self.pending_orders[dish_name])
        self._update_order_log(client_name, "PENDING", dish_name=dish_name)
        log(
            "SERVE",
            f"Piatto '{dish_name}' non pronto: cliente in coda ({queue_len})",
            event="serve.request.queued",
            dish_name=dish_name,
            client_name=client_name,
            queue_length=queue_len,
            dish_price=self._dish_price(dish_name),
        )
        await self._prepare_on_demand(dish_name, reason="client_request")

    async def preparation_complete(self, data: dict[str, Any]) -> None:
        dish_name = str(data.get("dish", "")).strip()
        if not dish_name:
            return

        self.state.dishes_ready[dish_name] = True
        self.state.dishes_in_preparation.discard(dish_name)
        self.state.dishes_preparing = max(0, self.state.dishes_preparing - 1)
        log("SERVE", f"Piatto '{dish_name}' pronto ✓", event="dish.prepare.completed", dish_name=dish_name)
        self._log_ready_dishes_summary("post-preparation")

        queue = self.pending_orders.get(dish_name)
        if not queue:
            return

        next_order = queue[0]
        served = await self._serve_ready_dish(
            dish_name=dish_name,
            client_id=next_order["client_id"],
            client_name=next_order["client_name"],
        )
        if served:
            # Re-fetch queue reference after async call (may have changed)
            queue = self.pending_orders.get(dish_name)
            if queue and queue[0].get("client_id") == next_order["client_id"]:
                queue.pop(0)
            if not queue:
                self.pending_orders.pop(dish_name, None)

        # Re-fetch again before triggering next preparation
        if self.pending_orders.get(dish_name):
            await self._prepare_on_demand(dish_name, reason="queued_client")

    async def heartbeat(self, data: dict[str, Any]) -> None:
        _ = data
        return

    async def message(self, data: dict[str, Any]) -> None:
        sender = data.get("sender")
        payload = data.get("payload")
        self._capture_own_purchases_from_message(sender=sender, payload=payload)
        log("MSG", f"from={sender} payload={payload}", event="message.received", sender=sender)

    async def new_message(self, data: dict[str, Any]) -> None:
        sender_name = data.get("senderName", data.get("senderId", "unknown"))
        text = data.get("text", "")
        log("DM", f"from={sender_name}: {text}", event="message.direct.received", sender=sender_name)

    async def dispatch_event(self, event_type: str, event_data: dict[str, Any]) -> None:
        if event_type == "heartbeat":
            # Avoid noisy warnings and keep the current phase context unchanged.
            return

        handler = self.event_handlers.get(event_type)
        if not handler:
            log(
                "EVENT",
                f"no handler for event_type={event_type}",
                event="event.unhandled",
                level="WARN",
                event_type=event_type,
            )
            return

        try:
            await handler(event_data)
        except Exception as exc:
            log(
                "ERROR",
                f"handler failed for {event_type}: {exc}",
                event="event.handler.failed",
                event_type=event_type,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
