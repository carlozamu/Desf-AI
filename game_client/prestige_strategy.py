from __future__ import annotations

import random
from enum import Enum
from typing import Any

from game_client.logging_utils import log
from game_client.strategy import build_low_cost_bids, recipe_ingredients

# ---------------------------------------------------------------------------
# Prestige tier thresholds
# ---------------------------------------------------------------------------
_LOW_MAX = 40
_MID_MAX = 74


def _prestige_tier(prestige: int) -> str:
    if prestige <= _LOW_MAX:
        return "low"
    if prestige <= _MID_MAX:
        return "mid"
    return "high"


# ---------------------------------------------------------------------------
# TrendDirection
# ---------------------------------------------------------------------------


class TrendDirection(str, Enum):
    HIGH_COST = "HIGH_COST"
    LOW_COST = "LOW_COST"
    BALANCED = "BALANCED"


# ---------------------------------------------------------------------------
# Multiplier matrix [tier][direction]
# ---------------------------------------------------------------------------
_MULTIPLIERS: dict[str, dict[TrendDirection, float]] = {
    "low": {
        TrendDirection.LOW_COST: 1.5,
        TrendDirection.BALANCED: 2.0,
        TrendDirection.HIGH_COST: 2.5,
    },
    "mid": {
        TrendDirection.LOW_COST: 2.5,
        TrendDirection.BALANCED: 3.0,
        TrendDirection.HIGH_COST: 4.0,
    },
    "high": {
        TrendDirection.LOW_COST: 4.0,
        TrendDirection.BALANCED: 4.5,
        TrendDirection.HIGH_COST: 6.0,
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_trend_direction(trend_plan: Any | None) -> TrendDirection:
    """Derive market direction from trend plan menu prices.

    Returns HIGH_COST if avg price > 150, LOW_COST if < 80, else BALANCED.
    Falls back to BALANCED when trend_plan is None or has no menu.
    """
    if trend_plan is None:
        log(
            "TREND",
            "Nessun piano trend disponibile → BALANCED",
            event="trend.direction.analyzed",
            direction=TrendDirection.BALANCED.value,
            avg_price=None,
        )
        return TrendDirection.BALANCED

    menu = getattr(trend_plan, "menu", None) or []
    prices = []
    for item in menu:
        try:
            prices.append(int(item["price"]))
        except (KeyError, TypeError, ValueError):
            continue

    if not prices:
        log(
            "TREND",
            "Nessun piano trend disponibile → BALANCED",
            event="trend.direction.analyzed",
            direction=TrendDirection.BALANCED.value,
            avg_price=None,
        )
        return TrendDirection.BALANCED

    avg_price = sum(prices) / len(prices)
    if avg_price > 150:
        direction = TrendDirection.HIGH_COST
    elif avg_price < 80:
        direction = TrendDirection.LOW_COST
    else:
        direction = TrendDirection.BALANCED

    log(
        "TREND",
        f"Mercato {direction.value} (prezzo medio: {avg_price:.0f}cr su {len(prices)} piatti)",
        event="trend.direction.analyzed",
        direction=direction.value,
        avg_price=round(avg_price, 1),
        menu_size=len(prices),
    )
    return direction


def select_prestige_balanced_recipes(
    all_recipes: list[dict[str, Any]],
    trend_direction: TrendDirection,
    max_per_tier: int = 1,
) -> list[dict[str, Any]]:
    """Select up to 3 recipes, one per prestige tier (low / mid / high).

    When a tier is empty, the function borrows from an adjacent tier so the
    result always contains as many recipes as possible (up to 3).

    The tier that is "dominant" for the given trend direction is picked first:
    - HIGH_COST → high first
    - LOW_COST  → low first
    - BALANCED  → mid first
    """
    tiers: dict[str, list[dict[str, Any]]] = {"low": [], "mid": [], "high": []}
    for recipe in all_recipes:
        try:
            prestige = int(recipe.get("prestige", 50))
        except (TypeError, ValueError):
            prestige = 50
        tiers[_prestige_tier(prestige)].append(recipe)

    # Shuffle each tier so the selection is not always the same recipe
    for bucket in tiers.values():
        random.shuffle(bucket)

    # Dominant tier order by direction
    if trend_direction == TrendDirection.HIGH_COST:
        order = ["high", "mid", "low"]
    elif trend_direction == TrendDirection.LOW_COST:
        order = ["low", "mid", "high"]
    else:
        order = ["mid", "low", "high"]

    chosen: list[dict[str, Any]] = []
    used_recipes: set[str] = set()

    def _pick_from_tier(tier: str) -> dict[str, Any] | None:
        for recipe in tiers[tier]:
            name = str(recipe.get("name", ""))
            if name not in used_recipes:
                used_recipes.add(name)
                return recipe
        return None

    # First pass: one from each tier in priority order
    for tier in order:
        recipe = _pick_from_tier(tier)
        if recipe is not None:
            chosen.append(recipe)

    # Second pass: if we still have fewer than 3, fill from any available tier
    if len(chosen) < 3:
        for tier in order:
            if len(chosen) >= 3:
                break
            recipe = _pick_from_tier(tier)
            if recipe is not None:
                chosen.append(recipe)

    selected_info = [
        {
            "name": r.get("name"),
            "tier": _prestige_tier(int(r.get("prestige", 50))),
            "prestige": r.get("prestige"),
        }
        for r in chosen
    ]
    recipes_str = " | ".join(
        f"{s['name']} ({s['tier']}·{s['prestige']})" for s in selected_info
    )
    log(
        "BID",
        f"Ricette: {recipes_str}",
        event="bid.prestige.selected",
        direction=trend_direction.value,
        selected=selected_info,
    )
    return chosen


def compute_trend_adjusted_price(
    ingredient_cost: int,
    prestige: int,
    trend_direction: TrendDirection,
    dish_name: str = "",
) -> int:
    """Compute menu price using tier × trend multiplier, clamped to [10, 999]."""
    tier = _prestige_tier(int(prestige))
    multiplier = _MULTIPLIERS[tier][trend_direction]
    raw_price = int(round(ingredient_cost * multiplier))
    final_price = max(10, min(999, raw_price))

    name_part = f"{dish_name}: " if dish_name else ""
    log(
        "MENU-PRICE",
        f"  {name_part}costo {ingredient_cost}cr × {multiplier} ({tier}/{trend_direction.value}) = {final_price}cr",
        event="menu.price.calculated",
        tier=tier,
        trend_direction=trend_direction.value,
        ingredient_cost=ingredient_cost,
        multiplier=multiplier,
        final_price=final_price,
        dish_name=dish_name or None,
    )
    return final_price


def build_prestige_bids(
    target_recipes: list[dict[str, Any]],
    ref_bid_amounts: dict[str, int],
    min_ingredient_bid: int,
    max_ingredient_bid: int,
    portions_per_recipe: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build closed-bid payload for ingredients of target_recipes.

    For each ingredient uses the reference amount from trend_plan.bids when
    available; otherwise falls back to the build_low_cost_bids logic.
    """
    if not target_recipes:
        return [], {}

    # Use build_low_cost_bids to get base quantities and fallback bids
    fallback_bids, fallback_bid_map = build_low_cost_bids(
        target_recipes,
        min_ingredient_bid=min_ingredient_bid,
        max_ingredient_bid=max_ingredient_bid,
        portions_per_recipe=portions_per_recipe,
    )

    # Build aggregate quantity map
    aggregate_qty: dict[str, int] = {}
    for recipe in target_recipes:
        for ingredient, qty in recipe_ingredients(recipe).items():
            aggregate_qty[ingredient] = aggregate_qty.get(ingredient, 0) + qty * portions_per_recipe

    bids: list[dict[str, Any]] = []
    bid_map: dict[str, int] = {}

    for ingredient, qty in sorted(aggregate_qty.items()):
        if ingredient in ref_bid_amounts:
            raw_amount = int(ref_bid_amounts[ingredient])
            bid_value = max(min_ingredient_bid, min(max_ingredient_bid, raw_amount))
        else:
            bid_value = fallback_bid_map.get(ingredient, min_ingredient_bid)

        bids.append(
            {
                "ingredient": ingredient,
                "bid": bid_value,
                "quantity": max(1, qty),
            }
        )
        bid_map[ingredient] = bid_value

    return bids, bid_map
