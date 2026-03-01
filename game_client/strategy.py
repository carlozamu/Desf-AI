from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from game_client.logging_utils import log

_REQUEST_PATTERN = re.compile(
    r"^\s*i want to eat\s+(.+?)(?:\.\s*i'?m intolerant to.+)?\s*$",
    re.IGNORECASE,
)



def normalize_order_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""

    match = _REQUEST_PATTERN.match(raw)
    if match:
        return match.group(1).strip().lower()

    return raw



def recipe_ingredients(recipe: dict[str, Any]) -> dict[str, int]:
    raw = recipe.get("ingredients", {})

    if isinstance(raw, dict):
        ingredients: dict[str, int] = {}
        for name, qty in raw.items():
            if not name:
                continue
            if isinstance(qty, (int, float)):
                ingredients[str(name)] = max(1, int(qty))
            else:
                ingredients[str(name)] = 1
        return ingredients

    if isinstance(raw, list):
        out: dict[str, int] = {}
        for name in raw:
            if name:
                out[str(name)] = out.get(str(name), 0) + 1
        return out

    return {}



def pick_fallback_recipes(recipes: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    sortable = []
    for recipe in recipes:
        ingredients = recipe_ingredients(recipe)
        if not ingredients:
            continue
        sortable.append((sum(ingredients.values()), len(ingredients), recipe.get("name", ""), recipe))

    sortable.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in sortable[:limit]]


async def choose_target_recipes_for_bidding(
    turn_id: int,
    *,
    get_recipes_fn: Callable[[], Awaitable[list[dict[str, Any]]]],
    collect_turn_meals_fn: Callable[[int], Awaitable[dict[str, Any]]],
    summarize_unserved_requests_fn: Callable[[dict[str, Any]], list[dict[str, Any]]],
    max_target_recipes: int,
) -> list[dict[str, Any]]:
    recipes = await get_recipes_fn()
    if not recipes:
        return []

    recipes_by_name = {
        str(recipe.get("name", "")).strip().lower(): recipe
        for recipe in recipes
        if recipe.get("name")
    }

    if turn_id <= 1:
        return pick_fallback_recipes(recipes, max_target_recipes)

    previous_turn = turn_id - 1
    try:
        meals_data = await collect_turn_meals_fn(previous_turn)
        ranking = summarize_unserved_requests_fn(meals_data)
    except Exception as exc:
        log("STRATEGY", f"Impossibile analizzare /meals turn {previous_turn}: {exc}")
        ranking = []

    chosen: list[dict[str, Any]] = []
    for row in ranking:
        if int(row.get("unserved_requests", 0)) <= 0:
            continue

        recipe_name = str(row.get("recipe_name", "")).strip().lower()
        candidate = recipes_by_name.get(recipe_name)
        if not candidate:
            continue

        chosen.append(candidate)
        if len(chosen) >= max_target_recipes:
            break

    if chosen:
        return chosen

    return pick_fallback_recipes(recipes, max_target_recipes)



def build_low_cost_bids(
    target_recipes: list[dict[str, Any]],
    *,
    min_ingredient_bid: int,
    max_ingredient_bid: int,
    portions_per_recipe: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    aggregate_qty: dict[str, int] = {}
    ingredient_presence: dict[str, int] = {}

    for recipe in target_recipes:
        ingredients = recipe_ingredients(recipe)
        for ingredient, qty in ingredients.items():
            aggregate_qty[ingredient] = aggregate_qty.get(ingredient, 0) + qty * portions_per_recipe
            ingredient_presence[ingredient] = ingredient_presence.get(ingredient, 0) + 1

    bids: list[dict[str, Any]] = []
    bid_map: dict[str, int] = {}

    for ingredient, qty in sorted(aggregate_qty.items()):
        pressure = ingredient_presence.get(ingredient, 1)
        bid_value = min_ingredient_bid + min(10, max(0, (pressure - 1) * 5))
        bid_value = max(min_ingredient_bid, min(max_ingredient_bid, bid_value))

        bids.append(
            {
                "ingredient": ingredient,
                "bid": int(bid_value),
                "quantity": int(max(1, qty)),
            }
        )
        bid_map[ingredient] = int(bid_value)

    return bids, bid_map



def estimate_recipe_cost(
    recipe: dict[str, Any],
    ingredient_bids: dict[str, int],
    *,
    min_ingredient_bid: int,
) -> int:
    cost = 0
    for ingredient, qty in recipe_ingredients(recipe).items():
        bid = ingredient_bids.get(ingredient, min_ingredient_bid)
        cost += bid * qty
    return max(cost, min_ingredient_bid)



def get_client_id_from_event(data: dict[str, Any]) -> str | None:
    for key in ("clientId", "customerId", "client_id", "id"):
        value = data.get(key)
        if value is not None:
            return str(value)
    return None



def pick_dish_for_order(order_text: str, active_menu: list[dict[str, Any]]) -> str | None:
    normalized = normalize_order_text(order_text)
    if not normalized:
        return None

    for item in active_menu:
        dish_name = str(item.get("name", "")).strip()
        if not dish_name:
            continue
        if dish_name.lower() in normalized:
            return dish_name

    return None


async def resolve_client_id_from_meals(
    order_text: str,
    client_name: str | None,
    *,
    current_turn_id: int,
    get_my_meals_fn: Callable[[int], Awaitable[list[dict[str, Any]]]],
) -> str | None:
    if current_turn_id <= 0:
        return None

    meals = await get_my_meals_fn(current_turn_id)
    if not meals:
        return None

    target_order = normalize_order_text(order_text)
    target_name = str(client_name or "").strip().lower()

    for meal in meals:
        if meal.get("executed") is True:
            continue

        req = normalize_order_text(str(meal.get("request", "")))
        meal_customer = meal.get("customer", {}) if isinstance(meal.get("customer"), dict) else {}
        meal_name = str(meal_customer.get("name", "")).strip().lower()

        order_match = bool(target_order and target_order in req)
        name_match = bool(target_name and target_name == meal_name)
        if not (order_match or name_match):
            continue

        for key in ("customerId", "customer_id", "clientId", "id"):
            value = meal.get(key)
            if value is not None:
                return str(value)

    return None
