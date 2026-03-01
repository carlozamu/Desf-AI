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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    import aiohttp
else:
    try:
        import aiohttp  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        aiohttp = None  # type: ignore[assignment]

from game_client.logging_utils import log as structured_log
from game_client.logging_utils import log_context, new_correlation_id, set_log_context


import os
from dotenv import load_dotenv

load_dotenv()

TEAM_ID = os.getenv("TEAM_ID")
TEAM_API_KEY = os.getenv("SERVER_API_KEY")
CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
set_log_context(run_id=os.getenv("RUN_ID", "main"))

if not TEAM_API_KEY or not TEAM_ID:
    raise SystemExit("Set TEAM_API_KEY and TEAM_ID environment variables")

# GLOBALE VARIAVLE TO STORE the TURNEMENT
CURRENT_TURN_ID = None


def log(tag: str, message: str, **fields: Any) -> None:
    structured_log(tag, message, **fields)


def _safe_turn_id(value: Any) -> int | None:
    try:
        turn_id = int(value)
    except (TypeError, ValueError):
        return None
    if turn_id <= 0:
        return None
    return turn_id


async def game_started(data: dict[str, Any]) -> None:
    global CURRENT_TURN_ID

    turn_id = _safe_turn_id(data.get("turn_id"))

    # Store the turnement id
    CURRENT_TURN_ID = turn_id
    set_log_context(turn_id=turn_id, phase="speaking")

    log("EVENT", "game started, turn id: " + str(turn_id), event="event.game_started")


async def speaking_phase_started() -> None:
    set_log_context(phase="speaking")
    log("EVENT", "speaking phase started", event="phase.started")


async def closed_bid_phase_started() -> None:
    set_log_context(phase="closed_bid")
    log("EVENT", "closed bid phase started", event="phase.started")
    
    global CURRENT_TURN_ID
    if CURRENT_TURN_ID is not None:
        log("AGENT", f"Calculating bid prices for turn {CURRENT_TURN_ID}...")

        try:
            from price_analyst.agent import get_bidding_prices
        except Exception as exc:
            log(
                "ERROR",
                f"price_analyst unavailable: {exc}",
                event="price_analyst.unavailable",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return
        
        # We run the agent synchronously in a separate thread so we don't block the asyncio event loop
        prices = await asyncio.to_thread(get_bidding_prices, CURRENT_TURN_ID)
        
        if prices:
            log("AGENT", f"Suggested Prices: {json.dumps(prices, indent=2)}")
            # FUTURE: Submit these bids to the server using the team API
            
            # --- START BIDDING PIPELINE ---
            from bidder.bidder import place_bids_for_turn
            
            try:
                log("AGENT", f"Placing bids for turn {CURRENT_TURN_ID}...")
                await place_bids_for_turn(turn_id=CURRENT_TURN_ID, prices=prices)
                
                log("AGENT", "Bidding pipeline execution completed.")
            except Exception as e:
                log("ERROR", f"Failed to execute bidding pipeline: {e}")
            # --- END BIDDING PIPELINE ---
    else:
        log("AGENT", "Skip pricing calculation for turn 0 or unknown turn.")


async def waiting_phase_started() -> None:
    set_log_context(phase="waiting")
    log("EVENT", "waiting phase started", event="phase.started")

    global CURRENT_TURN_ID
    if CURRENT_TURN_ID is not None:
        log("AGENT 4", f"Starting bid history scraping for turn {CURRENT_TURN_ID}...")

        try:
            from price_analyst.bid_tracker import fetch_and_save_bid_history
        except Exception as exc:
            log(
                "ERROR",
                f"bid_tracker unavailable: {exc}",
                event="bid_history.unavailable",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return

        async def _safe_fetch_bid_history() -> None:
            try:
                await fetch_and_save_bid_history(
                    turn_id=CURRENT_TURN_ID,
                    api_key=TEAM_API_KEY,
                    base_url=BASE_URL,
                    cred_path=CRED_PATH,
                )
            except Exception as exc:
                log(
                    "ERROR",
                    f"bid history background task failed: {exc}",
                    event="bid_history.task.failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        # Launch the task in the background without blocking the SSE
        asyncio.create_task(_safe_fetch_bid_history())
    else:
        log("AGENT 4", "Skip bid history: unknown or invalid turn_id.")


async def serving_phase_started() -> None:
    set_log_context(phase="serving")
    log("EVENT", "serving phase started", event="phase.started")


async def end_turn() -> None:
    set_log_context(phase="stopped")
    log("EVENT", "turn ended", event="phase.started")


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
    event_turn_id = _safe_turn_id(data.get("turn_id"))
    if event_turn_id is not None:
        CURRENT_TURN_ID = event_turn_id

    phase = data.get("phase", "unknown")
    set_log_context(phase=phase, turn_id=CURRENT_TURN_ID)
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
            log("SSE", "connected", event="sse.connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        log("SSE", f"raw: {line}")
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})
    with log_context(correlation_id=new_correlation_id(), event_type=event_type):
        if isinstance(event_data, dict):
            await dispatch_event(event_type, event_data)
        else:
            await dispatch_event(event_type, {"value": event_data})


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It owns the SSE HTTP connection lifecycle.
async def listen_once(session: "aiohttp.ClientSession") -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open", event="sse.connection.open")
        async for line in response.content:
            await handle_line(line)


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It controls script exit behavior when the SSE connection drops.
async def listen_once_and_exit_on_drop() -> None:
    if aiohttp is None:
        raise RuntimeError("aiohttp is required to run client_template SSE listener")

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
