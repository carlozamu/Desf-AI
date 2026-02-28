# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like"
# ]
# ///

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp
from dotenv import load_dotenv

from datapizza.tools.mcp_client import MCPClient
from serving.meals_tracker import collect_turn_meals, init_firebase, save_to_firebase as save_meals_to_firebase, summarize_unserved_requests
from serving.menu_tracker import collect_all_menus, get_lowest_competitor_price, save_to_firebase as save_menus_to_firebase

load_dotenv()

TEAM_ID = os.getenv("TEAM_ID", "")
TEAM_API_KEY = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", ""))
BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
RUN_ID = os.getenv("RUN_ID", "main")

if not TEAM_ID or not TEAM_API_KEY:
    raise SystemExit("Set TEAM_ID and SERVER_API_KEY (or TEAM_API_KEY) in .env")

mcp_client = MCPClient(
    url=f"{BASE_URL}/mcp",
    headers={"x-api-key": TEAM_API_KEY},
)

CURRENT_TURN_ID: int = 0
TARGET_RECIPES: list[dict[str, Any]] = []
ACTIVE_MENU: list[dict[str, Any]] = []
DISHES_READY: dict[str, bool] = {}
INGREDIENT_BIDS: dict[str, int] = {}

MIN_INGREDIENT_BID = 20
MAX_INGREDIENT_BID = 30
PORTIONS_PER_RECIPE = 2
MAX_TARGET_RECIPES = 3

PRICING_MODE = os.getenv("PRICING_MODE", "").strip().lower()
PRICING_TEST_TURN_RAW = os.getenv("PRICING_TEST_TURN", "").strip()
SSE_RETRY_DELAY = int(os.getenv("SSE_RETRY_DELAY", "3"))
SSE_CONFLICT_RETRY_DELAY = int(os.getenv("SSE_CONFLICT_RETRY_DELAY", "6"))

try:
    PRICING_TEST_TURN = int(PRICING_TEST_TURN_RAW) if PRICING_TEST_TURN_RAW else -1
except ValueError:
    PRICING_TEST_TURN = -1


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {datetime.now().isoformat(timespec='seconds')}: {message}")


def _normalize_order_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""

    pattern = re.compile(r"^\s*i want to eat\s+(.+?)(?:\.\s*i'?m intolerant to.+)?\s*$", re.IGNORECASE)
    match = pattern.match(raw)
    if match:
        return match.group(1).strip().lower()
    return raw


async def api_get(endpoint: str, *, params: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{endpoint}"
    headers = {"x-api-key": TEAM_API_KEY}
    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                log("HTTP", f"GET {endpoint} -> {response.status} {text[:180]}")
                return None
    except aiohttp.ClientError as exc:
        log("HTTP", f"GET {endpoint} failed: {exc}")
        return None


async def get_recipes() -> list[dict[str, Any]]:
    data = await api_get("/recipes")
    return data if isinstance(data, list) else []


async def get_my_meals(turn_id: int) -> list[dict[str, Any]]:
    data = await api_get(
        "/meals",
        params={"turn_id": turn_id, "restaurant_id": TEAM_ID},
    )
    return data if isinstance(data, list) else []


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


async def choose_target_recipes_for_bidding(turn_id: int) -> list[dict[str, Any]]:
    recipes = await get_recipes()
    if not recipes:
        return []

    recipes_by_name = {
        str(recipe.get("name", "")).strip().lower(): recipe
        for recipe in recipes
        if recipe.get("name")
    }

    if turn_id <= 1:
        return pick_fallback_recipes(recipes, MAX_TARGET_RECIPES)

    previous_turn = turn_id - 1
    try:
        meals_data = await collect_turn_meals(previous_turn)
        ranking = summarize_unserved_requests(meals_data)
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
        if len(chosen) >= MAX_TARGET_RECIPES:
            break

    if chosen:
        return chosen

    return pick_fallback_recipes(recipes, MAX_TARGET_RECIPES)


def build_low_cost_bids(target_recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global INGREDIENT_BIDS

    aggregate_qty: dict[str, int] = {}
    ingredient_presence: dict[str, int] = {}

    for recipe in target_recipes:
        ingredients = recipe_ingredients(recipe)
        for ingredient, qty in ingredients.items():
            aggregate_qty[ingredient] = aggregate_qty.get(ingredient, 0) + qty * PORTIONS_PER_RECIPE
            ingredient_presence[ingredient] = ingredient_presence.get(ingredient, 0) + 1

    bids: list[dict[str, Any]] = []
    bid_map: dict[str, int] = {}

    for ingredient, qty in sorted(aggregate_qty.items()):
        pressure = ingredient_presence.get(ingredient, 1)
        bid_value = MIN_INGREDIENT_BID + min(10, max(0, (pressure - 1) * 5))
        bid_value = max(MIN_INGREDIENT_BID, min(MAX_INGREDIENT_BID, bid_value))

        bids.append(
            {
                "ingredient": ingredient,
                "bid": int(bid_value),
                "quantity": int(max(1, qty)),
            }
        )
        bid_map[ingredient] = int(bid_value)

    INGREDIENT_BIDS = bid_map
    return bids


def estimate_recipe_cost(recipe: dict[str, Any]) -> int:
    cost = 0
    for ingredient, qty in recipe_ingredients(recipe).items():
        bid = INGREDIENT_BIDS.get(ingredient, MIN_INGREDIENT_BID)
        cost += bid * qty
    return max(cost, MIN_INGREDIENT_BID)


async def call_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    try:
        return await mcp_client.call_tool(tool_name, arguments)
    except Exception as exc:
        log("MCP", f"{tool_name} failed: {exc}")
        return None


async def game_started(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID, TARGET_RECIPES, ACTIVE_MENU, DISHES_READY, INGREDIENT_BIDS

    CURRENT_TURN_ID = int(data.get("turn_id", CURRENT_TURN_ID or 0) or 0)
    TARGET_RECIPES = []
    ACTIVE_MENU = []
    DISHES_READY = {}
    INGREDIENT_BIDS = {}

    log("EVENT", f"game_started turn={CURRENT_TURN_ID}")
    await call_mcp("update_restaurant_is_open", {"is_open": True})


async def speaking_phase_started() -> None:
    log("PHASE", "speaking")


async def closed_bid_phase_started(data: dict[str, Any] | None = None) -> None:
    global CURRENT_TURN_ID, TARGET_RECIPES

    if data and data.get("turn_id") is not None:
        CURRENT_TURN_ID = int(data["turn_id"])

    log("PHASE", f"closed_bid turn={CURRENT_TURN_ID}")

    TARGET_RECIPES = await choose_target_recipes_for_bidding(CURRENT_TURN_ID)
    if not TARGET_RECIPES:
        log("BID", "Nessuna ricetta target trovata")
        return

    bids = build_low_cost_bids(TARGET_RECIPES)
    if not bids:
        log("BID", "Nessun bid da inviare")
        return

    recipe_names = [str(r.get("name")) for r in TARGET_RECIPES]
    log("BID", f"Target ricette: {recipe_names}")
    log("BID", f"Invio {len(bids)} bids (range {MIN_INGREDIENT_BID}-{MAX_INGREDIENT_BID})")
    await call_mcp("closed_bid", {"bids": bids})


async def waiting_phase_started() -> None:
    global ACTIVE_MENU

    log("PHASE", "waiting")

    if not TARGET_RECIPES:
        log("MENU", "TARGET_RECIPES vuoto, menu non aggiornato")
        return

    is_cost_1x_test = (
        PRICING_MODE == "cost_1x_test"
        and PRICING_TEST_TURN > 0
        and CURRENT_TURN_ID == PRICING_TEST_TURN
    )
    if is_cost_1x_test:
        log("MENU", f"Pricing mode attivo: cost_1x_test (turn={CURRENT_TURN_ID})")

    menus_data = await collect_all_menus() if not is_cost_1x_test else {}

    menu_items: list[dict[str, Any]] = []
    for recipe in TARGET_RECIPES:
        dish_name = str(recipe.get("name", "")).strip()
        if not dish_name:
            continue

        ingredient_cost = estimate_recipe_cost(recipe)
        if is_cost_1x_test:
            final_price = max(1, int(ingredient_cost))
            pricing_mode_applied = "cost_1x_test"
        else:
            base_price = max(50, int(ingredient_cost * 1.8))
            competitor_min = get_lowest_competitor_price(
                menus_data,
                dish_name,
                my_restaurant_id=TEAM_ID,
            )
            if competitor_min is not None:
                competitive_price = max(40, int(competitor_min) - 1)
                final_price = max(base_price, competitive_price)
            else:
                final_price = base_price
            final_price = max(1, min(999, final_price))
            pricing_mode_applied = "default_competitive"

        menu_items.append({"name": dish_name, "price": final_price})
        log(
            "MENU-PRICE",
            f"dish={dish_name} cost={ingredient_cost} price={final_price} mode={pricing_mode_applied}",
        )

    if not menu_items:
        log("MENU", "Nessuna voce menu da salvare")
        return

    ACTIVE_MENU = menu_items
    await call_mcp("save_menu", {"items": menu_items})
    log("MENU", f"Menu aggiornato: {ACTIVE_MENU}")


async def serving_phase_started() -> None:
    global DISHES_READY

    log("PHASE", "serving")

    if not ACTIVE_MENU:
        log("SERVE", "Menu vuoto, nessuna preparazione")
        return

    for item in ACTIVE_MENU:
        dish_name = str(item.get("name", "")).strip()
        if not dish_name:
            continue

        DISHES_READY[dish_name] = False
        await call_mcp("prepare_dish", {"dish_name": dish_name})
        await asyncio.sleep(0.2)


def _get_client_id_from_event(data: dict[str, Any]) -> str | None:
    for key in ("clientId", "customerId", "client_id", "id"):
        value = data.get(key)
        if value is not None:
            return str(value)
    return None


def _pick_dish_for_order(order_text: str) -> str | None:
    normalized = _normalize_order_text(order_text)
    if not normalized:
        return None

    for item in ACTIVE_MENU:
        dish_name = str(item.get("name", "")).strip()
        if not dish_name:
            continue
        if dish_name.lower() in normalized:
            return dish_name

    return None


async def resolve_client_id_from_meals(order_text: str, client_name: str | None) -> str | None:
    if CURRENT_TURN_ID <= 0:
        return None

    meals = await get_my_meals(CURRENT_TURN_ID)
    if not meals:
        return None

    target_order = _normalize_order_text(order_text)
    target_name = str(client_name or "").strip().lower()

    for meal in meals:
        if meal.get("executed") is True:
            continue

        req = _normalize_order_text(str(meal.get("request", "")))
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


async def client_spawned(data: dict[str, Any]) -> None:
    client_name = str(data.get("clientName", data.get("name", "")))
    order_text = str(data.get("orderText", data.get("order", "")))

    dish_name = _pick_dish_for_order(order_text)
    if not dish_name:
        return

    if DISHES_READY.get(dish_name) is not True:
        return

    client_id = _get_client_id_from_event(data)
    if not client_id:
        client_id = await resolve_client_id_from_meals(order_text, client_name)

    if not client_id:
        log("SERVE", f"Nessun client_id trovato per ordine '{order_text}'")
        return

    await call_mcp("serve_dish", {"dish_name": dish_name, "client_id": client_id})
    DISHES_READY[dish_name] = False

    # Rimettiamo in preparazione il piatto servito per rifornire la linea.
    await call_mcp("prepare_dish", {"dish_name": dish_name})


async def preparation_complete(data: dict[str, Any]) -> None:
    dish_name = str(data.get("dish", "")).strip()
    if not dish_name:
        return

    DISHES_READY[dish_name] = True


async def message(data: dict[str, Any]) -> None:
    sender = data.get("sender")
    payload = data.get("payload")
    log("MSG", f"from={sender} payload={payload}")


async def new_message(data: dict[str, Any]) -> None:
    sender_name = data.get("senderName", data.get("senderId", "unknown"))
    text = data.get("text", "")
    log("DM", f"from={sender_name}: {text}")


async def end_turn() -> None:
    log("PHASE", f"stopped turn={CURRENT_TURN_ID}")

    if CURRENT_TURN_ID <= 0:
        return

    db = init_firebase()
    if db is None:
        log("TRACK", "Firebase non disponibile, skip salvataggio tracker")
        return

    try:
        meals_data, menus_data = await asyncio.gather(
            collect_turn_meals(CURRENT_TURN_ID),
            collect_all_menus(),
        )

        tasks: list[Awaitable[Any]] = []
        if meals_data:
            tasks.append(
                asyncio.to_thread(
                    save_meals_to_firebase,
                    db,
                    CURRENT_TURN_ID,
                    RUN_ID,
                    meals_data,
                )
            )
        if menus_data:
            tasks.append(
                asyncio.to_thread(
                    save_menus_to_firebase,
                    db,
                    CURRENT_TURN_ID,
                    RUN_ID,
                    menus_data,
                )
            )

        if tasks:
            await asyncio.gather(*tasks)
    except Exception as exc:
        log("TRACK", f"Errore salvataggio tracker: {exc}")


async def game_phase_changed(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID

    if data.get("turn_id") is not None:
        CURRENT_TURN_ID = int(data["turn_id"])

    phase = data.get("phase", "unknown")
    handlers: dict[str, Callable[[], Awaitable[None]]] = {
        "speaking": speaking_phase_started,
        "closed_bid": lambda: closed_bid_phase_started(data),
        "waiting": waiting_phase_started,
        "serving": serving_phase_started,
        "stopped": end_turn,
    }

    handler = handlers.get(phase)
    if handler:
        await handler()
    else:
        log("PHASE", f"fase non gestita: {phase}")


async def game_reset(data: dict[str, Any]) -> None:
    log("EVENT", f"game_reset {data}")


EVENT_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "game_started": game_started,
    "game_phase_changed": game_phase_changed,
    "game_reset": game_reset,
    "client_spawned": client_spawned,
    "preparation_complete": preparation_complete,
    "message": message,
    "new_message": new_message,
}


async def dispatch_event(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        return

    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler failed for {event_type}: {exc}")


async def handle_line(raw_line: bytes) -> None:
    if not raw_line:
        return

    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return

    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})

    if isinstance(event_data, dict):
        await dispatch_event(event_type, event_data)
    else:
        await dispatch_event(event_type, {"value": event_data})


async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open")
        async for line in response.content:
            await handle_line(line)


async def listen_once_and_exit_on_drop() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                await listen_once(session)
                log("SSE", f"connection closed, retry in {SSE_RETRY_DELAY}s")
                await asyncio.sleep(SSE_RETRY_DELAY)
            except aiohttp.ClientResponseError as exc:
                if exc.status == 409:
                    log(
                        "SSE",
                        (
                            f"409 Conflict: esiste gia una connessione attiva per team {TEAM_ID}. "
                            f"Retry in {SSE_CONFLICT_RETRY_DELAY}s"
                        ),
                    )
                    await asyncio.sleep(SSE_CONFLICT_RETRY_DELAY)
                    continue
                if exc.status in (401, 403):
                    raise SystemExit(f"SSE auth error ({exc.status}). Verifica TEAM_ID/API key.")
                log("SSE", f"HTTP error {exc.status}, retry in {SSE_RETRY_DELAY}s")
                await asyncio.sleep(SSE_RETRY_DELAY)
            except aiohttp.ClientError as exc:
                log("SSE", f"connection error: {exc}; retry in {SSE_RETRY_DELAY}s")
                await asyncio.sleep(SSE_RETRY_DELAY)


async def main() -> None:
    log("INIT", f"team={TEAM_ID} base_url={BASE_URL}")
    await listen_once_and_exit_on_drop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "client stopped")
