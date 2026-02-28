"""
trader/trader_agent.py
-----------------------
Trader Agent — executes BUY/SELL transactions on the live market.

Input  : alerts (list[dict])  — from the Market Scanner
Output : (markdown_report: str, inventory_delta: dict[str, int])
         - inventory_delta: {ingredient: +N bought / -N sold}
         - All trades logged to Firestore under trade_log/turn_{N}
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List

import firebase_admin
from firebase_admin import credentials, firestore

from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents import Agent
from datapizza.tools import Tool

import api
from memory import GameMemory
from trader.prompts import TRADER_SYSTEM_PROMPT

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
    logger.warning("REGOLO_API_KEY not set — Trader Agent will not work.")

regolo_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ── Shared state ──────────────────────────────────────────────────────────────
_memory: GameMemory = None
_alerts: List[Dict] = []
_dry_run: bool = os.getenv("TRADER_DRY_RUN", "false").lower() == "true"
# Track executed trades in-process so tools can reference them
_trade_log: List[Dict] = []


def init_trader(mem: GameMemory) -> None:
    """Call once at startup to bind the agent to the shared GameMemory."""
    global _memory
    _memory = mem


# ── Tools ──────────────────────────────────────────────────────────────────────
async def get_current_alerts(**kwargs) -> str:
    """Returns the current Market Scanner opportunity alerts."""
    return json.dumps(_alerts, indent=2)


async def get_current_inventory(**kwargs) -> str:
    """Returns the current ingredient inventory of the restaurant."""
    if _memory:
        inventory = _memory.get_current_state().get("inventory", {})
        if inventory:
            return json.dumps(inventory, indent=2)
    return json.dumps({}, indent=2)


async def get_restaurant_balance(**kwargs) -> str:
    """Returns the current restaurant balance (budget available)."""
    try:
        info = await api.get_my_restaurant()
        if info:
            balance = info.get("balance", info.get("saldo", 0))
            return json.dumps({"balance": balance}, indent=2)
    except Exception as e:
        logger.warning(f"Could not fetch balance: {e}")
    return json.dumps({"balance": 9999}, indent=2)  # fallback for dry-run


async def execute_buy(market_entry_id: int, ingredient: str, quantity: int, **kwargs) -> str:
    """
    Executes an immediate purchase from a live market SELL entry.
    In dry-run mode, only simulates the trade.
    """
    if _dry_run:
        logger.info(f"[DRY-RUN] BUY {quantity}x {ingredient} (entry #{market_entry_id})")
        _trade_log.append({"type": "BUY", "ingredient": ingredient, "quantity": quantity,
                            "market_entry_id": market_entry_id, "dry_run": True})
        return json.dumps({"success": True, "dry_run": True})

    try:
        result = await api.call_mcp_tool("execute_transaction", {"market_entry_id": market_entry_id})
        success = not result.get("result", {}).get("isError", True)
        _trade_log.append({"type": "BUY", "ingredient": ingredient, "quantity": quantity,
                            "market_entry_id": market_entry_id, "success": success})
        return json.dumps({"success": success, "result": result})
    except Exception as e:
        logger.error(f"execute_buy failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


async def create_sell_listing(ingredient: str, quantity: int, price: float, **kwargs) -> str:
    """
    Creates a SELL listing on the open market for surplus ingredients.
    In dry-run mode, only simulates the action.
    """
    if _dry_run:
        logger.info(f"[DRY-RUN] SELL {quantity}x {ingredient} @ {price}")
        _trade_log.append({"type": "SELL", "ingredient": ingredient, "quantity": quantity,
                            "price": price, "dry_run": True})
        return json.dumps({"success": True, "dry_run": True})

    try:
        result = await api.call_mcp_tool("create_market_entry", {
            "side": "SELL",
            "ingredient_name": ingredient,
            "quantity": quantity,
            "price": price,
        })
        success = not result.get("result", {}).get("isError", True)
        _trade_log.append({"type": "SELL", "ingredient": ingredient, "quantity": quantity,
                            "price": price, "success": success})
        return json.dumps({"success": success, "result": result})
    except Exception as e:
        logger.error(f"create_sell_listing failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


_tools = [
    Tool(name="get_current_alerts", func=get_current_alerts,
         description="Returns the prioritized BUY/SELL opportunity alerts from the Market Scanner."),
    Tool(name="get_current_inventory", func=get_current_inventory,
         description="Returns the current ingredient inventory of the restaurant."),
    Tool(name="get_restaurant_balance", func=get_restaurant_balance,
         description="Returns the restaurant's current balance (available budget)."),
    Tool(name="execute_buy", func=execute_buy,
         description="Executes an immediate purchase from a market SELL entry. Args: market_entry_id (int), ingredient (str), quantity (int)."),
    Tool(name="create_sell_listing", func=create_sell_listing,
         description="Creates a SELL listing for surplus ingredients. Args: ingredient (str), quantity (int), price (float)."),
]


# ── Delta parser ──────────────────────────────────────────────────────────────
def _compute_inventory_delta() -> Dict[str, int]:
    """
    Computes the inventory delta from executed trades.
    BUY = +quantity, SELL = -quantity.
    """
    delta: Dict[str, int] = {}
    for trade in _trade_log:
        ing = trade.get("ingredient", "")
        qty = trade.get("quantity", 0)
        if trade.get("type") == "BUY":
            delta[ing] = delta.get(ing, 0) + qty
        elif trade.get("type") == "SELL":
            delta[ing] = delta.get(ing, 0) - qty
    return {k: v for k, v in delta.items() if v != 0}


# ── Firebase persistence ───────────────────────────────────────────────────────
def save_trade_log_to_firebase(turn_id: int, iteration: int) -> None:
    """
    Persists the trade log for this iteration to Firestore:
      Collection : trade_log
      Document   : turn_{N}_iter_{I}
    """
    if not _trade_log:
        return
    payload = {
        "trades": _trade_log.copy(),
        "turn_id": turn_id,
        "iteration": iteration,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        doc_id = f"turn_{turn_id}_iter_{iteration}"
        db.collection("trade_log").document(doc_id).set(payload, merge=True)
        logger.info(f"Trade log saved → Firestore trade_log/{doc_id}")
    except Exception as e:
        logger.error(f"Failed to save trade log: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
async def execute_trades(
    alerts: List[Dict],
    turn_id: int = 0,
    iteration: int = 0,
) -> tuple[str, Dict[str, int]]:
    """
    Run the Trader Agent to execute BUY/SELL transactions.

    Parameters
    ----------
    alerts      : list from Market Scanner
    turn_id     : current turn
    iteration   : loop iteration number (for logging)

    Returns
    -------
    markdown_report : str
    inventory_delta : dict[str, int]  (+N = bought, -N = sold)
    """
    global _alerts, _trade_log
    _alerts = alerts
    _trade_log = []  # Reset for this run

    agent = Agent(
        name="TraderAgent",
        client=regolo_client,
        system_prompt=TRADER_SYSTEM_PROMPT,
        tools=_tools,
    )

    try:
        response = agent.run(
            "Execute the trades based on the opportunity alerts. "
            "Sell surplus first, then buy what the Chef needs within budget."
        )
        markdown_report: str = getattr(response, "text", str(response))
    except Exception as e:
        logger.error(f"Trader Agent failed: {e}")
        return f"Error: {e}", {}

    inventory_delta = _compute_inventory_delta()
    save_trade_log_to_firebase(turn_id, iteration)

    logger.info(f"Trader done — inventory delta: {inventory_delta}")
    return markdown_report, inventory_delta
