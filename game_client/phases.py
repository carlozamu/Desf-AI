from __future__ import annotations

import asyncio
import math
from typing import Any, Awaitable, Callable

from game_client.config import ClientConfig, get_firebase_cred_path
from game_client.logging_utils import log, set_log_context
from game_client.market_buyer import buy_missing_ingredients
from game_client.market_seller import liquidate_inventory_on_stockout
from game_client.prestige_strategy import (
    TrendDirection,
    analyze_trend_direction,
    build_prestige_bids,
    select_prestige_balanced_recipes,
)
from game_client.state import GameState
from game_client.strategy import estimate_recipe_cost, recipe_ingredients
from game_client.trend_strategy import parse_trend_plan


class PhaseRunner:
    def __init__(
        self,
        *,
        config: ClientConfig,
        state: GameState,
        api_client: Any,
        mcp_gateway: Any,
        run_seller_agent_fn: Callable[[int], Awaitable[None]],
        collect_turn_meals_fn: Callable[[int], Awaitable[dict[str, Any]]],
        summarize_unserved_requests_fn: Callable[[dict[str, Any]], list[dict[str, Any]]],
        fetch_and_save_bid_history_fn: Callable[..., Awaitable[Any]],
        compute_menu_price_fn: Callable[..., tuple[int, str]],
        init_firebase_fn: Callable[[], Any],
        collect_all_menus_fn: Callable[[], Awaitable[dict[str, Any]]],
        save_meals_to_firebase_fn: Callable[..., bool],
        save_menus_to_firebase_fn: Callable[..., bool],
        run_trend_analysis_fn: Callable[[int, list[int] | None], Awaitable[str]],
    ) -> None:
        self.config = config
        self.state = state
        self.api_client = api_client
        self.mcp_gateway = mcp_gateway
        self.run_seller_agent_fn = run_seller_agent_fn
        self.collect_turn_meals_fn = collect_turn_meals_fn
        self.summarize_unserved_requests_fn = summarize_unserved_requests_fn
        self.fetch_and_save_bid_history_fn = fetch_and_save_bid_history_fn
        self.compute_menu_price_fn = compute_menu_price_fn
        self.init_firebase_fn = init_firebase_fn
        self.collect_all_menus_fn = collect_all_menus_fn
        self.save_meals_to_firebase_fn = save_meals_to_firebase_fn
        self.save_menus_to_firebase_fn = save_menus_to_firebase_fn
        self.run_trend_analysis_fn = run_trend_analysis_fn
        self._serving_prewarm_task: asyncio.Task[Any] | None = None
        self._trend_prefetch_task: asyncio.Task[Any] | None = None
        self._trend_prefetch_turn_id: int = 0

    @staticmethod
    def _get_previous_turns(turn_id: int) -> list[int]:
        if turn_id <= 1:
            return []
        return list(range(max(1, turn_id - 2), turn_id))

    async def _ensure_trend_plan_for_target_turn(
        self,
        *,
        source_turn_id: int,
        target_turn_id: int,
    ) -> None:
        source_turn = int(source_turn_id or 0)
        target_turn = int(target_turn_id or 0)
        if source_turn <= 0 or target_turn <= 0:
            return

        existing_plan = self.state.next_turn_trend_plan
        if existing_plan is not None and int(existing_plan.target_turn_id) == target_turn:
            return

        previous_turns = self._get_previous_turns(source_turn)
        log(
            "TREND",
            (
                f"Analisi trend post-turno: source={source_turn} -> target={target_turn} "
                f"(precedenti source: {previous_turns})"
            ),
            event="trend.plan.requested",
            source_turn_id=source_turn,
            target_turn_id=target_turn,
            previous_turns=previous_turns,
        )
        try:
            report = await self.run_trend_analysis_fn(
                source_turn,
                previous_turns if previous_turns else None,
            )
        except Exception as exc:
            log(
                "TREND",
                f"trend analysis failed for source={source_turn} target={target_turn}: {exc}",
                event="trend.plan.failed",
                level="ERROR",
                source_turn_id=source_turn,
                target_turn_id=target_turn,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return

        plan, reason = parse_trend_plan(
            source_turn_id=source_turn,
            target_turn_id=target_turn,
            report=report,
        )
        if plan is None:
            if existing_plan is not None and int(existing_plan.target_turn_id) == target_turn:
                self.state.next_turn_trend_plan = None
            log(
                "TREND",
                f"trend plan unusable for source={source_turn} target={target_turn}: {reason}",
                event="trend.plan.unusable",
                level="WARN",
                source_turn_id=source_turn,
                target_turn_id=target_turn,
            )
            return

        self.state.next_turn_trend_plan = plan
        log(
            "TREND",
            (
                f"Trend plan pronto per turno {target_turn}: "
                f"{len(plan.menu)} piatti menu, {len(plan.bids)} ingredienti bid"
            ),
            event="trend.plan.stored",
            source_turn_id=source_turn,
            target_turn_id=target_turn,
            menu_size=len(plan.menu),
            bids_size=len(plan.bids),
        )

    def _start_trend_prefetch_for_next_turn(self, *, source_turn_id: int) -> None:
        source_turn = int(source_turn_id or 0)
        target_turn = source_turn + 1
        if source_turn <= 0:
            return

        existing_plan = self.state.next_turn_trend_plan
        if existing_plan is not None and int(existing_plan.target_turn_id) == target_turn:
            log(
                "TREND",
                f"Prefetch trend skip: piano gia pronto per turno target {target_turn}",
                event="trend.prefetch.skipped.already_ready",
                source_turn_id=source_turn,
                target_turn_id=target_turn,
            )
            return

        running = self._trend_prefetch_task
        if running is not None:
            if not running.done() and int(self._trend_prefetch_turn_id) == target_turn:
                log(
                    "TREND",
                    f"Prefetch trend gia in corso per turno target {target_turn}",
                    event="trend.prefetch.already_running",
                    source_turn_id=source_turn,
                    target_turn_id=target_turn,
                )
                return
            if not running.done():
                running.cancel()
                log(
                    "TREND",
                    (
                        "Prefetch trend cancellato per nuovo target turno: "
                        f"{self._trend_prefetch_turn_id} -> {target_turn}"
                    ),
                    event="trend.prefetch.cancelled",
                    source_turn_id=source_turn,
                    target_turn_id=target_turn,
                )

        task = asyncio.create_task(
            self._ensure_trend_plan_for_target_turn(
                source_turn_id=source_turn,
                target_turn_id=target_turn,
            )
        )
        self._trend_prefetch_task = task
        self._trend_prefetch_turn_id = target_turn
        log(
            "TREND",
            (
                f"Prefetch trend avviato a fine turno {source_turn} "
                f"per target turno {target_turn}"
            ),
            event="trend.prefetch.started",
            source_turn_id=source_turn,
            target_turn_id=target_turn,
        )

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            if self._trend_prefetch_task is done_task:
                self._trend_prefetch_task = None
                self._trend_prefetch_turn_id = 0
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(
                    "TREND",
                    f"Prefetch trend in errore: {exc}",
                    event="trend.prefetch.failed",
                    level="ERROR",
                    source_turn_id=source_turn,
                    target_turn_id=target_turn,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            else:
                log(
                    "TREND",
                    f"Prefetch trend completato per target turno {target_turn}",
                    event="trend.prefetch.completed",
                    source_turn_id=source_turn,
                    target_turn_id=target_turn,
                )

        task.add_done_callback(_on_done)

    async def _apply_budget_cap(self, bids: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Scale bid quantities down if total cost exceeds turn budget constraints."""
        restaurant_info = await self.api_client.get_restaurant_info()
        if not restaurant_info:
            return bids

        try:
            balance = float(restaurant_info.get("balance", restaurant_info.get("credits", 0)) or 0)
        except (TypeError, ValueError):
            return bids

        if balance <= 0:
            return bids

        turn_spend_cap = max(100, int(self.state.turn_budget_cap_effective or self.config.turn_spend_cap))
        spend_floor_cfg = float(self.state.auction_spend_floor_ratio_effective or 0.0)
        if spend_floor_cfg <= 0:
            spend_floor_cfg = float(self.config.auction_spend_floor_ratio)
        spend_floor_cfg = max(0.0, min(1.0, spend_floor_cfg))

        budget_limit_balance = balance * 0.90
        budget_limit = min(budget_limit_balance, float(turn_spend_cap))
        total_cost = sum(int(b.get("bid", 0)) * int(b.get("quantity", 1)) for b in bids)

        spend_floor_ratio = spend_floor_cfg
        spend_floor = int(round(budget_limit * spend_floor_ratio))
        if bids and spend_floor > 0 and total_cost < spend_floor:
            ranked_indices = sorted(
                range(len(bids)),
                key=lambda i: (
                    int(bids[i].get("bid", 0)),
                    int(bids[i].get("quantity", 0)),
                ),
                reverse=True,
            )
            gap = max(0, spend_floor - total_cost)
            if ranked_indices and gap > 0:
                weight_sum = sum(max(1, len(ranked_indices) - rank) for rank in range(len(ranked_indices)))
                for rank, idx in enumerate(ranked_indices):
                    row = bids[idx]
                    bid_val = max(1, int(row.get("bid", 0)))
                    weight = max(1, len(ranked_indices) - rank)
                    share_cost = int(round((gap * weight) / max(1, weight_sum)))
                    extra_qty = max(0, int(math.ceil(share_cost / bid_val)))
                    if extra_qty <= 0:
                        continue
                    row["quantity"] = max(1, int(row.get("quantity", 1)) + extra_qty)

                total_cost = sum(int(b.get("bid", 0)) * int(b.get("quantity", 1)) for b in bids)
                if total_cost < spend_floor:
                    top_idx = ranked_indices[0]
                    top_bid = max(1, int(bids[top_idx].get("bid", 0)))
                    top_extra = int(math.ceil((spend_floor - total_cost) / top_bid))
                    bids[top_idx]["quantity"] = max(
                        1,
                        int(bids[top_idx].get("quantity", 1)) + max(0, top_extra),
                    )
                    total_cost = sum(int(b.get("bid", 0)) * int(b.get("quantity", 1)) for b in bids)

                log(
                    "BID",
                    (
                        "Asta sotto-spesa: quantità riallocate per saturare il budget "
                        f"({total_cost:.0f}cr / floor {spend_floor:.0f}cr)"
                    ),
                    event="bid.budget.floor_boosted",
                    spend_floor=spend_floor,
                    spend_floor_ratio=spend_floor_ratio,
                    total_cost=total_cost,
                    budget_limit=budget_limit,
                )

        if total_cost <= budget_limit:
            log(
                "BID",
                (
                    f"Budget OK: {total_cost:.0f} / {balance:.0f} "
                    f"(limite effettivo {budget_limit:.0f}, cap turno {turn_spend_cap})"
                ),
                event="bid.budget.ok",
                total_cost=total_cost,
                balance=balance,
                budget_limit=budget_limit,
                turn_spend_cap=turn_spend_cap,
            )
            return bids

        scale = budget_limit / total_cost
        scaled: list[dict[str, Any]] = []
        for b in bids:
            new_qty = max(1, int(int(b.get("quantity", 1)) * scale))
            scaled.append({**b, "quantity": new_qty})

        # Hard-cap refinement: first reduce quantities >1, then drop costlier lines if needed.
        def _row_cost(row: dict[str, Any]) -> int:
            return int(row.get("bid", 0)) * int(row.get("quantity", 0))

        new_total = sum(_row_cost(row) for row in scaled)

        # Pass 1: reduce quantities down to 1 starting from less competitive rows
        # so high-bid ingredients keep higher depth whenever possible.
        if new_total > budget_limit:
            reducible = sorted(
                range(len(scaled)),
                key=lambda i: (
                    int(scaled[i].get("bid", 0)),
                    -int(scaled[i].get("quantity", 0)),
                ),
            )
            for idx in reducible:
                row = scaled[idx]
                bid_val = int(row.get("bid", 0))
                qty = int(row.get("quantity", 0))
                while qty > 1 and new_total > budget_limit:
                    qty -= 1
                    new_total -= bid_val
                row["quantity"] = qty
                if new_total <= budget_limit:
                    break

        # Pass 2: if still above budget (all qty=1), drop lowest-bid lines first.
        if new_total > budget_limit:
            keep = list(scaled)
            keep.sort(
                key=lambda row: (
                    int(row.get("bid", 0)),
                    int(row.get("quantity", 0)),
                ),
            )
            dropped = 0
            while keep and new_total > budget_limit:
                removed = keep.pop(0)
                new_total -= _row_cost(removed)
                dropped += 1
            scaled = keep
            if dropped > 0:
                log(
                    "BID",
                    f"Budget hard-cap: rimosse {dropped} linee bid",
                    event="bid.budget.hardcap_trimmed",
                    dropped_rows=dropped,
                    scaled_cost=new_total,
                    budget_limit=budget_limit,
                )

        if new_total < 0:
            new_total = 0
        log(
            "BID",
            (
                f"Budget cap applicato: {total_cost:.0f} → {new_total:.0f} "
                f"(saldo {balance:.0f}, limite effettivo {budget_limit:.0f}, cap turno {turn_spend_cap})"
            ),
            event="bid.budget.capped",
            level="WARN",
            original_cost=total_cost,
            scaled_cost=new_total,
            balance=balance,
            scale_factor=scale,
            budget_limit=budget_limit,
            turn_spend_cap=turn_spend_cap,
        )
        return scaled

    def _set_phase_context(self, phase: str) -> None:
        if phase != "serving":
            self._cancel_serving_prewarm(reason=f"phase_change:{phase}")
        self.state.current_phase = phase
        set_log_context(turn_id=self.state.current_turn_id, phase=phase)

    def _cancel_serving_prewarm(self, *, reason: str) -> None:
        task = self._serving_prewarm_task
        if task is None:
            return
        if not task.done():
            task.cancel()
            log(
                "SERVE",
                f"Prewarm cancellato ({reason})",
                event="dish.prepare.prewarm.cancelled",
                reason=reason,
            )
        self._serving_prewarm_task = None

    def _schedule_serving_prewarm(self, dish_names: list[str]) -> None:
        if not dish_names:
            return

        self._cancel_serving_prewarm(reason="serving_restart")
        task = asyncio.create_task(self._run_serving_prewarm(dish_names))
        self._serving_prewarm_task = task

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            if self._serving_prewarm_task is done_task:
                self._serving_prewarm_task = None
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(
                    "SERVE",
                    f"Prewarm task in errore: {exc}",
                    event="dish.prepare.prewarm.failed",
                    level="ERROR",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        task.add_done_callback(_on_done)

    async def _run_serving_prewarm(self, dish_names: list[str]) -> None:
        requested = len(dish_names)
        submitted = 0
        skipped = 0
        failed = 0
        semaphore = asyncio.Semaphore(max(1, int(self.config.prewarm_concurrency)))

        async def _prepare_one(dish_name: str) -> None:
            nonlocal submitted, skipped, failed

            async with semaphore:
                if self.state.current_phase != "serving":
                    skipped += 1
                    return
                if self.state.dishes_ready.get(dish_name) is True:
                    skipped += 1
                    return
                if dish_name in self.state.dishes_in_preparation:
                    skipped += 1
                    return

                self.state.dishes_in_preparation.add(dish_name)
                self.state.dishes_preparing += 1
                self.state.dishes_ready[dish_name] = False

                try:
                    response = await self.mcp_gateway.call_tool(
                        "prepare_dish",
                        {"dish_name": dish_name},
                    )
                except asyncio.CancelledError:
                    self.state.dishes_in_preparation.discard(dish_name)
                    self.state.dishes_preparing = max(0, self.state.dishes_preparing - 1)
                    raise

                if response is None:
                    self.state.dishes_in_preparation.discard(dish_name)
                    self.state.dishes_preparing = max(0, self.state.dishes_preparing - 1)
                    failed += 1
                    log(
                        "SERVE",
                        f"Preparazione prewarm fallita per '{dish_name}'",
                        event="dish.prepare.failed",
                        level="ERROR",
                        dish_name=dish_name,
                        reason="serving_prewarm",
                    )
                    return

                submitted += 1
                log(
                    "SERVE",
                    f"'{dish_name}' messo in preparazione",
                    event="dish.prepare.submitted",
                    dish_name=dish_name,
                    reason="serving_prewarm",
                )

        try:
            await asyncio.gather(*(_prepare_one(name) for name in dish_names))
        except asyncio.CancelledError:
            log(
                "SERVE",
                (
                    "Prewarm interrotto: "
                    f"richiesti={requested}, inviati={submitted}, skipped={skipped}, falliti={failed}"
                ),
                event="dish.prepare.prewarm.cancelled",
                requested=requested,
                submitted=submitted,
                skipped=skipped,
                failed=failed,
            )
            raise

        log(
            "SERVE",
            (
                "Prewarm completato: "
                f"richiesti={requested}, inviati={submitted}, skipped={skipped}, falliti={failed}"
            ),
            event="dish.prepare.prewarm.completed",
            requested=requested,
            submitted=submitted,
            skipped=skipped,
            failed=failed,
        )

    @staticmethod
    def _recipe_key(name: str) -> str:
        return str(name or "").strip().lower()

    async def _load_previous_turn_demand(self) -> dict[str, dict[str, int]]:
        previous_turn = int(self.state.current_turn_id or 0) - 1
        if previous_turn <= 0:
            return {}

        if (
            self.state.previous_turn_demand
            and int(self.state.previous_turn_demand_turn_id) == previous_turn
        ):
            return self.state.previous_turn_demand

        try:
            meals_data = await self.collect_turn_meals_fn(previous_turn)
            ranking = self.summarize_unserved_requests_fn(meals_data)
        except Exception as exc:
            log(
                "TREND",
                f"Domanda turno precedente non disponibile: {exc}",
                event="demand.previous_turn.failed",
                level="WARN",
                source_turn_id=previous_turn,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return {}

        demand: dict[str, dict[str, int]] = {}
        for row in ranking:
            name = self._recipe_key(row.get("recipe_name", ""))
            if not name:
                continue
            demand[name] = {
                "total_requests": int(row.get("total_requests", 0) or 0),
                "unserved_requests": int(row.get("unserved_requests", 0) or 0),
                "executed_requests": int(row.get("executed_requests", 0) or 0),
            }

        self.state.previous_turn_demand = demand
        self.state.previous_turn_demand_turn_id = previous_turn
        log(
            "TREND",
            f"Domanda turno {previous_turn} caricata ({len(demand)} ricette)",
            event="demand.previous_turn.loaded",
            source_turn_id=previous_turn,
            recipes_count=len(demand),
        )
        return demand

    def _demand_priority(self, recipe_name: str, demand_map: dict[str, dict[str, int]]) -> int:
        row = demand_map.get(self._recipe_key(recipe_name), {})
        unserved = int(row.get("unserved_requests", 0) or 0)
        total = int(row.get("total_requests", 0) or 0)
        executed = int(row.get("executed_requests", 0) or 0)
        return (unserved * 5) + (total * 2) + executed

    def _select_dishes_for_prewarm(
        self,
        menu_items: list[dict[str, Any]],
        *,
        demand_map: dict[str, dict[str, int]],
        target_count: int,
    ) -> list[str]:
        if target_count <= 0:
            return []

        ranked: list[tuple[int, int, int, str]] = []
        for item in menu_items:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue

            row = demand_map.get(self._recipe_key(dish_name), {})
            unserved = int(row.get("unserved_requests", 0) or 0)
            total = int(row.get("total_requests", 0) or 0)
            executed = int(row.get("executed_requests", 0) or 0)
            demand_score = (unserved * 6) + (total * 2) + executed

            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                price = 0
            volume_bias = max(0, 250 - max(0, price))
            score = (demand_score * 1000) + volume_bias
            ranked.append((score, demand_score, volume_bias, dish_name))

        ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)

        selected: list[str] = []
        seen: set[str] = set()
        for _, _, _, dish_name in ranked:
            if dish_name in seen:
                continue
            selected.append(dish_name)
            seen.add(dish_name)
            if len(selected) >= target_count:
                break
        return selected

    def _build_competitor_price_index(
        self,
        menus_data: dict[str, dict[str, Any]] | None,
    ) -> dict[str, float]:
        if not isinstance(menus_data, dict):
            return {}

        my_id = str(self.config.team_id)
        price_map: dict[str, float] = {}
        for rest_id, info in menus_data.items():
            if str(rest_id) == my_id:
                continue
            if not isinstance(info, dict):
                continue
            for item in info.get("menu", []):
                if not isinstance(item, dict):
                    continue
                dish_name = self._recipe_key(item.get("name", ""))
                raw_price = item.get("price")
                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    continue
                if not dish_name or price <= 0:
                    continue
                old = price_map.get(dish_name)
                if old is None or price < old:
                    price_map[dish_name] = price

        if price_map:
            log(
                "MENU",
                f"Prezzi competitor indicizzati: {len(price_map)} piatti",
                event="menu.competitor.indexed",
                indexed_count=len(price_map),
            )
        return price_map

    def _estimate_target_price(
        self,
        *,
        ingredient_cost: int,
        prestige: int,
        trend_direction: TrendDirection,
        trend_reference_price: int | None,
        competitor_low_price: float | None,
    ) -> int:
        tier = self._prestige_tier(prestige)
        multiplier_matrix: dict[str, dict[TrendDirection, float]] = {
            "low": {
                TrendDirection.LOW_COST: 1.5,
                TrendDirection.BALANCED: 2.0,
                TrendDirection.HIGH_COST: 2.4,
            },
            "mid": {
                TrendDirection.LOW_COST: 2.2,
                TrendDirection.BALANCED: 2.9,
                TrendDirection.HIGH_COST: 3.8,
            },
            "high": {
                TrendDirection.LOW_COST: 3.4,
                TrendDirection.BALANCED: 4.4,
                TrendDirection.HIGH_COST: 5.8,
            },
        }
        base_price = int(round(ingredient_cost * multiplier_matrix[tier][trend_direction]))
        if trend_reference_price is not None and trend_reference_price > 0:
            base_price = int(round((base_price * 0.55) + (trend_reference_price * 0.45)))

        if tier == "low":
            min_margin = max(ingredient_cost + 1, int(round(ingredient_cost * 1.20)))
            base_price = min(base_price, 95)
        elif tier == "mid":
            min_margin = max(ingredient_cost + 1, int(round(ingredient_cost * 1.45)))
            base_price = min(max(base_price, 55), 260)
        else:
            min_margin = max(ingredient_cost + 1, int(round(ingredient_cost * 1.90)))
            base_price = max(base_price, 210)

        if competitor_low_price is not None and competitor_low_price > 0:
            if tier == "low":
                base_price = min(base_price, int(round(competitor_low_price * 0.96)))
            elif tier == "mid":
                base_price = min(base_price, int(round(competitor_low_price * 0.98)))
            else:
                base_price = max(base_price, int(round(competitor_low_price * 0.98)))

        final_price = max(min_margin, base_price)
        return max(10, min(999, int(final_price)))

    def _build_feasible_menu(
        self,
        *,
        recipes: list[dict[str, Any]],
        inventory: dict[str, int],
        demand_map: dict[str, dict[str, int]],
        trend_direction: TrendDirection,
        trend_price_by_name: dict[str, int],
        competitor_low_by_name: dict[str, float],
        min_ingredient_bid: int,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
        candidates: list[dict[str, Any]] = []

        for recipe in recipes:
            dish_name = str(recipe.get("name", "")).strip()
            if not dish_name:
                continue

            ingredients = recipe_ingredients(recipe)
            if not ingredients:
                continue

            feasible = True
            for ing, qty in ingredients.items():
                if int(inventory.get(str(ing), 0)) < int(qty):
                    feasible = False
                    break
            if not feasible:
                continue

            try:
                prestige = int(recipe.get("prestige", 50))
            except (TypeError, ValueError):
                prestige = 50

            ingredient_cost = estimate_recipe_cost(
                recipe,
                self.state.ingredient_bids,
                min_ingredient_bid=min_ingredient_bid,
            )
            price = self._estimate_target_price(
                ingredient_cost=ingredient_cost,
                prestige=prestige,
                trend_direction=trend_direction,
                trend_reference_price=trend_price_by_name.get(dish_name),
                competitor_low_price=competitor_low_by_name.get(self._recipe_key(dish_name)),
            )
            demand_score = self._demand_priority(dish_name, demand_map)
            score = (demand_score * 1000) + (prestige * 10) - ingredient_cost

            candidates.append(
                {
                    "name": dish_name,
                    "price": price,
                    "score": score,
                    "ingredient_cost": ingredient_cost,
                    "prestige": prestige,
                    "band": self._price_band(price),
                }
            )

        if not candidates:
            return [], {}

        candidates.sort(key=lambda row: (int(row["score"]), int(row["prestige"])), reverse=True)
        cap = min(int(self.config.max_menu_items), len(candidates))
        if cap <= 0:
            return [], {}

        by_band: dict[str, list[dict[str, Any]]] = {"budget": [], "mid": [], "premium": []}
        for item in candidates:
            by_band[str(item["band"])].append(item)

        desired_budget = int(round(cap * 0.45))
        desired_mid = int(round(cap * 0.35))
        desired_premium = max(1, cap - desired_budget - desired_mid)
        desired = {
            "budget": max(1 if by_band["budget"] else 0, desired_budget),
            "mid": max(1 if by_band["mid"] else 0, desired_mid),
            "premium": max(1 if by_band["premium"] else 0, desired_premium),
        }

        selected: list[dict[str, Any]] = []
        used_names: set[str] = set()

        for band in ("budget", "mid", "premium"):
            for item in by_band[band]:
                if len(selected) >= cap or desired[band] <= 0:
                    break
                name = str(item["name"])
                if name in used_names:
                    continue
                selected.append(item)
                used_names.add(name)
                desired[band] -= 1

        if len(selected) < cap:
            for item in candidates:
                if len(selected) >= cap:
                    break
                name = str(item["name"])
                if name in used_names:
                    continue
                selected.append(item)
                used_names.add(name)

        # Ensure at least one very high-priced option (>200) for premium clients.
        has_over_200 = any(int(item["price"]) > 200 for item in selected)
        if not has_over_200 and selected:
            best = max(selected, key=lambda row: (int(row["prestige"]), int(row["price"])))
            best["price"] = max(int(best["price"]), 210)
            best["band"] = self._price_band(int(best["price"]))

        menu_items = [{"name": str(item["name"]), "price": int(item["price"])} for item in selected]
        recipe_meta = {
            str(item["name"]): {
                "ingredient_cost": int(item["ingredient_cost"]),
                "prestige": int(item["prestige"]),
            }
            for item in selected
        }
        return menu_items, recipe_meta

    def _log_client_segment_coverage(self, menu_items: list[dict[str, Any]]) -> None:
        segments = {
            "esploratore": 0,   # budget
            "famiglia": 0,      # value
            "saggio": 0,        # quality
            "astrobarone": 0,   # luxury
        }
        for item in menu_items:
            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                price = 0
            if price <= 60:
                segments["esploratore"] += 1
            elif price <= 160:
                segments["famiglia"] += 1
            elif price <= 260:
                segments["saggio"] += 1
            else:
                segments["astrobarone"] += 1

        log(
            "MENU",
            (
                "Copertura clienti: "
                f"Esploratore={segments['esploratore']} | "
                f"Famiglia={segments['famiglia']} | "
                f"Saggio={segments['saggio']} | "
                f"Astrobarone={segments['astrobarone']}"
            ),
            event="menu.client_coverage.summary",
            segments=segments,
        )

    @staticmethod
    def _prestige_tier(prestige: int) -> str:
        if prestige <= 40:
            return "low"
        if prestige <= 74:
            return "mid"
        return "high"

    def _enforce_tier_coverage(
        self,
        selected_recipes: list[dict[str, Any]],
        *,
        all_recipes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Ensure at least one recipe per tier (low/mid/high) when possible.
        Keeps existing priority and appends only missing tiers.
        """
        output = list(selected_recipes)
        used = {str(r.get("name", "")).strip() for r in output if r.get("name")}
        def _safe_prestige(recipe: dict[str, Any]) -> int:
            try:
                return int(recipe.get("prestige", 50))
            except (TypeError, ValueError):
                return 50

        have_tiers = {
            self._prestige_tier(_safe_prestige(r))
            for r in output
            if isinstance(r, dict)
        }
        missing_tiers = [tier for tier in ("low", "mid", "high") if tier not in have_tiers]
        if not missing_tiers:
            return output

        for tier in missing_tiers:
            candidate = next(
                (
                    r
                    for r in all_recipes
                    if str(r.get("name", "")).strip()
                    and str(r.get("name", "")).strip() not in used
                    and self._prestige_tier(_safe_prestige(r)) == tier
                ),
                None,
            )
            if candidate is None:
                continue
            output.append(candidate)
            used.add(str(candidate.get("name", "")).strip())
            if len(output) >= self.config.max_target_recipes:
                break

        return output

    def _top_up_recipe_pool(
        self,
        selected_recipes: list[dict[str, Any]],
        *,
        all_recipes: list[dict[str, Any]],
        trend_direction: TrendDirection,
        minimum_size: int,
    ) -> list[dict[str, Any]]:
        """
        Ensure we have a minimum number of menu candidates to cover
        multiple price/user segments in serving.
        """
        target_size = max(1, min(int(minimum_size), int(self.config.max_target_recipes)))
        if len(selected_recipes) >= target_size:
            return selected_recipes

        output = list(selected_recipes)
        used = {str(r.get("name", "")).strip() for r in output if r.get("name")}
        fallback = select_prestige_balanced_recipes(all_recipes, trend_direction)

        for recipe in fallback:
            name = str(recipe.get("name", "")).strip()
            if not name or name in used:
                continue
            output.append(recipe)
            used.add(name)
            if len(output) >= target_size:
                break

        if len(output) > len(selected_recipes):
            log(
                "BID",
                f"Top-up ricette: {len(selected_recipes)} → {len(output)}",
                event="bid.recipe_pool.topped_up",
                before_count=len(selected_recipes),
                after_count=len(output),
                target_size=target_size,
            )
        return output

    def _select_single_fallback_recipe(
        self,
        *,
        all_recipes: list[dict[str, Any]],
        demand_map: dict[str, dict[str, int]],
    ) -> dict[str, Any] | None:
        """
        Build an emergency fallback plan based on a single recipe:
        prioritize demand signal, then low ingredient complexity.
        """
        ranked: list[tuple[int, int, int, int, dict[str, Any]]] = []
        for recipe in all_recipes:
            recipe_name = str(recipe.get("name", "")).strip()
            if not recipe_name:
                continue
            ingredients = recipe_ingredients(recipe)
            if not ingredients:
                continue
            demand_score = self._demand_priority(recipe_name, demand_map)
            total_qty = sum(max(1, int(qty)) for qty in ingredients.values())
            distinct_count = len(ingredients)
            try:
                prestige = int(recipe.get("prestige", 50))
            except (TypeError, ValueError):
                prestige = 50
            ranked.append((demand_score, -total_qty, -distinct_count, prestige, recipe))

        if not ranked:
            return None

        ranked.sort(
            key=lambda row: (row[0], row[1], row[2], row[3]),
            reverse=True,
        )
        return ranked[0][4]

    def _concentrate_recipe_pool(
        self,
        selected_recipes: list[dict[str, Any]],
        *,
        max_size: int,
    ) -> list[dict[str, Any]]:
        """
        Reduce recipe breadth while maximizing ingredient overlap.
        Useful after stockout / low fill-rate so auction quantities concentrate
        on a smaller shared ingredient set.
        """
        target_size = max(1, int(max_size))
        if len(selected_recipes) <= target_size:
            return selected_recipes

        ranked = sorted(
            selected_recipes,
            key=lambda r: int(r.get("prestige", 0) or 0),
            reverse=True,
        )
        chosen = [ranked[0]]
        used = {self._recipe_key(ranked[0].get("name", ""))}
        covered = set(recipe_ingredients(ranked[0]).keys())

        while len(chosen) < target_size:
            best_recipe: dict[str, Any] | None = None
            best_score: tuple[int, int, int] | None = None
            for recipe in ranked:
                key = self._recipe_key(recipe.get("name", ""))
                if not key or key in used:
                    continue
                ingredients = set(recipe_ingredients(recipe).keys())
                overlap = len(ingredients & covered)
                prestige = int(recipe.get("prestige", 0) or 0)
                score = (overlap, len(ingredients), prestige)
                if best_score is None or score > best_score:
                    best_score = score
                    best_recipe = recipe

            if best_recipe is None:
                break

            chosen.append(best_recipe)
            used.add(self._recipe_key(best_recipe.get("name", "")))
            covered.update(recipe_ingredients(best_recipe).keys())

        return chosen

    def _derive_turn_budget_policy(self, *, balance: float) -> dict[str, Any]:
        """
        Build a budget policy for closed-bid using game economy assumptions:
        start credits + fixed credits gain each turn.
        """
        turn_id = max(1, int(self.state.current_turn_id or 1))
        expected_balance = float(
            int(self.config.initial_credits)
            + max(0, turn_id - 1) * int(self.config.credits_per_turn_income)
        )
        wealth_ratio = float(balance) / max(1.0, expected_balance)
        cap_cfg = float(self.config.turn_spend_cap)
        max_recipes = max(1, int(self.config.max_target_recipes))
        base_floor = max(0.0, min(1.0, float(self.config.auction_spend_floor_ratio)))

        low_balance_trigger = max(float(self.config.initial_credits) * 0.5, cap_cfg * 0.30)
        expansion_trigger = max(cap_cfg * 1.5, expected_balance * 0.30)
        monopoly_trigger = max(cap_cfg * 3.0, expected_balance * 0.60)

        mode = "growth"
        cap_ratio = 0.85
        balance_guard_ratio = 0.78
        floor_ratio = max(0.82, base_floor * 0.92)
        recipe_ratio = 0.95

        if balance <= low_balance_trigger or wealth_ratio < 0.15:
            mode = "survival"
            cap_ratio = 0.70
            balance_guard_ratio = 0.70
            floor_ratio = max(0.70, base_floor * 0.85)
            recipe_ratio = 0.80
        elif balance >= monopoly_trigger or (wealth_ratio >= 0.90 and balance >= cap_cfg * 3.0):
            mode = "monopoly"
            cap_ratio = 1.00
            balance_guard_ratio = 0.90
            floor_ratio = max(base_floor, 0.97)
            recipe_ratio = 1.00
        elif balance >= expansion_trigger or wealth_ratio >= 0.55:
            mode = "expansion"
            cap_ratio = 0.95
            balance_guard_ratio = 0.82
            floor_ratio = max(base_floor, 0.90)
            recipe_ratio = 1.00

        cap_from_mode = cap_cfg * cap_ratio
        cap_from_balance = max(100.0, float(balance) * balance_guard_ratio)
        turn_budget_cap = int(max(100.0, min(cap_cfg, cap_from_mode, cap_from_balance)))
        recipe_cap = max(6, min(max_recipes, int(round(max_recipes * recipe_ratio))))
        if mode in {"expansion", "monopoly"}:
            recipe_cap = max_recipes

        return {
            "mode": mode,
            "turn_budget_cap": turn_budget_cap,
            "floor_ratio": max(0.0, min(1.0, floor_ratio)),
            "recipe_cap": recipe_cap,
            "expected_balance": expected_balance,
            "wealth_ratio": wealth_ratio,
        }

    def _derive_dynamic_bid_bounds(
        self,
        *,
        trend_plan: Any | None,
        trend_direction: TrendDirection,
    ) -> tuple[int, int]:
        """
        Compute effective bid bounds for this turn.
        Priority:
        1) Trend-plan bid amounts (adaptive to current competition)
        2) Direction-based fallback when no trend amounts are available
        """
        floor = max(1, int(self.config.min_ingredient_bid))
        ceil = max(floor + 1, int(self.config.max_ingredient_bid))
        amounts: list[int] = []
        source = "fallback"
        max_amount = 0
        if trend_plan is not None:
            for row in getattr(trend_plan, "bids", {}).values():
                try:
                    amount = int(row["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                if amount > 0:
                    amounts.append(amount)
                    if amount > max_amount:
                        max_amount = amount

        if amounts:
            amounts.sort()
            median = amounts[(len(amounts) - 1) // 2]
            p75 = amounts[(3 * (len(amounts) - 1)) // 4]
            # Closed-bid must maximize ingredient coverage: keep trend prices
            # competitive (no down-scaling).
            effective_min = max(floor, min(ceil - 2, max(1, median)))
            effective_max = min(ceil, max(effective_min + 2, max(2, p75, max_amount)))
            source = "trend_plan"
        else:
            fallback_by_direction = {
                TrendDirection.LOW_COST: (
                    max(4, int(round(ceil * 0.20))),
                    max(12, int(round(ceil * 0.45))),
                ),
                TrendDirection.BALANCED: (
                    max(8, int(round(ceil * 0.35))),
                    max(18, int(round(ceil * 0.70))),
                ),
                TrendDirection.HIGH_COST: (
                    max(12, int(round(ceil * 0.50))),
                    max(30, int(round(ceil * 0.95))),
                ),
            }
            fallback_min, fallback_max = fallback_by_direction[trend_direction]
            effective_min = max(floor, int(fallback_min))
            effective_max = min(ceil, max(effective_min + 2, int(fallback_max)))
            source = "fallback"

        market_anchor = 0.0
        if float(self.state.observed_unit_price_ema or 0.0) > 0:
            market_anchor = float(self.state.observed_unit_price_ema)
        elif float(self.state.observed_unit_price_avg or 0.0) > 0:
            market_anchor = float(self.state.observed_unit_price_avg)

        if market_anchor > 0:
            anchor_offsets = {
                TrendDirection.LOW_COST: (0, 4),
                TrendDirection.BALANCED: (1, 6),
                TrendDirection.HIGH_COST: (2, 10),
            }
            off_min, off_max = anchor_offsets[trend_direction]
            anchor_min = max(1, int(round(market_anchor + off_min)))
            anchor_max = max(anchor_min + 2, int(round(market_anchor + off_max)))

            if amounts:
                effective_min = int(round((effective_min * 0.60) + (anchor_min * 0.40)))
                effective_max = int(round((effective_max * 0.60) + (anchor_max * 0.40)))
                source = "trend_plan+market_anchor"
            else:
                effective_min = anchor_min
                effective_max = anchor_max
                source = "market_anchor"

        fill_ema = float(self.state.auction_fill_rate_ema or 1.0)
        shortage_streak = int(self.state.auction_shortage_streak or 0)
        stockout_last_turn = bool(self.state.menu_stockout_last_turn)
        aggressiveness_bonus = 0
        if fill_ema < 0.45:
            aggressiveness_bonus += 6
        elif fill_ema < 0.60:
            aggressiveness_bonus += 4
        elif fill_ema < 0.75:
            aggressiveness_bonus += 2
        aggressiveness_bonus += min(4, max(0, shortage_streak))
        if stockout_last_turn:
            aggressiveness_bonus += 4

        if aggressiveness_bonus > 0:
            effective_min += max(1, aggressiveness_bonus // 2)
            effective_max += aggressiveness_bonus
            source = f"{source}+fill_rate_boost"

        # Stockout override: when last turn had no feasible menu, force
        # near-ceiling bids to recover inventory through closed-bid.
        if stockout_last_turn and ceil > floor:
            stockout_min = max(floor, int(round(ceil * 0.65)))
            effective_min = max(effective_min, stockout_min)
            effective_max = ceil
            source = f"{source}+stockout_override"

        effective_min = max(floor, min(ceil, int(effective_min)))
        effective_max = max(effective_min + 1, int(effective_max))
        effective_max = min(ceil, effective_max)

        if effective_max <= effective_min:
            effective_max = min(ceil, effective_min + 1)
            if effective_max <= effective_min:
                effective_max = effective_min

        log(
            "BID",
            f"Bid bounds dinamici: min={effective_min} max={effective_max}",
            event="bid.bounds.dynamic",
            effective_min=effective_min,
            effective_max=effective_max,
            source=source,
            samples=len(amounts),
            trend_direction=trend_direction.value,
            config_floor=floor,
            config_ceil=ceil,
            market_anchor=round(market_anchor, 4),
            market_anchor_turn_id=int(self.state.observed_unit_price_turn_id or 0),
            auction_fill_rate_ema=round(fill_ema, 6),
            auction_shortage_streak=shortage_streak,
            menu_stockout_last_turn=stockout_last_turn,
            aggressiveness_bonus=aggressiveness_bonus,
            trend_max_amount=max_amount,
        )
        return effective_min, effective_max

    @staticmethod
    def _price_band(price: int) -> str:
        if price <= 40:
            return "budget"
        if price <= 120:
            return "mid"
        return "premium"

    def _enforce_price_ladder(
        self,
        menu_items: list[dict[str, Any]],
        *,
        recipe_meta: dict[str, dict[str, int]],
    ) -> None:
        """
        Enforce at least one offer in each price band when feasible:
        - budget  <= 40
        - mid     41..120
        - premium >= 121
        """
        if len(menu_items) < 3:
            return

        counts = {"budget": 0, "mid": 0, "premium": 0}
        for item in menu_items:
            try:
                band = self._price_band(int(item.get("price", 0)))
            except (TypeError, ValueError):
                band = "mid"
            counts[band] += 1

        missing = [band for band, count in counts.items() if count == 0]
        if not missing:
            return

        indices = list(range(len(menu_items)))

        def _cost(idx: int) -> int:
            name = str(menu_items[idx].get("name", "")).strip()
            return int(recipe_meta.get(name, {}).get("ingredient_cost", 0))

        def _prestige(idx: int) -> int:
            name = str(menu_items[idx].get("name", "")).strip()
            return int(recipe_meta.get(name, {}).get("prestige", 50))

        by_cost = sorted(indices, key=lambda i: (_cost(i), _prestige(i)))
        by_mid_cost = sorted(indices, key=lambda i: abs(_cost(i) - 45))
        by_prestige = sorted(indices, key=lambda i: (_prestige(i), int(menu_items[i].get("price", 0))), reverse=True)
        used: set[int] = set()
        applied: list[dict[str, Any]] = []

        def _pick(candidates: list[int], *, max_cost: int | None = None) -> int | None:
            for idx in candidates:
                if idx in used:
                    continue
                if max_cost is not None and _cost(idx) > max_cost:
                    continue
                return idx
            return None

        for band in missing:
            if band == "budget":
                idx = _pick(by_cost, max_cost=35)
                if idx is None:
                    continue
                cost = _cost(idx)
                target = max(10, min(40, int(round(max(cost * 1.35, 24)))))
                if target < cost:
                    # Avoid guaranteed loss. If we cannot stay budget and profitable, skip.
                    continue
            elif band == "mid":
                idx = _pick(by_mid_cost, max_cost=95)
                if idx is None:
                    continue
                cost = _cost(idx)
                target = max(41, min(120, int(round(max(cost * 1.8, 70)))))
                if target < cost:
                    continue
            else:  # premium
                idx = _pick(by_prestige)
                if idx is None:
                    continue
                cost = _cost(idx)
                target = max(121, min(999, int(round(max(cost * 2.5, 160)))))

            old_price = int(menu_items[idx].get("price", 0))
            menu_items[idx]["price"] = target
            used.add(idx)
            applied.append(
                {
                    "dish_name": str(menu_items[idx].get("name", "")),
                    "band": band,
                    "old_price": old_price,
                    "new_price": target,
                    "ingredient_cost": cost,
                }
            )

        if applied:
            log(
                "MENU",
                f"Ladder prezzi applicata su {len(applied)} piatti",
                event="menu.price_ladder.applied",
                applied=applied,
            )

    async def speaking_phase_started(self) -> None:
        self._set_phase_context("speaking")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase annunci", event="phase.started")
        # Ensure the restaurant is open at each new turn even if it was closed
        # in a previous serving cycle.
        await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": True})

    async def closed_bid_phase_started(self, data: dict[str, Any] | None = None) -> None:
        if data and data.get("turn_id") is not None:
            self.state.current_turn_id = int(data["turn_id"])

        self._set_phase_context("closed_bid")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase offerta chiusa", event="phase.started")

        current_turn = int(self.state.current_turn_id or 0)
        trend_plan = self.state.next_turn_trend_plan
        if trend_plan is None:
            log(
                "TREND",
                f"Nessun trend plan precomputato per turno {current_turn}: fallback locale immediato",
                event="trend.plan.missing_for_turn",
                level="WARN",
                source_turn_id=current_turn - 1,
                target_turn_id=current_turn,
            )
        elif int(trend_plan.target_turn_id) != current_turn:
            log(
                "TREND",
                (
                    "Trend plan non allineato al turno corrente: "
                    f"target={trend_plan.target_turn_id}, current={current_turn}. "
                    "Uso fallback locale."
                ),
                event="trend.plan.target_mismatch",
                level="WARN",
                source_turn_id=int(trend_plan.source_turn_id),
                target_turn_id=int(trend_plan.target_turn_id),
                current_turn_id=current_turn,
            )
            if int(trend_plan.target_turn_id) < current_turn:
                self.state.next_turn_trend_plan = None
            trend_plan = None
        else:
            log(
                "TREND",
                (
                    f"Uso trend plan precomputato: "
                    f"source={trend_plan.source_turn_id} -> target={trend_plan.target_turn_id}"
                ),
                event="trend.plan.used_precomputed",
                source_turn_id=int(trend_plan.source_turn_id),
                target_turn_id=int(trend_plan.target_turn_id),
            )

        # 1. Determine trend direction
        trend_direction = analyze_trend_direction(trend_plan)
        self.state.trend_direction = trend_direction

        # 2. Fetch all recipes
        all_recipes = await self.api_client.get_recipes()
        all_recipes = sorted(all_recipes, key=lambda r: r.get("prestige", 0), reverse=True)
        self.state.all_recipes = all_recipes
        demand_map = await self._load_previous_turn_demand()
        restaurant_info = await self.api_client.get_restaurant_info()
        try:
            balance = float(restaurant_info.get("balance", restaurant_info.get("credits", 0)) or 0)
        except (TypeError, ValueError, AttributeError):
            balance = 0.0
        budget_policy = self._derive_turn_budget_policy(balance=balance)
        self.state.budget_mode = str(budget_policy["mode"])
        self.state.turn_budget_cap_effective = int(budget_policy["turn_budget_cap"])
        self.state.auction_spend_floor_ratio_effective = float(budget_policy["floor_ratio"])
        self.state.target_recipe_cap_effective = int(budget_policy["recipe_cap"])
        effective_recipe_cap = max(
            1,
            int(self.state.target_recipe_cap_effective or self.config.max_target_recipes),
        )
        log(
            "BID",
            (
                f"Policy budget={self.state.budget_mode}: "
                f"cap_turno={self.state.turn_budget_cap_effective}cr, "
                f"floor_ratio={self.state.auction_spend_floor_ratio_effective:.2f}, "
                f"recipe_cap={effective_recipe_cap}, "
                f"wealth={budget_policy['wealth_ratio']:.2f}x"
            ),
            event="budget.policy.selected",
            budget_mode=self.state.budget_mode,
            turn_budget_cap_effective=self.state.turn_budget_cap_effective,
            auction_spend_floor_ratio_effective=self.state.auction_spend_floor_ratio_effective,
            target_recipe_cap_effective=effective_recipe_cap,
            expected_balance=round(float(budget_policy["expected_balance"]), 2),
            wealth_ratio=round(float(budget_policy["wealth_ratio"]), 6),
            current_balance=round(balance, 2),
        )

        # 2b. Build target recipe set:
        #     - prefer trend plan menu recipes (to cover recommended price/user bands)
        #     - fallback to local balanced strategy when missing/unusable
        recipes_by_name = {
            self._recipe_key(r.get("name", "")): r
            for r in all_recipes
            if r.get("name")
        }
        selected: list[dict[str, Any]] = []

        if trend_plan is not None and trend_plan.menu:
            trend_names: list[str] = []
            for item in trend_plan.menu:
                name = str(item.get("name", "")).strip()
                if name:
                    trend_names.append(name)

            seen_names: set[str] = set()
            for name in trend_names:
                if name in seen_names:
                    continue
                recipe = recipes_by_name.get(self._recipe_key(name))
                if recipe is not None:
                    selected.append(recipe)
                    seen_names.add(name)
                if len(selected) >= effective_recipe_cap:
                    break

            if selected:
                log(
                    "BID",
                    f"Trend menu applicato: {len(selected)} ricette su {len(trend_names)} "
                    f"({', '.join(str(r.get('name', '')) for r in selected)})",
                    event="bid.recipe_pool.trend_menu",
                    selected_count=len(selected),
                    trend_menu_count=len(trend_names),
                )
            else:
                log(
                    "BID",
                    f"Trend menu senza match su catalogo ({len(trend_names)} voci) - fallback locale",
                    event="bid.recipe_pool.trend_menu.no_match",
                    level="WARN",
                )

        trend_guided_mode = bool(trend_plan is not None and trend_plan.menu and selected)

        # Keep trend as primary signal. Demand expansion is intentionally limited
        # in trend-guided mode to avoid random drift.
        if selected and demand_map and len(selected) < effective_recipe_cap:
            selected_names = {self._recipe_key(r.get("name", "")) for r in selected}
            added_from_demand = 0
            max_demand_additions = max(0, effective_recipe_cap - len(selected))
            if trend_guided_mode:
                max_demand_additions = min(1, max_demand_additions)
            for recipe_name, _ in sorted(
                demand_map.items(),
                key=lambda item: self._demand_priority(item[0], demand_map),
                reverse=True,
            ):
                key = self._recipe_key(recipe_name)
                if not key or key in selected_names:
                    continue
                recipe = recipes_by_name.get(key)
                if recipe is None:
                    continue
                selected.append(recipe)
                selected_names.add(key)
                added_from_demand += 1
                if added_from_demand >= max_demand_additions or len(selected) >= effective_recipe_cap:
                    break
            if added_from_demand > 0:
                log(
                    "BID",
                    f"Espansione trend con domanda: +{added_from_demand} ricette",
                    event="bid.recipe_pool.trend_expanded_with_demand",
                    added_count=added_from_demand,
                    total_selected=len(selected),
                )

        if not selected and demand_map:
            for recipe_name, _ in sorted(
                demand_map.items(),
                key=lambda item: self._demand_priority(item[0], demand_map),
                reverse=True,
            ):
                recipe = recipes_by_name.get(self._recipe_key(recipe_name))
                if recipe is None:
                    continue
                selected.append(recipe)
                if len(selected) >= effective_recipe_cap:
                    break
            if selected:
                log(
                    "BID",
                    f"Selezione guidata dalla domanda: {len(selected)} ricette",
                    event="bid.recipe_pool.from_demand",
                    selected_count=len(selected),
                )

        if not selected:
            selected = select_prestige_balanced_recipes(all_recipes, trend_direction)

        # Emergency fallback plan: keep one deterministic recipe to concentrate
        # ingredient collection if trend execution becomes infeasible.
        fallback_recipe = self._select_single_fallback_recipe(
            all_recipes=all_recipes,
            demand_map=demand_map,
        )
        if fallback_recipe is not None:
            fallback_name = str(fallback_recipe.get("name", "")).strip()
            selected_names = {self._recipe_key(r.get("name", "")) for r in selected}
            if fallback_name and self._recipe_key(fallback_name) not in selected_names:
                if len(selected) >= effective_recipe_cap and selected:
                    removed = selected.pop(-1)
                    log(
                        "BID",
                        (
                            "Fallback ricetta inserita sostituendo una ricetta secondaria: "
                            f"out='{removed.get('name', '')}', in='{fallback_name}'"
                        ),
                        event="bid.recipe_pool.fallback_replaced",
                        removed_recipe=str(removed.get("name", "")),
                        fallback_recipe=fallback_name,
                    )
                selected.append(fallback_recipe)
                log(
                    "BID",
                    f"Piano fallback pronto: 1 ricetta di backup '{fallback_name}'",
                    event="bid.recipe_pool.fallback_added",
                    fallback_recipe=fallback_name,
                )

        # In trend-guided mode avoid broad random diversification.
        if not trend_guided_mode:
            selected = self._enforce_tier_coverage(selected, all_recipes=all_recipes)
            selected = self._top_up_recipe_pool(
                selected,
                all_recipes=all_recipes,
                trend_direction=trend_direction,
                minimum_size=effective_recipe_cap,
            )
        else:
            log(
                "BID",
                "Modalita trend-guided: disattivo top-up ampio ricette",
                event="bid.recipe_pool.trend_guided",
                selected_count=len(selected),
                recipe_cap=effective_recipe_cap,
            )
        shortage_mode = bool(self.state.menu_stockout_last_turn) or (
            float(self.state.auction_fill_rate_ema or 1.0) < 0.60
        )
        should_concentrate = (
            shortage_mode
            and str(self.state.budget_mode or "growth") == "survival"
            and effective_recipe_cap > 8
        )
        if should_concentrate:
            focus_size = min(
                effective_recipe_cap,
                max(8, int(round(effective_recipe_cap * 0.75))),
            )
            focused = self._concentrate_recipe_pool(selected, max_size=focus_size)
            if len(focused) < len(selected):
                log(
                    "BID",
                    f"Concentrazione ricette per asta aggressiva: {len(selected)} → {len(focused)}",
                    event="bid.recipe_pool.concentrated",
                    before_count=len(selected),
                    after_count=len(focused),
                    focus_size=focus_size,
                )
            selected = focused

        # Respect configured cap.
        self.state.target_recipes = selected[:effective_recipe_cap]

        if not self.state.target_recipes:
            log(
                "BID",
                "Nessuna ricetta target trovata",
                event="bid.plan.empty",
                level="WARN",
            )
            return

        def _safe_prestige(recipe: dict[str, Any]) -> int:
            try:
                return int(recipe.get("prestige", 50))
            except (TypeError, ValueError):
                return 50

        tiers = [self._prestige_tier(_safe_prestige(r)) for r in self.state.target_recipes]
        log(
            "BID",
            f"Copertura target ricette: {len(self.state.target_recipes)} voci "
            f"(low={tiers.count('low')}, mid={tiers.count('mid')}, high={tiers.count('high')})",
            event="bid.recipe_pool.coverage",
            target_count=len(self.state.target_recipes),
            low_count=tiers.count("low"),
            mid_count=tiers.count("mid"),
            high_count=tiers.count("high"),
        )

        # 4. Build bids using trend plan reference amounts where available
        ref_bid_amounts: dict[str, int] = {}
        if trend_plan is not None:
            for ing, row in trend_plan.bids.items():
                try:
                    ref_bid_amounts[str(ing)] = int(row["amount"])
                except (KeyError, TypeError, ValueError):
                    pass

        effective_min_bid, effective_max_bid = self._derive_dynamic_bid_bounds(
            trend_plan=trend_plan,
            trend_direction=trend_direction,
        )
        self.state.bid_min_effective = effective_min_bid
        self.state.bid_max_effective = effective_max_bid

        bids, bid_map = build_prestige_bids(
            self.state.target_recipes,
            ref_bid_amounts,
            effective_min_bid,
            effective_max_bid,
            self.config.portions_per_recipe,
        )
        # Add a safety buffer on quantities so closed-bid covers recipes even
        # when auction matching is partial.
        extra_units = 0
        min_qty_by_mode = {
            "survival": 1,
            "growth": 2,
            "expansion": 3,
            "monopoly": 4,
        }
        min_qty_per_ingredient = int(
            min_qty_by_mode.get(str(self.state.budget_mode or "growth"), 2)
        )
        aggressive_quantity_mode = bool(self.state.menu_stockout_last_turn) or (
            float(self.state.auction_fill_rate_ema or 1.0) < 0.60
        )
        for row in bids:
            qty = max(min_qty_per_ingredient, int(row.get("quantity", 1)))
            if aggressive_quantity_mode:
                boost = max(3, qty)
            else:
                boost = max(1, int(math.ceil(qty * 0.50)))
            new_qty = qty + boost
            row["quantity"] = new_qty
            extra_units += new_qty - qty
        if extra_units > 0:
            log(
                "BID",
                (
                    f"Buffer quantità closed_bid applicato: +{extra_units} unità "
                    f"(mode={'aggressive' if aggressive_quantity_mode else 'standard'}, "
                    f"min_qty={min_qty_per_ingredient})"
                ),
                event="bid.quantity.buffer_applied",
                extra_units=extra_units,
                aggressive_mode=aggressive_quantity_mode,
                min_qty_per_ingredient=min_qty_per_ingredient,
            )
        self.state.ingredient_bids = bid_map

        # Breadth guardrail: keep line-count within what the budget can sustain.
        turn_spend_cap_effective = max(
            100,
            int(self.state.turn_budget_cap_effective or self.config.turn_spend_cap),
        )
        max_affordable_lines = max(1, int(turn_spend_cap_effective // max(1, effective_min_bid)))
        if len(bids) > max_affordable_lines:
            bids.sort(
                key=lambda row: (
                    int(row.get("quantity", 0)),
                    int(row.get("bid", 0)),
                ),
                reverse=True,
            )
            dropped = len(bids) - max_affordable_lines
            bids = bids[:max_affordable_lines]
            log(
                "BID",
                f"Riduzione ampiezza bid: -{dropped} linee (max sostenibile {max_affordable_lines})",
                event="bid.breadth.trimmed",
                dropped_rows=dropped,
                max_affordable_lines=max_affordable_lines,
            )

        if not bids:
            log("BID", "Nessun bid da inviare", event="bid.payload.empty", level="WARN")
            return

        # 5. Budget cap: scale bids if total cost > 80% of current balance
        bids = await self._apply_budget_cap(bids)
        self.state.closed_bid_planned_spend = sum(
            int(b.get("bid", 0)) * int(b.get("quantity", 0))
            for b in bids
        )
        self.state.market_spend_total = 0

        # 6. Submit bids
        log(
            "BID",
            f"{len(bids)} offerte pronte (mercato: {trend_direction.value})",
            event="bid.payload.built",
            bids_count=len(bids),
            trend_direction=trend_direction.value,
            planned_spend=self.state.closed_bid_planned_spend,
        )
        response = await self.mcp_gateway.call_tool("closed_bid", {"bids": bids})
        if response is None:
            log("BID", "Invio offerte fallito ✗", event="bid.submit.failed", level="ERROR", bids_count=len(bids))
        else:
            log("BID", "Offerte inviate ✓", event="bid.submitted", bids_count=len(bids))

    async def waiting_phase_started(self) -> None:
        self._set_phase_context("waiting")
        log("PHASE", f"Turno {self.state.current_turn_id}: attesa risultati offerta", event="phase.started")
        firebase_cred_path = get_firebase_cred_path()

        if self.state.current_turn_id <= 0:
            log(
                "BID",
                f"Skip bid history fetch: invalid turn={self.state.current_turn_id}",
                event="bid_history.fetch.skipped",
                level="WARN",
            )
        else:
            try:
                log(
                    "BID",
                    f"Fetching bid history for turn={self.state.current_turn_id}",
                    event="bid_history.fetch.started",
                )
                await self.fetch_and_save_bid_history_fn(
                    turn_id=self.state.current_turn_id,
                    api_key=self.config.team_api_key,
                    base_url=self.config.base_url,
                    cred_path=firebase_cred_path,
                )
                log(
                    "BID",
                    f"Bid history fetched for turn={self.state.current_turn_id}",
                    event="bid_history.fetch.completed",
                )
            except Exception as exc:
                log(
                    "BID",
                    (
                        "Impossibile ottenere storico bid: "
                        f"turn={self.state.current_turn_id} "
                        f"cred_path={firebase_cred_path} error={exc}"
                    ),
                    event="bid_history.fetch.failed",
                    level="ERROR",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        demand_map = await self._load_previous_turn_demand()

        trend_direction = self.state.trend_direction or TrendDirection.BALANCED
        trend_plan = self.state.next_turn_trend_plan
        if trend_plan is not None and int(trend_plan.target_turn_id) != int(self.state.current_turn_id or 0):
            trend_plan = None
        effective_min_bid = int(self.state.bid_min_effective or self.config.min_ingredient_bid)
        trend_price_by_name: dict[str, int] = {}
        if trend_plan is not None:
            for row in trend_plan.menu:
                try:
                    trend_price_by_name[str(row["name"]).strip()] = int(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue

        spend_cap = int(self.state.turn_budget_cap_effective or self.config.turn_spend_cap)
        auction_spend = int(self.state.auction_spend_total or self.state.closed_bid_planned_spend)
        remaining_market_budget = max(0, spend_cap - auction_spend)
        log(
            "BID",
            (
                f"Budget turno: cap={spend_cap}cr, "
                f"asta={auction_spend}cr, residuo mercato={remaining_market_budget}cr"
            ),
            event="budget.turn.summary",
            turn_spend_cap=spend_cap,
            auction_spend=auction_spend,
            remaining_market_budget=remaining_market_budget,
        )

        # Buy missing ingredients from market for bid targets, respecting remaining turn budget.
        self.state.market_spend_total = 0
        if self.config.auction_only_mode:
            log(
                "MARKET",
                "Auction-only mode attivo: skip acquisti mercato",
                event="market.buy.skipped.auction_only",
            )
        elif self.state.target_recipes and remaining_market_budget > 0:
            try:
                bought, market_spend = await buy_missing_ingredients(
                    self.mcp_gateway,
                    self.api_client,
                    self.state.target_recipes,
                    self.state.ingredient_bids,
                    default_bid=effective_min_bid,
                    max_total_spend=remaining_market_budget,
                )
                self.state.market_spend_total = int(market_spend)
                if bought:
                    log(
                        "MARKET",
                        f"Acquistati dal mercato: {bought}",
                        event="market.buy.summary",
                        bought=bought,
                        market_spend=market_spend,
                    )
            except Exception as exc:
                log(
                    "MARKET",
                    f"Market buyer fallito: {exc}",
                    event="market.buy.failed",
                    level="ERROR",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
        elif not self.state.target_recipes:
            log(
                "MARKET",
                "Skip acquisti mercato: nessuna ricetta target",
                event="market.buy.skipped.no_targets",
            )
        else:
            log(
                "MARKET",
                "Skip acquisti mercato: budget residuo esaurito",
                event="market.buy.skipped.budget_exhausted",
                remaining_market_budget=remaining_market_budget,
            )

        # Refresh inventory after market purchases.
        restaurant_info = await self.api_client.get_restaurant_info()
        inventory: dict[str, int] = {}
        if isinstance(restaurant_info, dict):
            raw_inventory = restaurant_info.get("inventory", {})
            if isinstance(raw_inventory, dict):
                for ingredient_name, qty in raw_inventory.items():
                    try:
                        parsed_qty = int(qty)
                    except (TypeError, ValueError):
                        parsed_qty = 0
                    if parsed_qty > 0:
                        inventory[str(ingredient_name)] = parsed_qty

        if not self.state.all_recipes:
            self.state.all_recipes = sorted(
                await self.api_client.get_recipes(),
                key=lambda r: r.get("prestige", 0),
                reverse=True,
            )

        competitor_low_by_name: dict[str, float] = {}
        try:
            menus_data = await self.collect_all_menus_fn()
            competitor_low_by_name = self._build_competitor_price_index(menus_data)
        except Exception as exc:
            log(
                "MENU",
                f"Impossibile leggere menu competitor: {exc}",
                event="menu.competitor.fetch.failed",
                level="WARN",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        menu_items, recipe_meta = self._build_feasible_menu(
            recipes=self.state.all_recipes,
            inventory=inventory,
            demand_map=demand_map,
            trend_direction=trend_direction,
            trend_price_by_name=trend_price_by_name,
            competitor_low_by_name=competitor_low_by_name,
            min_ingredient_bid=effective_min_bid,
        )
        log(
            "MENU",
            (
                f"Menu fattibile da inventario: {len(menu_items)} ricette "
                f"(cap configurato {self.config.max_menu_items})"
            ),
            event="menu.feasible.summary",
            feasible_count=len(menu_items),
            configured_cap=self.config.max_menu_items,
        )

        if not menu_items:
            self.state.active_menu = []
            self.state.dishes_ready = {}
            self.state.dishes_preparing = 0
            self.state.dishes_in_preparation = set()
            self.state.menu_stockout_last_turn = True

            current_turn_id = int(self.state.current_turn_id or 0)
            if self.config.stockout_liquidation_enabled:
                if current_turn_id > 0 and int(self.state.stockout_liquidation_turn_id or 0) != current_turn_id:
                    try:
                        liquidation = await liquidate_inventory_on_stockout(
                            api_client=self.api_client,
                            mcp_gateway=self.mcp_gateway,
                            inventory=inventory,
                            ingredient_bids=self.state.ingredient_bids,
                            default_price=effective_min_bid,
                            min_sell_price=int(self.config.stockout_sell_min_price),
                        )
                        self.state.stockout_liquidation_turn_id = current_turn_id
                        log(
                            "MARKET",
                            (
                                "Liquidazione stockout completata: "
                                f"sold={liquidation.get('sold_units', 0)}, "
                                f"listed={liquidation.get('listed_units', 0)}, "
                                f"failed_tx={liquidation.get('failed_transactions', 0)}, "
                                f"failed_listing={liquidation.get('failed_entries', 0)}"
                            ),
                            event="market.sell.stockout.summary",
                            sold_units=int(liquidation.get("sold_units", 0) or 0),
                            listed_units=int(liquidation.get("listed_units", 0) or 0),
                            failed_transactions=int(liquidation.get("failed_transactions", 0) or 0),
                            failed_entries=int(liquidation.get("failed_entries", 0) or 0),
                            estimated_revenue=int(liquidation.get("estimated_revenue", 0) or 0),
                        )
                    except Exception as exc:
                        log(
                            "MARKET",
                            f"Liquidazione stockout fallita: {exc}",
                            event="market.sell.stockout.failed",
                            level="ERROR",
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                else:
                    log(
                        "MARKET",
                        "Liquidazione stockout gia tentata in questo turno: skip",
                        event="market.sell.stockout.skipped.already_attempted",
                        source_turn_id=current_turn_id,
                    )
            else:
                log(
                    "MARKET",
                    "Liquidazione stockout disabilitata da configurazione",
                    event="market.sell.stockout.skipped.disabled",
                )

            await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": False})
            log(
                "MENU",
                "Nessun piatto fattibile da inventario: ristorante chiuso",
                event="menu.update.empty_and_closed",
                level="WARN",
            )
            return

        self._enforce_price_ladder(menu_items, recipe_meta=recipe_meta)

        self.state.menu_stockout_last_turn = False
        self.state.active_menu = menu_items
        self.state.dishes_ready = {str(item["name"]): False for item in menu_items if item.get("name")}
        self.state.dishes_preparing = 0
        self.state.dishes_in_preparation = set()
        response = await self.mcp_gateway.call_tool("save_menu", {"items": menu_items})
        if response is None:
            log("MENU", "Salvataggio menu fallito", event="menu.save.failed", level="ERROR", menu_size=len(menu_items))
            return
        preview_items = menu_items[:20]
        menu_str = " | ".join(f"{item['name']} {item['price']}cr" for item in preview_items)
        if len(menu_items) > len(preview_items):
            menu_str = f"{menu_str} | ... (+{len(menu_items) - len(preview_items)} piatti)"
        log("MENU", f"Menu → {menu_str}", event="menu.saved", menu_size=len(menu_items))
        coverage = {"budget": 0, "mid": 0, "premium": 0}
        for item in menu_items:
            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                price = 0
            coverage[self._price_band(price)] += 1
        log(
            "MENU",
            (
                "Copertura fasce prezzo: "
                f"budget={coverage['budget']}, "
                f"mid={coverage['mid']}, "
                f"premium={coverage['premium']}"
            ),
            event="menu.coverage.summary",
            budget_count=coverage["budget"],
            mid_count=coverage["mid"],
            premium_count=coverage["premium"],
            menu_size=len(menu_items),
        )
        self._log_client_segment_coverage(menu_items)

    async def serving_phase_started(self) -> None:
        self._set_phase_context("serving")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase servizio", event="phase.started")

        skip_seller_after_stockout = (
            not self.state.active_menu
            and int(self.state.stockout_liquidation_turn_id or 0) == int(self.state.current_turn_id or 0)
        )
        if skip_seller_after_stockout:
            log(
                "SELLER",
                "Skip seller LLM: liquidazione stockout gia eseguita in waiting",
                event="seller.skipped.stockout_already_liquidated",
                source_turn_id=self.state.current_turn_id,
            )
        else:
            try:
                log(
                    "SELLER",
                    f"Avvio seller surplus per turno {self.state.current_turn_id}",
                    event="seller.started",
                )
                seller_timeout = max(2, int(self.config.seller_timeout_seconds))
                try:
                    await asyncio.wait_for(
                        self.run_seller_agent_fn(self.state.current_turn_id or 0),
                        timeout=seller_timeout,
                    )
                except asyncio.TimeoutError:
                    log(
                        "SELLER",
                        (
                            "Seller timeout: superati "
                            f"{seller_timeout}s, continuo la fase serving senza attenderlo"
                        ),
                        event="seller.timeout",
                        level="WARN",
                        timeout_seconds=seller_timeout,
                        source_turn_id=self.state.current_turn_id,
                    )
                log("SELLER", "Seller surplus completato", event="seller.completed")
            except Exception as exc:
                log(
                    "ERROR",
                    f"Seller agent failed: {exc}",
                    event="seller.failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        if not self.state.active_menu:
            await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": False})
            log(
                "SERVE",
                "Menu vuoto, nessuna preparazione",
                event="dish.prepare.skipped.empty_menu",
                level="WARN",
            )
            return

        log(
            "SERVE",
            "Preparazione ibrida attiva: prewarm iniziale + on-demand agli ordini",
            event="dish.prepare.mode.hybrid",
            menu_size=len(self.state.active_menu),
        )
        menu_snapshot_parts: list[str] = []
        for item in self.state.active_menu[:20]:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue
            try:
                price = int(item.get("price", 0))
            except (TypeError, ValueError):
                price = 0
            menu_snapshot_parts.append(f"{dish_name} {price}cr ({self._price_band(price)})")

        hidden_count = max(0, len(self.state.active_menu) - 20)
        if hidden_count > 0:
            menu_snapshot_parts.append(f"... (+{hidden_count} piatti)")

        if menu_snapshot_parts:
            log(
                "MENU",
                "Menu attivo serving: " + " | ".join(menu_snapshot_parts),
                event="menu.serving.snapshot",
                menu_size=len(menu_snapshot_parts),
            )

        menu_size = len(self.state.active_menu)
        prewarm_fraction = float(self.config.prewarm_menu_fraction)
        prewarm_target = 0
        if prewarm_fraction > 0:
            prewarm_target = max(1, int(math.ceil(menu_size * prewarm_fraction)))
        prewarm_target = min(menu_size, prewarm_target)

        if prewarm_target <= 0:
            log(
                "SERVE",
                "Prewarm disattivato da configurazione",
                event="dish.prepare.prewarm.skipped.disabled",
                prewarm_fraction=prewarm_fraction,
                menu_size=menu_size,
            )
            return

        previous_turn = int(self.state.current_turn_id or 0) - 1
        demand_map: dict[str, dict[str, int]] = {}
        if (
            self.state.previous_turn_demand
            and int(self.state.previous_turn_demand_turn_id) == previous_turn
        ):
            demand_map = self.state.previous_turn_demand

        prewarm_dishes = self._select_dishes_for_prewarm(
            self.state.active_menu,
            demand_map=demand_map,
            target_count=prewarm_target,
        )
        if not prewarm_dishes:
            log(
                "SERVE",
                "Prewarm saltato: nessun piatto candidabile",
                event="dish.prepare.prewarm.skipped.empty",
                menu_size=menu_size,
                target_count=prewarm_target,
            )
            return

        preview = " | ".join(prewarm_dishes[:12])
        if len(prewarm_dishes) > 12:
            preview = f"{preview} | ... (+{len(prewarm_dishes) - 12} piatti)"

        log(
            "SERVE",
            (
                f"Prewarm pianificato: {len(prewarm_dishes)}/{menu_size} piatti "
                f"(target ~{prewarm_fraction:.0%}, concorrenza={self.config.prewarm_concurrency})"
            ),
            event="dish.prepare.prewarm.planned",
            menu_size=menu_size,
            target_count=prewarm_target,
            scheduled_count=len(prewarm_dishes),
            prewarm_fraction=prewarm_fraction,
            prewarm_concurrency=self.config.prewarm_concurrency,
            demand_signal="previous_turn" if demand_map else "price_bias",
        )
        log(
            "SERVE",
            f"Prewarm queue: {preview}",
            event="dish.prepare.prewarm.queue",
            queued_count=len(prewarm_dishes),
        )
        self._schedule_serving_prewarm(prewarm_dishes)

    async def end_turn(self) -> None:
        self._set_phase_context("stopped")
        log("PHASE", f"Turno {self.state.current_turn_id}: fine", event="phase.started")

        if self.state.current_turn_id <= 0:
            log("TRACK", "skip end_turn tracking: invalid turn", event="tracker.save.skipped.invalid_turn", level="WARN")
            return

        log("TRACK", "starting end_turn tracking save", event="tracker.save.started")
        db = self.init_firebase_fn()
        if db is None:
            log(
                "TRACK",
                "Firebase non disponibile, skip salvataggio tracker",
                event="tracker.save.skipped.firebase_unavailable",
                level="WARN",
            )
        else:
            try:
                meals_data, menus_data = await asyncio.gather(
                    self.collect_turn_meals_fn(self.state.current_turn_id),
                    self.collect_all_menus_fn(),
                )

                tasks: list[Awaitable[Any]] = []
                if meals_data:
                    tasks.append(
                        asyncio.to_thread(
                            self.save_meals_to_firebase_fn,
                            db,
                            self.state.current_turn_id,
                            self.config.run_id,
                            meals_data,
                        )
                    )

                if menus_data:
                    tasks.append(
                        asyncio.to_thread(
                            self.save_menus_to_firebase_fn,
                            db,
                            self.state.current_turn_id,
                            self.config.run_id,
                            menus_data,
                        )
                    )

                if tasks:
                    await asyncio.gather(*tasks)
                    log(
                        "TRACK",
                        f"Dati turno salvati su Firebase ({len(tasks)} sezioni)",
                        event="tracker.save.completed",
                        saved_sections=len(tasks),
                    )
                else:
                    log("TRACK", "no tracker payload to save", event="tracker.save.skipped.empty_payload", level="WARN")
            except Exception as exc:
                log(
                    "TRACK",
                    f"Errore salvataggio tracker: {exc}",
                    event="tracker.save.failed",
                    level="ERROR",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        self._start_trend_prefetch_for_next_turn(source_turn_id=self.state.current_turn_id)
