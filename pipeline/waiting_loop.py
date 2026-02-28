"""
pipeline/waiting_loop.py
------------------------
Orchestrates the iterative Chef → Market Scanner → Trader loop
during the WAITING phase.

Goal: minimize ingredient waste and maximize menu profitability.
Terminates when no more useful trades can be made, or max_iterations is reached.
At every iteration, menu + inventory are persisted to Firestore.
At exit, the final menu is pushed to the server via save_menu MCP tool.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import firebase_admin
from firebase_admin import firestore
import os

import api
from memory import GameMemory

logger = logging.getLogger(__name__)

# ── Firebase (idempotent) ─────────────────────────────────────────────────────
if not firebase_admin._apps:
    from firebase_admin import credentials
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    firebase_admin.initialize_app(
        credentials.Certificate(cred_path) if cred_path else None
    )
db = firestore.client()


# ── Firestore helpers ─────────────────────────────────────────────────────────
def _save_loop_snapshot(
    turn_id: int,
    iteration: int,
    inventory: Dict[str, int],
    menu: List[Dict],
    shopping_list: Dict[str, int],
    alerts_count: int,
    delta: Dict[str, int],
) -> None:
    """
    Saves a full snapshot of the current loop state to Firestore.
    Collection : loop_snapshots
    Document   : turn_{N}_iter_{I}
    """
    payload = {
        "turn_id": turn_id,
        "iteration": iteration,
        "inventory": inventory,
        "menu": menu,
        "shopping_list": shopping_list,
        "alerts_count": alerts_count,
        "inventory_delta": delta,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        doc_id = f"turn_{turn_id}_iter_{iteration}"
        db.collection("loop_snapshots").document(doc_id).set(payload)
        logger.info(f"Loop snapshot saved → Firestore loop_snapshots/{doc_id}")
    except Exception as e:
        logger.error(f"Failed to save loop snapshot: {e}")


def _save_final_menu(turn_id: int, menu: List[Dict], inventory: Dict[str, int]) -> None:
    """
    Saves the final committed menu and inventory to Firestore.
    Collection : final_menu
    Document   : turn_{N}
    """
    payload = {
        "turn_id": turn_id,
        "menu": menu,
        "inventory_at_commit": inventory,
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.collection("final_menu").document(f"turn_{turn_id}").set(payload)
        logger.info(f"Final menu saved → Firestore final_menu/turn_{turn_id}")
    except Exception as e:
        logger.error(f"Failed to save final menu: {e}")


# ── Inventory helpers ─────────────────────────────────────────────────────────
async def _refresh_inventory(memory: GameMemory) -> Dict[str, int]:
    """
    Fetches the current restaurant state from the API and updates memory.
    Returns the up-to-date inventory dict.
    """
    try:
        info = await api.get_my_restaurant()
        if info:
            raw_inventory = info.get("inventory", info.get("inventario", {}))
            # Normalize: inventory may be {name: qty} or [{name, quantity}]
            if isinstance(raw_inventory, list):
                inventory = {item["name"]: item["quantity"] for item in raw_inventory}
            else:
                inventory = raw_inventory
            memory.state["current_turn_state"]["inventory"] = inventory
            memory.save()
            return inventory
    except Exception as e:
        logger.warning(f"Could not refresh inventory from API: {e}")
    return memory.get_current_state().get("inventory", {})


def _apply_delta(inventory: Dict[str, int], delta: Dict[str, int]) -> Dict[str, int]:
    """Applies inventory_delta to the current inventory snapshot."""
    updated = dict(inventory)
    for ingredient, change in delta.items():
        updated[ingredient] = max(0, updated.get(ingredient, 0) + change)
    return updated


# ── Menu extraction ───────────────────────────────────────────────────────────
def _extract_menu_items(shopping_list: Dict[str, int], recipes: Dict) -> List[Dict]:
    """
    Builds a simple menu list from the Chef's output (recipes we can cook).
    Returns list of {name, price} for save_menu MCP call.
    Pricing: base prestige * 10, floored at 10 credits.
    """
    menu_items = []
    for recipe_name, recipe_data in recipes.items():
        if isinstance(recipe_data, dict):
            prestige = recipe_data.get("prestige", 0)
            price = max(10, prestige * 10)
            menu_items.append({"name": recipe_name, "price": price})
    return menu_items


# ── Main loop ─────────────────────────────────────────────────────────────────
async def run_waiting_loop(
    memory: GameMemory,
    target_audience: str = "Galactic Explorer",
    max_iterations: int = 3,
) -> Dict:
    """
    Iterative Chef → Market Scanner → Trader loop for the WAITING phase.

    Parameters
    ----------
    memory           : shared GameMemory
    target_audience  : customer target for this turn
    max_iterations   : safety cap to avoid runaway loops

    Returns
    -------
    dict with keys:
        menu          : list[dict]    — final menu items ({name, price})
        inventory     : dict[str,int] — final inventory snapshot
        iterations    : int           — how many iterations ran
        turn_id       : int
    """
    # Lazy imports to avoid circular deps and allow commented-out usage in main.py
    from chef.chef_agent import plan_daily_menu
    from market_scanner.market_scanner_agent import scan_market
    from trader.trader_agent import execute_trades

    turn_id: int = memory.get_current_state().get("turn_id", 0)
    logger.info(f"=== WAITING LOOP START | turn={turn_id} | audience={target_audience} ===")

    # Fresh inventory from API before starting
    inventory = await _refresh_inventory(memory)
    final_menu: List[Dict] = []
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        logger.info(f"--- Loop iteration {iteration}/{max_iterations} ---")

        # ── 1. CHEF: plan menu & generate shopping list ────────────────────────
        logger.info("[LOOP] Chef planning menu...")
        markdown_plan, shopping_list = plan_daily_menu(target_audience)
        logger.info(f"[LOOP] Shopping list: {shopping_list}")

        # Build proposed menu from recipes in memory
        proposed_menu = _extract_menu_items(shopping_list, memory.get_recipes())
        final_menu = proposed_menu  # updated each iteration

        # ── 2. MARKET SCANNER: scan for opportunities ──────────────────────────
        logger.info("[LOOP] Market Scanner scanning...")
        _, alerts = await asyncio.to_thread(scan_market, shopping_list)
        actionable = [a for a in alerts if a.get("urgency") in ("high", "medium")]
        logger.info(f"[LOOP] {len(actionable)} actionable alerts.")

        # ── 3. Save snapshot BEFORE trading ───────────────────────────────────
        _save_loop_snapshot(
            turn_id=turn_id,
            iteration=iteration,
            inventory=inventory,
            menu=proposed_menu,
            shopping_list=shopping_list,
            alerts_count=len(actionable),
            delta={},
        )

        # ── 4. TRADER: execute trades ─────────────────────────────────────────
        if not actionable:
            logger.info("[LOOP] No actionable alerts — loop converged.")
            break

        logger.info("[LOOP] Trader executing trades...")
        _, inventory_delta = await execute_trades(
            alerts=actionable,
            turn_id=turn_id,
            iteration=iteration,
        )

        # ── 5. Update inventory in memory ─────────────────────────────────────
        if inventory_delta:
            inventory = _apply_delta(inventory, inventory_delta)
            memory.state["current_turn_state"]["inventory"] = inventory
            memory.save()
            logger.info(f"[LOOP] Inventory updated: delta={inventory_delta}")

            # Save updated snapshot AFTER trading
            _save_loop_snapshot(
                turn_id=turn_id,
                iteration=iteration,
                inventory=inventory,
                menu=proposed_menu,
                shopping_list=shopping_list,
                alerts_count=len(actionable),
                delta=inventory_delta,
            )
        else:
            logger.info("[LOOP] No inventory changes — loop converged.")
            break

    # ── 6. Push final menu to server ──────────────────────────────────────────
    logger.info(f"[LOOP] Pushing final menu ({len(final_menu)} items) to server...")
    try:
        result = await api.call_mcp_tool("save_menu", {"items": final_menu})
        if result.get("result", {}).get("isError"):
            logger.error(f"[LOOP] save_menu failed: {result}")
        else:
            logger.info("[LOOP] Menu saved to server successfully.")
    except Exception as e:
        logger.error(f"[LOOP] save_menu exception: {e}")

    # ── 7. Persist final committed menu + inventory ────────────────────────────
    _save_final_menu(turn_id=turn_id, menu=final_menu, inventory=inventory)

    logger.info(f"=== WAITING LOOP END | iterations={iteration} ===")
    return {
        "menu": final_menu,
        "inventory": inventory,
        "iterations": iteration,
        "turn_id": turn_id,
    }
