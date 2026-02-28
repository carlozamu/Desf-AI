"""
pipeline/waiting_loop.py
------------------------
Orchestrates the iterative Chef → Market Scanner → Trader loop
during the WAITING phase.

Goal: minimize ingredient waste and maximize menu profitability.
Terminates when no more useful trades can be made, or max_iterations is reached.

Firestore strategy:
  - Reads the latest run_N document from `runs/` to get the pre-optimized candidate menu (top 10 from OR-Tools).
  - The Chef picks the best subset (≤ 10 dishes) from that candidate menu.
  - After every iteration, the current menu + inventory are saved back to that same run_N document.
  - At exit, the final menu is pushed to the server via save_menu MCP tool.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import firebase_admin
from firebase_admin import firestore
import os

import api
from memory import GameMemory

logger = logging.getLogger(__name__)

MAX_MENU_SIZE = 10

# ── Firebase (idempotent) ─────────────────────────────────────────────────────
if not firebase_admin._apps:
    from firebase_admin import credentials
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    firebase_admin.initialize_app(
        credentials.Certificate(cred_path) if cred_path else None
    )
db = firestore.client()


# ── Run document helpers ──────────────────────────────────────────────────────
def _get_latest_run() -> Tuple[Optional[str], List[Dict]]:
    """
    Finds the highest-numbered run document in Firestore `runs/`.
    Returns (run_doc_id, initial_menu_list).
    initial_menu is a list of {name, price} as saved by run_pipeline.py.
    """
    try:
        runs = list(db.collection("runs").stream())
        if not runs:
            logger.warning("No run documents found in Firestore.")
            return None, []

        # Sort by numeric part of the ID: run_1, run_2, ...
        def run_num(doc):
            try:
                return int(doc.id.split("_")[1])
            except (IndexError, ValueError):
                return 0

        latest = sorted(runs, key=run_num)[-1]
        data = latest.to_dict()
        initial_menu = data.get("initial_menu", [])
        logger.info(f"Latest run: {latest.id} — {len(initial_menu)} candidate dishes")
        return latest.id, initial_menu
    except Exception as e:
        logger.error(f"Failed to read latest run from Firestore: {e}")
        return None, []


def _save_to_run(
    run_doc_id: str,
    menu: List[Dict],
    inventory: Dict[str, int],
    iteration: int,
    turn_id: int,
) -> None:
    """
    Saves the current menu + inventory back to the run_N document.
    Updates these fields on each iteration so the DB always has the latest state.
    """
    if not run_doc_id:
        logger.warning("No run_doc_id — skipping Firestore save.")
        return

    payload = {
        "current_menu": menu,
        "current_inventory": inventory,
        "last_iteration": iteration,
        "turn_id": turn_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.collection("runs").document(run_doc_id).set(payload, merge=True)
        logger.info(f"Run updated → Firestore runs/{run_doc_id} (iter {iteration})")
    except Exception as e:
        logger.error(f"Failed to save to Firestore runs/{run_doc_id}: {e}")


def _save_final_to_run(
    run_doc_id: str,
    menu: List[Dict],
    inventory: Dict[str, int],
    turn_id: int,
    iterations: int,
) -> None:
    """Marks the run as finalized with the committed menu and inventory."""
    if not run_doc_id:
        return
    payload = {
        "status": "final",
        "final_menu": menu,
        "final_inventory": inventory,
        "turn_id": turn_id,
        "total_iterations": iterations,
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.collection("runs").document(run_doc_id).set(payload, merge=True)
        logger.info(f"Final state committed → Firestore runs/{run_doc_id}")
    except Exception as e:
        logger.error(f"Failed to save final state: {e}")


# ── Menu helpers ──────────────────────────────────────────────────────────────
def _select_menu_from_candidates(
    shopping_list: Dict[str, int],
    candidate_menu: List[Dict],
    all_recipes: Dict,
) -> List[Dict]:
    """
    Selects up to MAX_MENU_SIZE dishes for the final menu.

    Priority order:
    1. Recipes the Chef chose (i.e. their ingredients appear in the shopping_list)
       that are also in the candidate_menu from the optimizer.
    2. Fill remaining slots with the top-scored candidates from the run.

    Each item: {name: str, price: int}
    """
    candidate_names = {item["name"]: item for item in candidate_menu}
    candidate_prices = {item["name"]: item.get("price", 100) for item in candidate_menu}

    # Determine which recipes the Chef was shopping for
    # (any recipe whose ingredients intersect with the shopping list)
    chef_chosen_names = set()
    shopping_ingredients = set(shopping_list.keys())
    for recipe_name, recipe_data in all_recipes.items():
        if isinstance(recipe_data, dict):
            recipe_ingredients = set(recipe_data.get("ingredients", {}).keys())
            if recipe_ingredients & shopping_ingredients:
                chef_chosen_names.add(recipe_name)

    # 1st priority: Chef choices that are in the candidate list (optimizer vetted)
    selected = [
        {"name": name, "price": candidate_prices[name]}
        for name in chef_chosen_names
        if name in candidate_names
    ][:MAX_MENU_SIZE]

    # 2nd priority: fill with remaining candidates up to MAX_MENU_SIZE
    if len(selected) < MAX_MENU_SIZE:
        selected_names = {item["name"] for item in selected}
        for item in candidate_menu:
            if len(selected) >= MAX_MENU_SIZE:
                break
            if item["name"] not in selected_names:
                selected.append({"name": item["name"], "price": item.get("price", 100)})

    logger.info(f"Selected {len(selected)} menu items (cap={MAX_MENU_SIZE})")
    return selected


# ── Inventory helpers ─────────────────────────────────────────────────────────
async def _refresh_inventory(memory: GameMemory) -> Dict[str, int]:
    """Fetches current inventory from the API and updates memory."""
    try:
        info = await api.get_my_restaurant()
        if info:
            raw_inventory = info.get("inventory", info.get("inventario", {}))
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
    """Applies an inventory delta (BUY=+, SELL=-)."""
    updated = dict(inventory)
    for ingredient, change in delta.items():
        updated[ingredient] = max(0, updated.get(ingredient, 0) + change)
    return updated


# ── Main loop ─────────────────────────────────────────────────────────────────
async def run_waiting_loop(
    memory: GameMemory,
    target_audience: str = "Galactic Explorer",
    max_iterations: int = 3,
) -> Dict:
    """
    Iterative Chef → Market Scanner → Trader loop for the WAITING phase.

    - Reads the latest run_N document for the candidate menu (≤10 dishes).
    - Chef picks the best subset, Market Scanner finds BUY/SELL opportunities,
      Trader executes them.
    - Menu + inventory saved to Firestore runs/run_N after every iteration.
    - Final menu (≤10 dishes) pushed to server via save_menu MCP.

    Returns dict: {menu, inventory, iterations, turn_id, run_doc_id}
    """
    from chef.chef_agent import plan_daily_menu
    from market_scanner.market_scanner_agent import scan_market
    from trader.trader_agent import execute_trades

    turn_id: int = memory.get_current_state().get("turn_id", 0)
    logger.info(f"=== WAITING LOOP START | turn={turn_id} | audience={target_audience} ===")

    # ── Load candidate menu from the latest optimizer run ─────────────────────
    run_doc_id, candidate_menu = _get_latest_run()
    if not candidate_menu:
        logger.warning("No candidate menu from optimizer — using empty fallback.")
        candidate_menu = []

    # Fresh inventory from API
    inventory = await _refresh_inventory(memory)
    final_menu: List[Dict] = candidate_menu[:MAX_MENU_SIZE]  # safe starting menu
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        logger.info(f"--- Loop iteration {iteration}/{max_iterations} ---")

        # ── 1. CHEF: plan menu & generate shopping list ────────────────────────
        logger.info("[LOOP] Chef planning menu...")
        markdown_plan, shopping_list = plan_daily_menu(target_audience)
        logger.info(f"[LOOP] Shopping list ({len(shopping_list)} items): {list(shopping_list.keys())[:5]}...")

        # Select ≤10 dishes from the optimizer candidates using Chef's guidance
        proposed_menu = _select_menu_from_candidates(
            shopping_list=shopping_list,
            candidate_menu=candidate_menu,
            all_recipes=memory.get_recipes(),
        )
        final_menu = proposed_menu

        # ── 2. MARKET SCANNER: scan for opportunities ──────────────────────────
        logger.info("[LOOP] Market Scanner scanning...")
        _, alerts = await asyncio.to_thread(scan_market, shopping_list)
        actionable = [a for a in alerts if a.get("urgency") in ("high", "medium")]
        logger.info(f"[LOOP] {len(actionable)} actionable alerts.")

        # ── 3. Save current state to Firestore (before trading) ───────────────
        _save_to_run(
            run_doc_id=run_doc_id,
            menu=proposed_menu,
            inventory=inventory,
            iteration=iteration,
            turn_id=turn_id,
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
            logger.info(f"[LOOP] Inventory updated: {inventory_delta}")

            # Save updated state to Firestore after trading
            _save_to_run(
                run_doc_id=run_doc_id,
                menu=proposed_menu,
                inventory=inventory,
                iteration=iteration,
                turn_id=turn_id,
            )
        else:
            logger.info("[LOOP] No inventory changes — loop converged.")
            break

    # ── 6. Push final menu to server ──────────────────────────────────────────
    logger.info(f"[LOOP] Pushing final menu ({len(final_menu)} dishes) to server...")
    try:
        result = await api.call_mcp_tool("save_menu", {"items": final_menu})
        if result.get("result", {}).get("isError"):
            logger.error(f"[LOOP] save_menu failed: {result}")
        else:
            logger.info("[LOOP] Menu saved to server successfully.")
    except Exception as e:
        logger.error(f"[LOOP] save_menu exception: {e}")

    # ── 7. Persist final state to Firestore ───────────────────────────────────
    _save_final_to_run(
        run_doc_id=run_doc_id,
        menu=final_menu,
        inventory=inventory,
        turn_id=turn_id,
        iterations=iteration,
    )

    logger.info(f"=== WAITING LOOP END | iterations={iteration} | menu={len(final_menu)} dishes ===")
    return {
        "menu": final_menu,
        "inventory": inventory,
        "iterations": iteration,
        "turn_id": turn_id,
        "run_doc_id": run_doc_id,
    }
