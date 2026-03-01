from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from game_client.config import ClientConfig, get_firebase_cred_path
from game_client.logging_utils import log
from game_client.state import GameState
from game_client.strategy import (
    build_low_cost_bids,
    choose_target_recipes_for_bidding,
    estimate_recipe_cost,
)


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

    async def speaking_phase_started(self) -> None:
        log("PHASE", "speaking")

    async def closed_bid_phase_started(self, data: dict[str, Any] | None = None) -> None:
        if data and data.get("turn_id") is not None:
            self.state.current_turn_id = int(data["turn_id"])

        log("PHASE", f"closed_bid turn={self.state.current_turn_id}")

        self.state.target_recipes = await choose_target_recipes_for_bidding(
            self.state.current_turn_id,
            get_recipes_fn=self.api_client.get_recipes,
            collect_turn_meals_fn=self.collect_turn_meals_fn,
            summarize_unserved_requests_fn=self.summarize_unserved_requests_fn,
            max_target_recipes=self.config.max_target_recipes,
        )

        if not self.state.target_recipes:
            log("BID", "Nessuna ricetta target trovata")
            return

        bids, bid_map = build_low_cost_bids(
            self.state.target_recipes,
            min_ingredient_bid=self.config.min_ingredient_bid,
            max_ingredient_bid=self.config.max_ingredient_bid,
            portions_per_recipe=self.config.portions_per_recipe,
        )
        self.state.ingredient_bids = bid_map

        if not bids:
            log("BID", "Nessun bid da inviare")
            return

        recipe_names = [str(r.get("name")) for r in self.state.target_recipes]
        log("BID", f"Target ricette: {recipe_names}")
        log(
            "BID",
            (
                f"Invio {len(bids)} bids "
                f"(range {self.config.min_ingredient_bid}-{self.config.max_ingredient_bid})"
            ),
        )
        await self.mcp_gateway.call_tool("closed_bid", {"bids": bids})

    async def waiting_phase_started(self) -> None:
        log("PHASE", "waiting")
        firebase_cred_path = get_firebase_cred_path()

        if self.state.current_turn_id <= 0:
            log("BID", f"Skip bid history fetch: invalid turn={self.state.current_turn_id}")
        else:
            try:
                await self.fetch_and_save_bid_history_fn(
                    turn_id=self.state.current_turn_id,
                    api_key=self.config.team_api_key,
                    base_url=self.config.base_url,
                    cred_path=firebase_cred_path,
                )
            except Exception as exc:
                log(
                    "BID",
                    (
                        "Impossibile ottenere storico bid: "
                        f"turn={self.state.current_turn_id} "
                        f"cred_path={firebase_cred_path} error={exc}"
                    ),
                )

        if not self.state.target_recipes:
            log("MENU", "TARGET_RECIPES vuoto, menu non aggiornato")
            return

        is_cost_1x_test = (
            self.config.pricing_mode == "cost_1x_test"
            and self.config.pricing_test_turn > 0
            and self.state.current_turn_id == self.config.pricing_test_turn
        )
        if is_cost_1x_test:
            log("MENU", f"Pricing mode attivo: cost_1x_test (turn={self.state.current_turn_id})")

        menu_items: list[dict[str, Any]] = []
        for dish_index, recipe in enumerate(self.state.target_recipes):
            dish_name = str(recipe.get("name", "")).strip()
            if not dish_name:
                continue

            ingredient_cost = estimate_recipe_cost(
                recipe,
                self.state.ingredient_bids,
                min_ingredient_bid=self.config.min_ingredient_bid,
            )
            final_price, pricing_mode_applied = self.compute_menu_price_fn(
                ingredient_cost,
                is_cost_1x_test=is_cost_1x_test,
                dish_index=dish_index,
            )

            menu_items.append({"name": dish_name, "price": final_price})
            log(
                "MENU-PRICE",
                (
                    f"dish={dish_name} cost={ingredient_cost} "
                    f"price={final_price} mode={pricing_mode_applied}"
                ),
            )

        if not menu_items:
            log("MENU", "Nessuna voce menu da salvare")
            return

        self.state.active_menu = menu_items
        await self.mcp_gateway.call_tool("save_menu", {"items": menu_items})
        log("MENU", f"Menu aggiornato: {self.state.active_menu}")

    async def serving_phase_started(self) -> None:
        log("PHASE", "serving")

        try:
            log("SELLER", f"Running surplus seller for turn {self.state.current_turn_id}...")
            await self.run_seller_agent_fn(self.state.current_turn_id or 0)
            log("SELLER", "Surplus seller completed.")
        except Exception as exc:
            log("ERROR", f"Seller agent failed: {exc}")

        if not self.state.active_menu:
            log("SERVE", "Menu vuoto, nessuna preparazione")
            return

        for item in self.state.active_menu:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue

            self.state.dishes_ready[dish_name] = False
            await self.mcp_gateway.call_tool("prepare_dish", {"dish_name": dish_name})
            await asyncio.sleep(0.2)

    async def end_turn(self) -> None:
        log("PHASE", f"stopped turn={self.state.current_turn_id}")

        if self.state.current_turn_id <= 0:
            return

        db = self.init_firebase_fn()
        if db is None:
            log("TRACK", "Firebase non disponibile, skip salvataggio tracker")
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
        except Exception as exc:
            log("TRACK", f"Errore salvataggio tracker: {exc}")
