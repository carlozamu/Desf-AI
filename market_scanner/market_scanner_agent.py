"""
market_scanner/market_scanner_agent.py
---------------------------------------
Market Scanner Agent — scans the live market and produces opportunity alerts
for the Trader agent to act on.

Input  : shopping_list (dict[str, int])  — from the Chef agent
Output : (markdown_report: str, alerts: list[dict])
         alerts are also persisted to Firestore under market_alerts/turn_{N}
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Any

import firebase_admin
from firebase_admin import credentials, firestore

from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents import Agent
from datapizza.tools import Tool

import api
from memory import GameMemory
from market_scanner.prompts import MARKET_SCANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ── Firebase (idempotent) ─────────────────────────────────────────────────────
if not firebase_admin._apps:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    firebase_admin.initialize_app(
        credentials.Certificate(cred_path) if cred_path else None
    )

db = firestore.client()

# ── AI Client ─────────────────────────────────────────────────────────────────
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY")
if not REGOLO_API_KEY:
    logger.warning("REGOLO_API_KEY not set — Market Scanner Agent will not work.")

regolo_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ── Shared state ──────────────────────────────────────────────────────────────
_memory: GameMemory = None
_shopping_list: Dict[str, int] = {}


def init_market_scanner(mem: GameMemory) -> None:
    """Call once at startup to bind the agent to the shared GameMemory."""
    global _memory
    _memory = mem


# ── Mock data (fallback if not connected to pipeline) ─────────────────────────
MOCK_SHOPPING_LIST: Dict[str, int] = {
    "Radici di Gravità": 5,
    "Foglie di Nebulosa": 10,
    "Essenza di Tachioni": 5,
}

MOCK_MARKET_ENTRIES: List[Dict] = [
    {
        "id": 1, "side": "SELL", "ingredient_name": "Radici di Gravità",
        "quantity": 8, "price": 12.0, "restaurant_id": "team_b"
    },
    {
        "id": 2, "side": "SELL", "ingredient_name": "Foglie di Nebulosa",
        "quantity": 5, "price": 18.5, "restaurant_id": "team_c"
    },
    {
        "id": 3, "side": "BUY", "ingredient_name": "Pomodoro Spaziale",
        "quantity": 10, "price": 9.0, "restaurant_id": "team_d"
    },
]


# ── Tools ─────────────────────────────────────────────────────────────────────
async def get_shopping_list(**kwargs) -> str:
    """
    Returns the Chef's current shopping list.
    Reads from the in-memory dict first; falls back to Firestore, then mock.
    """
    if _shopping_list:
        return json.dumps(_shopping_list, indent=2)

    # Try Firestore
    if _memory:
        turn_id = _memory.get_current_state().get("turn_id", 0)
        try:
            doc = db.collection("chef_plans").document(f"turn_{turn_id}").get()
            if doc.exists:
                data = doc.to_dict()
                return json.dumps(data.get("shopping_list", {}), indent=2)
        except Exception as e:
            logger.warning(f"Could not read shopping list from Firestore: {e}")

    return json.dumps(MOCK_SHOPPING_LIST, indent=2)


async def get_current_inventory(**kwargs) -> str:
    """Returns the current ingredient inventory of the restaurant."""
    if _memory:
        inventory = _memory.get_current_state().get("inventory", {})
        if inventory:
            return json.dumps(inventory, indent=2)
    return json.dumps({"Pomodoro Spaziale": 10, "Farina Quantica": 5}, indent=2)


async def get_live_market(**kwargs) -> str:
    """
    Fetches all active market entries (BUY/SELL offers from all restaurants).
    Falls back to mock data if the API is unavailable.
    """
    try:
        entries = await api.get_market_entries()
        if entries:
            return json.dumps(entries, indent=2)
    except Exception as e:
        logger.warning(f"Live market unavailable: {e}")
    return json.dumps(MOCK_MARKET_ENTRIES, indent=2)


async def get_bid_history(**kwargs) -> str:
    """
    Fetches the bid history for the current turn to establish price anchors.
    Falls back to an empty list if unavailable.
    """
    turn_id = 0
    if _memory:
        turn_id = _memory.get_current_state().get("turn_id", 0)
    try:
        history = await api.get_bid_history(turn_id)
        if history:
            return json.dumps(history, indent=2)
    except Exception as e:
        logger.warning(f"Bid history unavailable: {e}")
    return json.dumps([], indent=2)


_tools = [
    Tool(
        name="get_shopping_list",
        func=get_shopping_list,
        description="Returns the Chef's shopping list: ingredients needed and quantity.",
    ),
    Tool(
        name="get_current_inventory",
        func=get_current_inventory,
        description="Returns the current ingredient inventory of the restaurant.",
    ),
    Tool(
        name="get_live_market",
        func=get_live_market,
        description="Returns all active BUY/SELL market entries from all restaurants.",
    ),
    Tool(
        name="get_bid_history",
        func=get_bid_history,
        description="Returns the bid history for the current turn to calibrate fair prices.",
    ),
]


# ── Alert parser ──────────────────────────────────────────────────────────────
def _parse_alerts(markdown_text: str) -> List[Dict]:
    """
    Extracts the JSON alert array from the agent's markdown output.
    Looks for the last ```json ... ``` block and parses it.
    """
    json_blocks = re.findall(r"```json\s*\n(.*?)```", markdown_text, re.DOTALL)
    for block in reversed(json_blocks):  # Use the last JSON block
        try:
            data = json.loads(block.strip())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            continue
    return []


# ── Firebase persistence ───────────────────────────────────────────────────────
def save_alerts_to_firebase(
    alerts: List[Dict],
    turn_id: int = 0,
) -> None:
    """
    Saves opportunity alerts to Firestore:
      Collection : market_alerts
      Document   : turn_{turn_id}
    """
    if not alerts:
        logger.warning("No alerts to save — skipping Firebase write.")
        return

    payload = {
        "alerts": alerts,
        "turn_id": turn_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        db.collection("market_alerts").document(f"turn_{turn_id}").set(payload, merge=True)
        logger.info(f"Alerts saved → Firestore market_alerts/turn_{turn_id}")
    except Exception as e:
        logger.error(f"Failed to save alerts to Firebase: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
def scan_market(shopping_list: Dict[str, int]) -> tuple[str, List[Dict]]:
    """
    Run the Market Scanner Agent.

    Parameters
    ----------
    shopping_list : dict[str, int]
        The Chef's shopping list (ingredient → quantity needed).

    Returns
    -------
    markdown_report : str
        Full analysis markdown (market summary, BUY/SELL tables, bid tips).
    alerts : list[dict]
        Parsed opportunity alerts; also persisted to Firestore.
    """
    global _shopping_list
    _shopping_list = shopping_list  # Make available to tools

    turn_id: int = 0
    if _memory:
        turn_id = _memory.get_current_state().get("turn_id", 0)

    agent = Agent(
        name="MarketScannerAgent",
        client=regolo_client,
        system_prompt=MARKET_SCANNER_SYSTEM_PROMPT,
        tools=_tools,
    )

    try:
        response = agent.run(
            "Scan the live market based on the Chef's shopping list and our current inventory. "
            "Produce actionable BUY/SELL alerts for the Trader."
        )
        markdown_report: str = getattr(response, "text", str(response))
    except Exception as e:
        logger.error(f"Market Scanner Agent failed: {e}")
        return f"Error: {e}", []

    alerts = _parse_alerts(markdown_report)
    logger.info(f"Parsed {len(alerts)} market alerts.")

    save_alerts_to_firebase(alerts, turn_id=turn_id)

    return markdown_report, alerts
