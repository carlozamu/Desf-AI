# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like"
# ]
# ///

import asyncio
import json
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp



from price_analyst.agent import get_bidding_prices
from price_analyst.bid_tracker import fetch_and_save_bid_history
from bidder.bidder import calculate_bids # Solo l'utility per generare l'array di bids
from serving.meals_tracker import collect_turn_meals, save_to_firebase, init_firebase
from serving.menu_tracker import collect_all_menus, save_to_firebase as save_menus_to_firebase

from datapizza.tools.mcp_client import MCPClient
import os
from dotenv import load_dotenv

load_dotenv()

TEAM_ID = os.getenv("TEAM_ID")
TEAM_API_KEY = os.getenv("SERVER_API_KEY")
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# Etichetta della run corrente — modifica a ogni run ufficiale
RUN_ID = os.getenv("RUN_ID", "main")

BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")

if not TEAM_API_KEY or not TEAM_ID:
    raise SystemExit("Set TEAM_API_KEY and TEAM_ID environment variables")

# GLOBALE VARIAVLE TO STORE the TURNEMENT
CURRENT_TURN_ID = None

# INIZIALIZZIAMO MCP CLIENT
mcp_client = MCPClient(
    url=f"{BASE_URL}/mcp",
    headers={"x-api-key": TEAM_API_KEY}
)


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {datetime.now()}: {message}")


BID_LOG_FILE = Path(__file__).parent / "bids_log.jsonl"

# ──────────────────────────────────────────────────────────────────────────────
# FORCED RECIPE — imposta None per usare il menu optimizer automatico
# Ricetta economica: ingredienti poco contesi (~50-70cr totali)
# ──────────────────────────────────────────────────────────────────────────────
FORCED_RECIPE: dict | None = None  # OR-Tools planner sceglierà la migliore ricetta

def log_bids_to_file(turn_id: int, recipe_name: str, bids: list) -> None:
    """Appende il bid round a bids_log.jsonl per tenere storico delle nostre offerte."""
    record = {
        "ts": datetime.now().isoformat(),
        "turn_id": turn_id,
        "recipe": recipe_name,
        "bids": bids,
        "total_estimated_cost": sum(b["bid"] * b.get("quantity", 1) for b in bids)
    }
    try:
        with BID_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log("BID-LOG", f"Appended {len(bids)} bids to {BID_LOG_FILE.name}")
    except Exception as e:
        log("ERROR", f"Failed to write bid log: {e}")




def get_ingredient_prices_from_history() -> dict:
    """Legge bids_log.jsonl e restituisce il prezzo medio bid per ingrediente (ultime N run)."""
    prices: dict = {}
    if not BID_LOG_FILE.exists():
        return {}
    try:
        lines = [l for l in BID_LOG_FILE.read_text(encoding='utf-8').splitlines() if l.strip()]
        for line in lines[-5:]:
            record = json.loads(line)
            if record.get('recipe') == 'fallback':
                continue
            for bid in record.get('bids', []):
                ing   = bid['ingredient']
                price = bid['bid'] * bid.get('quantity', 1)
                prices.setdefault(ing, []).append(price)
    except Exception as e:
        log('ERROR', f'get_ingredient_prices_from_history: {e}')
    return {ing: sum(v) / len(v) for ing, v in prices.items()}


MAX_MENU_PRICE = 999
DEFAULT_ING_PRICE = 65

def estimate_recipe_cost(recipe: dict, price_history: dict) -> float:
    """Stima il costo totale degli ingredienti di una ricetta dai prezzi storici."""
    raw_ings = recipe.get('ingredients', {})
    ings = list(raw_ings.keys()) if isinstance(raw_ings, dict) else list(raw_ings)
    return sum(price_history.get(ing, DEFAULT_ING_PRICE) for ing in ings)


async def _run_planner() -> None:
    """OR-Tools su ricette fattibili (costo*1.1 <= 999) — aggiorna TARGET_RECIPE ogni turno."""
    global TARGET_RECIPE
    try:
        price_history = get_ingredient_prices_from_history()
        log('AGENT', f'📊 Ingredient price history: {len(price_history)} ingredients known.')

        recipes     = await asyncio.to_thread(get_recipes_data)
        competitors = await asyncio.to_thread(get_competitors_menus)

        if not recipes:
            log('AGENT', '⚠️ No recipes in Firebase. Skipping optimization.')
            return

        feasible = [r for r in recipes
                    if estimate_recipe_cost(r, price_history) * 1.1 <= MAX_MENU_PRICE]

        log('AGENT', f'🔍 Feasible recipes (cost×1.1 ≤ {MAX_MENU_PRICE}cr): {len(feasible)}/{len(recipes)}')

        if not feasible:
            log('AGENT', '⚠️ No feasible recipes within price cap — keeping current TARGET_RECIPE.')
            return

        best_menu = await asyncio.to_thread(
            build_and_solve_model, feasible, competitors, 1, 0.5, 'esploratore', DEFAULT_ING_PRICE
        )

        if best_menu:
            chosen    = best_menu[0]
            real_cost = estimate_recipe_cost(chosen, price_history)
            real_price = min(int(real_cost * 1.1), MAX_MENU_PRICE)
            chosen['recommended_price'] = real_price
            TARGET_RECIPE = chosen
            name_str = TARGET_RECIPE['name']
            log('AGENT', f'✅ Planner chose: "{name_str}" | Est. cost: {real_cost:.0f}cr | Price: {real_price}cr')
        else:
            log('AGENT', '⚠️ Planner returned no recipes from feasible set.')

    except Exception as e:
        log('ERROR', f'_run_planner failed: {e}')

async def game_started(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID

    turn_id = data.get("turn_id", None)
    CURRENT_TURN_ID = turn_id
    DISH_READY = False
    TARGET_RECIPE = None # Reset recipe at start of turn since we define it in waiting phase!

    log("EVENT", "game started, turn id: " + str(turn_id))

    # 1. Apriamo il ristorante in ogni nuova run
    try:
        log("AGENT", "Opening the restaurant for the new turn.")
        res = await mcp_client.call_tool("update_restaurant_is_open", {"is_open": True})
        log("AGENT", f"MCP Tool Response (Open Menu): {res}")
    except Exception as e:
        log("ERROR", f"Failed to open restaurant: {e}")

    # 2. Sincronizza ricette su Firebase (in background, non bloccante)
    asyncio.create_task(_sync_firebase())


async def _sync_firebase() -> None:
    """Sincronizza ricette e ristoranti su Firebase in background."""
    try:
        log("AGENT", "Syncing recipes and restaurants to Firebase...")
        await fetch_and_store_recipes()
        await fetch_and_store_restaurants()
        log("AGENT", "Firebase sync complete.")
    except Exception as e:
        log("ERROR", f"Firebase sync failed: {e}")


async def speaking_phase_started() -> None:
    log("EVENT", "speaking phase started")
    
    # Manda 'cosa avete oggi nel menu?' a tutti i ristoranti aperti
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{BASE_URL}/restaurants"
            headers = {"x-api-key": TEAM_API_KEY}
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if isinstance(data, list):
                        restaurants = data
                    elif isinstance(data, dict) and "restaurants" in data:
                        restaurants = data["restaurants"]
                    else:
                        restaurants = []

                    msg = "cosa avete oggi nel menu?"
                    count = 0
                    for rest in restaurants:
                        rest_id = rest.get("id")
                        if rest_id is not None and str(rest_id) != str(TEAM_ID) and rest.get("isOpen") is True:
                            try:
                                await mcp_client.call_tool("send_message", {"recipient_id": rest_id, "text": msg})
                                count += 1
                                log("AGENT", f"Sent message to restaurant {rest_id}")
                            except Exception as e:
                                log("ERROR", f"Failed to send message to {rest_id}: {e}")
                    log("AGENT", f"Speaking phase: sent message to {count} open restaurants.")
                else:
                    log("ERROR", f"Failed to fetch restaurants: HTTP {response.status}")
    except Exception as e:
        log("ERROR", f"Error during speaking phase message distribution: {e}")


async def closed_bid_phase_started(data: dict[str, Any] = None) -> None:
    log("EVENT", "closed bid phase started")
        
    global CURRENT_TURN_ID, LAST_BID_INGREDIENT, TARGET_RECIPE
    import api as game_api
    
    if data and data.get("turn_id"):
        CURRENT_TURN_ID = data.get("turn_id")
        
    if CURRENT_TURN_ID is not None and CURRENT_TURN_ID > 0:
        log("AGENT", "1. in fase close-bid punti su tanti ingredienti che nei log precedenti non costano tanto")
        
        try:
            recipes = await game_api.get_recipes()
            if not recipes:
                log("AGENT", "No recipes found, skipping bid.")
                return
            real_ingredients = set()
            for recipe in recipes:
                for ing in recipe.get("ingredients", {}).keys():
                    real_ingredients.add(ing)
                    
            history_prices = get_ingredient_prices_from_history()
            merged_prices = {}
            for ing in real_ingredients:
                merged_prices[ing] = history_prices.get(ing, DEFAULT_ING_PRICE)
                
            sorted_ings = sorted(merged_prices.items(), key=lambda x: x[1])
            cheapest_ings = sorted_ings[:25]
            
            bids_payload = []
            for ing, price in cheapest_ings:
                bids_payload.append({
                    "ingredient": ing,
                    "bid": int(price + 1),
                    "quantity": 2
                })
                
            if bids_payload:
                log("AGENT", f"Placing {len(bids_payload)} bulk bids on cheap ingredients.")
                LAST_BID_INGREDIENT = bids_payload[0]["ingredient"]
                response = await mcp_client.call_tool("closed_bid", {"bids": bids_payload})
                log("AGENT", f"MCP Tool Response (Closed Bid): {response}")
                log_bids_to_file(CURRENT_TURN_ID, "cheap_bulk", bids_payload)
                
        except Exception as e:
            log("ERROR", f"Bidding pipeline failed: {e}")
    else:
        log("AGENT", "Skip pricing calculation for turn 0 or unknown turn.")


async def waiting_phase_started() -> None:
    log("EVENT", "waiting phase started")

    global CURRENT_TURN_ID, TARGET_RECIPE
    import api as game_api
    if CURRENT_TURN_ID is not None and CURRENT_TURN_ID > 0:
        log("AGENT 4", f"Starting bid history scraping for turn {CURRENT_TURN_ID}...")
        asyncio.create_task(fetch_and_save_bid_history(
            turn_id=CURRENT_TURN_ID,
            api_key=TEAM_API_KEY,
            base_url=BASE_URL,
            cred_path=CRED_PATH
        ))
    else:
        log("AGENT 4", "Error: unknown turn_id. Cannot download bids.")

    # 2. in base a quello che hai comprato definisci le ricette e imposta un prezzo basso (guadagno 1%)
    try:
        log("AGENT", "Fetching inventory to define recipes based on what we bought...")
        my_restaurant = await game_api.get_my_restaurant()
        if not my_restaurant:
            log("ERROR", "Could not fetch restaurant status in waiting phase.")
            return
            
        inventory = my_restaurant.get("inventory", {})
        log("AGENT", f"Current inventory: {inventory}")
        
        recipes = await game_api.get_recipes()
        feasible_recipes = []
        
        for recipe in recipes:
            can_make = True
            for ing, qty_needed in recipe.get("ingredients", {}).items():
                if inventory.get(ing, 0) < qty_needed:
                    can_make = False
                    break
            if can_make:
                feasible_recipes.append(recipe)
                
        if feasible_recipes:
            history_prices = get_ingredient_prices_from_history()
            best_recipe = None
            lowest_cost = float('inf')
            
            for r in feasible_recipes:
                cost = sum(history_prices.get(ing, DEFAULT_ING_PRICE) * qty for ing, qty in r.get("ingredients", {}).items())
                if cost < lowest_cost:
                    lowest_cost = cost
                    best_recipe = r
                    
            if best_recipe:
                TARGET_RECIPE = best_recipe
                cost = lowest_cost
                # prezzo basso (guadagno 1%)
                target_price = max(1, int(cost * 1.01))
                TARGET_RECIPE["recommended_price"] = target_price
                log("AGENT", f"Determined TARGET_RECIPE based on inventory: '{TARGET_RECIPE['name']}' at price {target_price}")
                
                # IMPORTANTE devi salvare il menu
                menu_payload = [{"name": TARGET_RECIPE["name"], "price": target_price}]
                res = await mcp_client.call_tool("save_menu", {"items": menu_payload})
                log("AGENT", f"save_menu response: {res}")
        else:
            log("AGENT", "No feasible recipes with current inventory.")
            TARGET_RECIPE = None
    except Exception as e:
        log("ERROR", f"Waiting phase menu setting failed: {e}")


async def serving_phase_started() -> None:
    log("EVENT", "serving phase started")
    global TARGET_RECIPE, DISH_READY
    
    # IMPORTANTE: preparare il piatto e venderlo ai clienti (la vendita è in client_spawned)
    if TARGET_RECIPE:
        dish_name = TARGET_RECIPE.get("name")
        log("AGENT", f"🍳 Preparing dish: '{dish_name}'...")
        try:
            res = await mcp_client.call_tool("prepare_dish", {"dish_name": dish_name})
            log("AGENT", f"prepare_dish response: {res}")
        except Exception as e:
            log("ERROR", f"Failed to prepare dish: {e}")
    else:
        log("AGENT", "No TARGET_RECIPE set, skipping preparation. Looking at server menu instead...")
        # Fallback if no feasible recipe found, try to see if we had one and just use its name
        try:
            import api as game_api
            my_restaurant = await game_api.get_my_restaurant()
            menu_items = my_restaurant.get("menu", {}).get("items", [])
            if menu_items:
                fallback_dish = menu_items[0].get("name")
                log("AGENT", f"🍳 Fallback Preparing dish: '{fallback_dish}'...")
                res = await mcp_client.call_tool("prepare_dish", {"dish_name": fallback_dish})
                log("AGENT", f"prepare_dish fallback response: {res}")
        except Exception as e:
            log("ERROR", f"Failed fallback prepare: {e}")


async def end_turn() -> None:
    log("EVENT", "turn ended")
    global TARGET_RECIPE

    # Pre-calcola la ricetta migliore per il turno SUCCESSIVO (OR-Tools)
    # così è già pronta quando scatta game_started
    # We no longer re-compute recipes at end_turn (Esploratore strategy)
    pass


async def client_spawned(data: dict[str, Any]) -> None:
    client_name = data.get("clientName", "unknown")
    order_text = str(data.get("orderText", "unknown"))
    order_text = order_text.lower().replace("i'd like a ", "").replace("i'd like ", "")
    log("EVENT", f"client={client_name} order={order_text}")


async def preparation_complete(data: dict[str, Any]) -> None:
    dish_name = data.get("dish", "unknown")
    log("EVENT", f"dish ready: {dish_name}")


async def message(data: dict[str, Any]) -> None:
    sender = data.get("sender", "unknown")
    text = data.get("payload", "")
    log("EVENT", f"message from {sender}: {text}")


async def game_phase_changed(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID
    phase = data.get("phase", "unknown")
    
    # Recupera il turn_id dall'evento se ci siamo persi il game_started (es. restart)
    recovered_turn_id = data.get("turn_id")
    if recovered_turn_id and (CURRENT_TURN_ID is None or CURRENT_TURN_ID == 0):
        CURRENT_TURN_ID = recovered_turn_id
        log("AGENT", f"[RECOVERY] Recovered turn_id={CURRENT_TURN_ID} from phase event '{phase}'")

    handlers: dict[str, Callable[[], Awaitable[None]]] = {
        "speaking": speaking_phase_started,
        "closed_bid": closed_bid_phase_started,
        "waiting": waiting_phase_started,
        "serving": serving_phase_started,
        "stopped": end_turn,
    }
    handler = handlers.get(phase)
    if handler:
        await handler()
    else:
        log("EVENT", f"unknown phase: {phase}")


async def game_reset(data: dict[str, Any]) -> None:
    if data:
        log("EVENT", f"game reset: {data}")
    else:
        log("EVENT", "game reset")


EVENT_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "game_started": game_started,
    "game_phase_changed": game_phase_changed,
    "game_reset": game_reset,
    "client_spawned": client_spawned,
    "preparation_complete": preparation_complete,
    "message": message,
}

##########################################################################################
#                                    DANGER ZONE                                         #
##########################################################################################
# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.


# It is the central event dispatcher used by all handlers.
async def dispatch_event(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        return
    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler failed for {event_type}: {exc}")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It parses SSE lines and translates them into internal events.
async def handle_line(raw_line: bytes) -> None:
    if not raw_line:
        return

    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return

    # Standard SSE data format: data: ...
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        log("SSE", f"raw: {line}")
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})
    if isinstance(event_data, dict):
        await dispatch_event(event_type, event_data)
    else:
        await dispatch_event(event_type, {"value": event_data})


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It owns the SSE HTTP connection lifecycle.
async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open")
        async for line in response.content:
            await handle_line(line)


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It controls script exit behavior when the SSE connection drops.
async def listen_once_and_exit_on_drop() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        await listen_once(session)
        log("SSE", "connection closed, exiting")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# Keep this minimal to avoid changing startup behavior.
async def main() -> None:
    log("INIT", f"team={TEAM_ID} base_url={BASE_URL}")
    await listen_once_and_exit_on_drop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "client stopped")