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


async def game_started(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID

    turn_id = data.get("turn_id", None)

    # Store the turnement id
    CURRENT_TURN_ID = turn_id 

    log("EVENT", "game started, turn id: " + str(turn_id))

    # 1. Apriamo il ristorante in ogni nuova run
    try:
        log("AGENT", "Opening the restaurant for the new turn.")
        res = await mcp_client.call_tool("update_restaurant_is_open", {"is_open": True})
        log("AGENT", f"MCP Tool Response (Open Menu): {res}")
    except Exception as e:
        log("ERROR", f"Failed to open restaurant: {e}")


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
        
    global CURRENT_TURN_ID
    
    # Tentativo di recuperare il turn_id dal payload dell'evento se ci siamo persi il game_started
    if data and data.get("turn_id"):
        CURRENT_TURN_ID = data.get("turn_id")
        
    if CURRENT_TURN_ID is not None and CURRENT_TURN_ID > 0:
        log("AGENT", f"Calculating bid prices for turn {CURRENT_TURN_ID} using AI Analyst...")
        
        try:
            # 1. Recupera la lista reale di ingredienti dal server tramite le ricette
            real_ingredients: set[str] = set()
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BASE_URL}/recipes", headers={"x-api-key": TEAM_API_KEY}) as r:
                    if r.status == 200:
                        recipes = await r.json()
                        for recipe in recipes:
                            for ing in recipe.get("ingredients", {}).keys():
                                real_ingredients.add(ing)
                        log("AGENT", f"Loaded {len(real_ingredients)} real game ingredients from server.")
                    else:
                        log("ERROR", f"Could not fetch recipes: HTTP {r.status}")

            if not real_ingredients:
                log("AGENT", "No real ingredients found, skipping bid.")
                return

            # 2. Genera i prezzi tramite l'AI Analyst
            prices = await asyncio.to_thread(get_bidding_prices, CURRENT_TURN_ID)
            
            if prices and len(prices) > 0:
                log("AGENT", f"AI Suggested prices for {len(prices)} items.")

                # 3. Scegliamo il PRIMO ingrediente suggerito che esista davvero nel gioco
                valid_ingredient = None
                valid_price = None
                for name, price in prices.items():
                    if name in real_ingredients:
                        valid_ingredient = name
                        valid_price = price
                        break

                if valid_ingredient is None:
                    # Fallback: prendi qualunque ingrediente reale disponibile
                    valid_ingredient = next(iter(real_ingredients))
                    valid_price = 20  # prezzo di fallback ragionevole
                    log("AGENT", f"AI didn't suggest real ingredients, fallback to: {valid_ingredient}")

                bids_payload = [{"ingredient": valid_ingredient, "bid": round(valid_price * 1.1, 2), "quantity": 1}]
                log("AGENT", f"Placing SINGLE test bid: {json.dumps(bids_payload)}")
                
                # 4. Inviamo la transazione (Asta Cieca) via MCP Tool 
                response = await mcp_client.call_tool("closed_bid", {"bids": bids_payload})
                log("AGENT", f"MCP Tool Response (Closed Bid): {response}")
                
            else:
                log("AGENT", "Nessun prezzo calcolato, salto il bid testuale.")
                
        except Exception as e:
            log("ERROR", f"Bidding pipeline failed: {e}")
    else:
        log("AGENT", "Skip pricing calculation for turn 0 or unknown turn.")


async def waiting_phase_started() -> None:
    log("EVENT", "waiting phase started")

    global CURRENT_TURN_ID
    if CURRENT_TURN_ID != 0:
        log("AGENT 4", f"Starting bid history scraping for turn {CURRENT_TURN_ID}...")
        # Launch the task in the background without blocking the SSE
        asyncio.create_task(fetch_and_save_bid_history(
            turn_id=CURRENT_TURN_ID,
            api_key=TEAM_API_KEY,
            base_url=BASE_URL,
            cred_path=CRED_PATH
        ))
    else:
        log("AGENT 4", "Error: unknown turn_id. Cannot download bids.")


async def serving_phase_started() -> None:
    log("EVENT", "serving phase started")


async def end_turn() -> None:
    log("EVENT", "turn ended")

    global CURRENT_TURN_ID
    if CURRENT_TURN_ID is not None and CURRENT_TURN_ID > 0:
        log("MEALS", f"Collecting meals data for turn {CURRENT_TURN_ID}...")
        try:
            db = init_firebase()
            if db:
                restaurants_data = await collect_turn_meals(CURRENT_TURN_ID)
                if restaurants_data:
                    await asyncio.to_thread(
                        save_to_firebase, db, CURRENT_TURN_ID, RUN_ID, restaurants_data
                    )
                    log("MEALS", f"✅ Meals for turn {CURRENT_TURN_ID} saved to Firebase.")
                else:
                    log("MEALS", "⚠️  No meals data collected.")
            else:
                log("MEALS", "⚠️  Firebase not available, skipping meals save.")
        except Exception as e:
            log("ERROR", f"Meals tracker failed: {e}")
    else:
        log("MEALS", "⚠️  Unknown turn_id, skipping meals save.")


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