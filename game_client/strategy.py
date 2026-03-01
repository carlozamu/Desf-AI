from __future__ import annotations

import difflib
import re
from typing import Any, Awaitable, Callable

from game_client.logging_utils import log

_REQUEST_PATTERN = re.compile(
    r"^\s*i want to eat\s+(.+?)(?:\.\s*i'?m intolerant to\s+(.+?))?\s*$",
    re.IGNORECASE,
)

_INTOLERANCE_SEP = re.compile(r"[,\s]+and\s+|[,\s]+", re.IGNORECASE)
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def normalize_order_text(text: str) -> tuple[str, list[str]]:
    """Return (dish_name, intolerances) parsed from order text."""
    raw = str(text or "").strip()
    if not raw:
        return "", []

    match = _REQUEST_PATTERN.match(raw)
    if match:
        dish = match.group(1).strip().lower()
        intolerance_raw = match.group(2) or ""
        intolerances = [
            t.strip().lower()
            for t in _INTOLERANCE_SEP.split(intolerance_raw)
            if t.strip()
        ]
        return dish, intolerances

    return raw.strip().lower(), []



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
        log(
            "STRATEGY",
            f"Impossibile analizzare /meals turn {previous_turn}: {exc}",
            event="strategy.meals_analysis.failed",
            level="WARN",
            source_turn_id=previous_turn,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
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
    bid_range = max(0, int(max_ingredient_bid) - int(min_ingredient_bid))
    max_pressure = max(1, max(ingredient_presence.values(), default=1))
    max_qty = max(1, max(aggregate_qty.values(), default=1))
    # Keep fallback bids competitive even when trend-plan bids are unavailable.
    # With wide bid ranges (e.g. max=200), avoid staying near the minimum.
    base_ratio = 0.60 if bid_range >= 40 else 0.35

    for ingredient, qty in sorted(aggregate_qty.items()):
        pressure = ingredient_presence.get(ingredient, 1)
        pressure_score = (pressure - 1) / max(1, max_pressure - 1)
        qty_score = (int(qty) - 1) / max(1, max_qty - 1)
        urgency = (pressure_score * 0.55) + (qty_score * 0.45)
        target_ratio = max(base_ratio, min(1.0, urgency))
        bid_value = int(min_ingredient_bid) + int(round(bid_range * target_ratio))
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



def _dish_has_intolerance_conflict(
    dish_name: str,
    intolerances: list[str],
    recipes: list[dict[str, Any]],
) -> bool:
    """Return True if any ingredient of dish_name matches a client intolerance."""
    if not intolerances:
        return False
    dish_lower = dish_name.lower()
    for recipe in recipes:
        if str(recipe.get("name", "")).strip().lower() != dish_lower:
            continue
        ingredients = recipe_ingredients(recipe)
        for ing in ingredients:
            ing_lower = ing.lower()
            for intol in intolerances:
                if intol and (intol in ing_lower or ing_lower in intol):
                    return True
    return False


def _tokenize_for_match(text: str) -> set[str]:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return set()
    return {t for t in _TOKEN_SPLIT_RE.split(normalized) if t}


def _dish_match_score(order_query: str, dish_name: str) -> float:
    order_norm = str(order_query or "").strip().lower()
    dish_norm = str(dish_name or "").strip().lower()
    if not order_norm or not dish_norm:
        return 0.0

    if dish_norm in order_norm or order_norm in dish_norm:
        return 1.0

    order_tokens = _tokenize_for_match(order_norm)
    dish_tokens = _tokenize_for_match(dish_norm)
    if not order_tokens or not dish_tokens:
        return 0.0

    overlap = len(order_tokens & dish_tokens) / max(1, len(dish_tokens))
    ratio = difflib.SequenceMatcher(None, order_norm, dish_norm).ratio()
    return max(overlap, (overlap * 0.6) + (ratio * 0.4))


_INGREDIENT_ORDER_RE = re.compile(
    r"i want something with\s+(.+)",
    re.IGNORECASE,
)


def _parse_ingredient_list(text: str) -> list[str]:
    """Extract ingredient names from 'I want something with X, Y, and Z'."""
    m = _INGREDIENT_ORDER_RE.search(text)
    if not m:
        return []
    raw = m.group(1).strip().rstrip(".")
    # Split on ", and " / ", " / " and "
    parts = re.split(r",\s*and\s+|,\s*|\s+and\s+", raw, flags=re.IGNORECASE)
    return [p.strip().lower() for p in parts if p.strip()]


def _ingredient_match_score(
    requested_ingredients: list[str],
    dish_name: str,
    recipes: list[dict[str, Any]],
) -> float:
    """Score a dish by how many of the requested ingredients it contains."""
    dish_lower = dish_name.lower()
    for recipe in recipes:
        if str(recipe.get("name", "")).strip().lower() != dish_lower:
            continue
        recipe_ings = {ing.lower() for ing in recipe_ingredients(recipe)}
        if not recipe_ings:
            return 0.0
        requested_set = set(requested_ingredients)
        overlap = len(requested_set & recipe_ings)
        if overlap == 0:
            return 0.0
        # Score: fraction of recipe ingredients that are requested
        return overlap / len(recipe_ings)
    return 0.0


def pick_dish_for_order(
    order_text: str,
    active_menu: list[dict[str, Any]],
    recipes: list[dict[str, Any]] | None = None,
) -> tuple[str | None, list[str]]:
    """Return (dish_name, intolerances).

    dish_name is None if no matching dish found or all matches conflict with
    the client's intolerances (in which case caller should log the conflict).
    """
    dish_query, intolerances = normalize_order_text(order_text)
    if not dish_query:
        return None, intolerances

    all_recipes = recipes or []
    ranked: list[tuple[float, str]] = []

    # Try ingredient-based matching first (for "I want something with X, Y, Z" orders)
    requested_ingredients = _parse_ingredient_list(order_text)
    if requested_ingredients and all_recipes:
        for item in active_menu:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue
            score = _ingredient_match_score(requested_ingredients, dish_name, all_recipes)
            if score > 0:
                ranked.append((score, dish_name))

    # Fallback to dish-name matching
    if not ranked:
        for item in active_menu:
            dish_name = str(item.get("name", "")).strip()
            if not dish_name:
                continue
            score = _dish_match_score(dish_query, dish_name)
            if score <= 0:
                continue
            ranked.append((score, dish_name))

    if not ranked:
        return None, intolerances

    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score = ranked[0][0]
    # Require a minimum confidence unless there is a very clear best match.
    if best_score < 0.20:
        return None, intolerances

    for _, dish_name in ranked:
        if _dish_has_intolerance_conflict(dish_name, intolerances, all_recipes):
            continue
        return dish_name, intolerances

    return None, intolerances


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

    target_order, _ = normalize_order_text(order_text)
    target_name = str(client_name or "").strip().lower()

    for meal in meals:
        if meal.get("executed") is True:
            continue

        req, _ = normalize_order_text(str(meal.get("request", "")))
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
