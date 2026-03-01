from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from game_client.config import ClientConfig, get_firebase_cred_path
from game_client.logging_utils import log, set_log_context
from game_client.prestige_strategy import (
    TrendDirection,
    analyze_trend_direction,
    build_prestige_bids,
    compute_trend_adjusted_price,
    select_prestige_balanced_recipes,
)
from game_client.state import GameState
from game_client.strategy import estimate_recipe_cost
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

    @staticmethod
    def _get_previous_turns(turn_id: int) -> list[int]:
        if turn_id <= 1:
            return []
        return list(range(max(1, turn_id - 2), turn_id))

    async def _ensure_trend_plan_for_current_turn(self) -> None:
        turn_id = int(self.state.current_turn_id or 0)
        if turn_id <= 0:
            return

        existing_plan = self.state.next_turn_trend_plan
        if existing_plan is not None and existing_plan.target_turn_id == turn_id:
            return

        previous_turns = self._get_previous_turns(turn_id)
        log(
            "TREND",
            f"Analisi trend per turno {turn_id} (precedenti: {previous_turns})",
            event="trend.plan.requested",
            source_turn_id=turn_id,
            previous_turns=previous_turns,
        )
        try:
            report = await self.run_trend_analysis_fn(
                turn_id,
                previous_turns if previous_turns else None,
            )
        except Exception as exc:
            log(
                "TREND",
                f"trend analysis failed for turn={turn_id}: {exc}",
                event="trend.plan.failed",
                level="ERROR",
                source_turn_id=turn_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return

        plan, reason = parse_trend_plan(
            source_turn_id=turn_id,
            target_turn_id=turn_id,
            report=report,
        )
        if plan is None:
            log(
                "TREND",
                f"trend plan unusable for turn={turn_id}: {reason}",
                event="trend.plan.unusable",
                level="WARN",
                source_turn_id=turn_id,
            )
            return

        self.state.next_turn_trend_plan = plan
        log(
            "TREND",
            f"Trend plan: {len(plan.menu)} piatti nel menu, {len(plan.bids)} ingredienti nelle offerte",
            event="trend.plan.stored",
            source_turn_id=turn_id,
            menu_size=len(plan.menu),
            bids_size=len(plan.bids),
        )

    def _set_phase_context(self, phase: str) -> None:
        self.state.current_phase = phase
        set_log_context(turn_id=self.state.current_turn_id, phase=phase)

    async def speaking_phase_started(self) -> None:
        self._set_phase_context("speaking")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase annunci", event="phase.started")

    async def closed_bid_phase_started(self, data: dict[str, Any] | None = None) -> None:
        if data and data.get("turn_id") is not None:
            self.state.current_turn_id = int(data["turn_id"])

        self._set_phase_context("closed_bid")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase offerta chiusa", event="phase.started")

        await self._ensure_trend_plan_for_current_turn()
        trend_plan = self.state.next_turn_trend_plan

        # 1. Determine trend direction
        trend_direction = analyze_trend_direction(trend_plan)
        self.state.trend_direction = trend_direction

        # 2. Fetch all recipes
        all_recipes = await self.api_client.get_recipes()

        # 3. Select 3 prestige-balanced recipes (one per tier)
        self.state.target_recipes = select_prestige_balanced_recipes(all_recipes, trend_direction)

        if not self.state.target_recipes:
            log(
                "BID",
                "Nessuna ricetta target trovata",
                event="bid.plan.empty",
                level="WARN",
            )
            return

        # 4. Build bids using trend plan reference amounts where available
        ref_bid_amounts: dict[str, int] = {}
        if trend_plan is not None:
            for ing, row in trend_plan.bids.items():
                try:
                    ref_bid_amounts[str(ing)] = int(row["amount"])
                except (KeyError, TypeError, ValueError):
                    pass

        bids, bid_map = build_prestige_bids(
            self.state.target_recipes,
            ref_bid_amounts,
            self.config.min_ingredient_bid,
            self.config.max_ingredient_bid,
            self.config.portions_per_recipe,
        )
        self.state.ingredient_bids = bid_map

        if not bids:
            log("BID", "Nessun bid da inviare", event="bid.payload.empty", level="WARN")
            return

        # 5. Submit bids
        log(
            "BID",
            f"{len(bids)} offerte pronte (mercato: {trend_direction.value})",
            event="bid.payload.built",
            bids_count=len(bids),
            trend_direction=trend_direction.value,
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

        await self._ensure_trend_plan_for_current_turn()

        if not self.state.target_recipes:
            log(
                "MENU",
                "TARGET_RECIPES vuoto, menu non aggiornato",
                event="menu.update.skipped.no_targets",
                level="WARN",
            )
            return

        trend_direction = self.state.trend_direction or TrendDirection.BALANCED

        menu_items: list[dict[str, Any]] = []
        for recipe in self.state.target_recipes:
            dish_name = str(recipe.get("name", "")).strip()
            if not dish_name:
                continue

            try:
                prestige = int(recipe.get("prestige", 50))
            except (TypeError, ValueError):
                prestige = 50

            ingredient_cost = estimate_recipe_cost(
                recipe,
                self.state.ingredient_bids,
                min_ingredient_bid=self.config.min_ingredient_bid,
            )
            final_price = compute_trend_adjusted_price(ingredient_cost, prestige, trend_direction, dish_name)

            menu_items.append({"name": dish_name, "price": final_price})

        if not menu_items:
            log("MENU", "Nessuna voce menu da salvare", event="menu.update.empty", level="WARN")
            return

        self.state.active_menu = menu_items
        response = await self.mcp_gateway.call_tool("save_menu", {"items": menu_items})
        if response is None:
            log("MENU", "Salvataggio menu fallito", event="menu.save.failed", level="ERROR", menu_size=len(menu_items))
            return
        menu_str = " | ".join(f"{item['name']} {item['price']}cr" for item in menu_items)
        log("MENU", f"Menu → {menu_str}", event="menu.saved", menu_size=len(menu_items))

    async def serving_phase_started(self) -> None:
        self._set_phase_context("serving")
        log("PHASE", f"Turno {self.state.current_turn_id}: fase servizio", event="phase.started")

        try:
            log(
                "SELLER",
                f"Avvio seller surplus per turno {self.state.current_turn_id}",
                event="seller.started",
            )
            await self.run_seller_agent_fn(self.state.current_turn_id or 0)
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
            log(
                "SERVE",
                "Menu vuoto, nessuna preparazione",
                event="dish.prepare.skipped.empty_menu",
                level="WARN",
            )
            return

        for item in self.state.active_menu:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue

            self.state.dishes_ready[dish_name] = False
            response = await self.mcp_gateway.call_tool("prepare_dish", {"dish_name": dish_name})
            if response is None:
                log("SERVE", f"Preparazione '{dish_name}' fallita", event="dish.prepare.failed", level="ERROR", dish_name=dish_name)
            else:
                log("SERVE", f"'{dish_name}' messo in preparazione", event="dish.prepare.submitted", dish_name=dish_name)
            await asyncio.sleep(0.2)

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
            return

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
