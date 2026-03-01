from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from game_client.config import ClientConfig, get_firebase_cred_path
from game_client.logging_utils import log, set_log_context
from game_client.market_buyer import buy_missing_ingredients
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

        budget_limit_balance = balance * 0.80
        budget_limit = min(budget_limit_balance, float(self.config.turn_spend_cap))
        total_cost = sum(int(b.get("bid", 0)) * int(b.get("quantity", 1)) for b in bids)

        if total_cost <= budget_limit:
            log(
                "BID",
                (
                    f"Budget OK: {total_cost:.0f} / {balance:.0f} "
                    f"(limite effettivo {budget_limit:.0f}, cap turno {self.config.turn_spend_cap})"
                ),
                event="bid.budget.ok",
                total_cost=total_cost,
                balance=balance,
                budget_limit=budget_limit,
                turn_spend_cap=self.config.turn_spend_cap,
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

        # Pass 1: reduce quantities down to 1 starting from costliest rows.
        if new_total > budget_limit:
            reducible = sorted(
                range(len(scaled)),
                key=lambda i: (int(scaled[i].get("bid", 0)), int(scaled[i].get("quantity", 0))),
                reverse=True,
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

        # Pass 2: if still above budget (all qty=1), drop highest-bid lines.
        if new_total > budget_limit:
            keep = list(scaled)
            keep.sort(
                key=lambda row: (
                    int(row.get("bid", 0)),
                    int(row.get("quantity", 0)),
                ),
                reverse=True,
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
                f"(saldo {balance:.0f}, limite effettivo {budget_limit:.0f}, cap turno {self.config.turn_spend_cap})"
            ),
            event="bid.budget.capped",
            level="WARN",
            original_cost=total_cost,
            scaled_cost=new_total,
            balance=balance,
            scale_factor=scale,
            budget_limit=budget_limit,
            turn_spend_cap=self.config.turn_spend_cap,
        )
        return scaled

    def _set_phase_context(self, phase: str) -> None:
        self.state.current_phase = phase
        set_log_context(turn_id=self.state.current_turn_id, phase=phase)

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
        if trend_plan is not None:
            for row in getattr(trend_plan, "bids", {}).values():
                try:
                    amount = int(row["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                if amount > 0:
                    amounts.append(amount)

        if amounts:
            amounts.sort()
            p25 = amounts[(len(amounts) - 1) // 4]
            p75 = amounts[(3 * (len(amounts) - 1)) // 4]
            # Competitive bias: avoid blindly mirroring inflated trend bids.
            # We scale down trend references to stay broad under turn budget.
            scaled_min = int(round(float(p25) * 0.35))
            scaled_max = int(round(float(p75) * 0.70))
            effective_min = max(floor, min(ceil - 2, max(1, scaled_min)))
            effective_max = min(ceil, max(effective_min + 2, max(2, scaled_max)))
        else:
            fallback_by_direction = {
                TrendDirection.LOW_COST: (2, 8),
                TrendDirection.BALANCED: (3, 10),
                TrendDirection.HIGH_COST: (4, 12),
            }
            fallback_min, fallback_max = fallback_by_direction[trend_direction]
            effective_min = max(floor, int(fallback_min))
            effective_max = min(ceil, max(effective_min + 2, int(fallback_max)))

        if effective_max <= effective_min:
            effective_max = min(ceil, effective_min + 2)
            if effective_max <= effective_min:
                effective_max = effective_min

        log(
            "BID",
            f"Bid bounds dinamici: min={effective_min} max={effective_max}",
            event="bid.bounds.dynamic",
            effective_min=effective_min,
            effective_max=effective_max,
            source="trend_plan" if amounts else "fallback",
            samples=len(amounts),
            trend_direction=trend_direction.value,
            config_floor=floor,
            config_ceil=ceil,
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

        await self._ensure_trend_plan_for_current_turn()
        trend_plan = self.state.next_turn_trend_plan

        # 1. Determine trend direction
        trend_direction = analyze_trend_direction(trend_plan)
        self.state.trend_direction = trend_direction

        # 2. Fetch all recipes
        all_recipes = await self.api_client.get_recipes()
        all_recipes = sorted(all_recipes, key=lambda r: r.get("prestige", 0), reverse=True)
        self.state.all_recipes = all_recipes
        demand_map = await self._load_previous_turn_demand()

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
                if len(selected) >= self.config.max_target_recipes:
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

        # Even when trend recipes are present, expand breadth with demand-driven choices.
        if selected and demand_map and len(selected) < self.config.max_target_recipes:
            selected_names = {self._recipe_key(r.get("name", "")) for r in selected}
            added_from_demand = 0
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
                if len(selected) >= self.config.max_target_recipes:
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
                if len(selected) >= self.config.max_target_recipes:
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

        # Guarantee low/mid/high coverage when possible.
        selected = self._enforce_tier_coverage(selected, all_recipes=all_recipes)
        selected = self._top_up_recipe_pool(
            selected,
            all_recipes=all_recipes,
            trend_direction=trend_direction,
            minimum_size=self.config.max_target_recipes,
        )

        # Respect configured cap.
        self.state.target_recipes = selected[: self.config.max_target_recipes]

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
        self.state.ingredient_bids = bid_map

        # Breadth guardrail: keep line-count within what the budget can sustain.
        max_affordable_lines = max(1, int(self.config.turn_spend_cap // max(1, effective_min_bid)))
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

        await self._ensure_trend_plan_for_current_turn()
        demand_map = await self._load_previous_turn_demand()

        trend_direction = self.state.trend_direction or TrendDirection.BALANCED
        trend_plan = self.state.next_turn_trend_plan
        effective_min_bid = int(self.state.bid_min_effective or self.config.min_ingredient_bid)
        trend_price_by_name: dict[str, int] = {}
        if trend_plan is not None:
            for row in trend_plan.menu:
                try:
                    trend_price_by_name[str(row["name"]).strip()] = int(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue

        spend_cap = int(self.config.turn_spend_cap)
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
        if self.state.target_recipes and remaining_market_budget > 0:
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
            await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": False})
            log(
                "MENU",
                "Nessun piatto fattibile da inventario: ristorante chiuso",
                event="menu.update.empty_and_closed",
                level="WARN",
            )
            return

        self._enforce_price_ladder(menu_items, recipe_meta=recipe_meta)

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
            "Preparazione on-demand attiva: i piatti verranno preparati all'arrivo del cliente",
            event="dish.prepare.mode.on_demand",
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
