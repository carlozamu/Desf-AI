import asyncio
import os
import sys
from memory import GameMemory
import api

# Carica dal client_template l'infrastruttura SSE di base
import client_template as sse_client

# ── Agent imports (uncomment to activate) ────────────────────────────────────
# from chef.chef_agent import init_chef_agent, plan_daily_menu
# from market_scanner.market_scanner_agent import init_market_scanner, scan_market
# from trader.trader_agent import init_trader
# from pipeline.waiting_loop import run_waiting_loop

memory = GameMemory()

# ── Startup ───────────────────────────────────────────────────────────────────
async def fetch_initial_data():
    """Fetch recipes and restaurant info at startup and store in memory."""
    sse_client.log("API", "Fetching recipes from server...")
    recipes = await api.get_recipes()
    if recipes:
        memory.update_recipes(recipes)
        sse_client.log("API", f"Loaded {len(recipes)} recipes into memory.")
    else:
        sse_client.log("ERROR", "Could not fetch recipes.")

    sse_client.log("API", "Fetching own restaurant info...")
    info = await api.get_my_restaurant()
    if info:
        sse_client.log("API", f"Restaurant Info: {info}")


# ── SSE Phase Handlers ────────────────────────────────────────────────────────
async def custom_game_started(data):
    turn_id = data.get("turn_id", 0)
    memory.update_turn(turn_id)
    sse_client.log("EVENT", f"Game started, turn_id={turn_id}")
    await fetch_initial_data()

    # Bind all agents to shared memory once at game start
    # init_chef_agent(memory)
    # init_market_scanner(memory)
    # init_trader(memory)


async def custom_phase_changed(data):
    phase = data.get("phase", "unknown")
    memory.update_phase(phase)

    # Delegate logging to the template handler
    await sse_client.game_phase_changed(data)

    if phase == "speaking":
        # ── PRE-SCAN: Chef + Market Scanner (first pass before auction) ──────
        # Run early to gather bid intelligence before the closed_bid phase.
        #
        # target_audience = "Galactic Explorer"  # TODO: infer from game state
        # _, shopping_list = plan_daily_menu(target_audience)
        # sse_client.log("CHEF", f"Shopping list ready: {shopping_list}")
        # _, alerts = scan_market(shopping_list)
        # sse_client.log("MARKET_SCANNER", f"{len(alerts)} alerts pre-auction.")
        pass

    elif phase == "closed_bid":
        # Auction bids are submitted here (handled by a separate bidder module).
        # The Market Scanner's bid strategy tips inform bid amounts.
        pass

    elif phase == "waiting":
        # ── WAITING LOOP: Chef → Market Scanner → Trader (iterative) ─────────
        # Runs after the auction settles. Iterates until inventory is stable
        # or max_iterations is reached. Goal: minimize ingredient waste.
        #
        # At every iteration:
        #   - Chef re-plans the menu with the updated inventory
        #   - Market Scanner scans for live BUY/SELL opportunities
        #   - Trader executes transactions via MCP tools
        #   - Inventory + menu snapshot saved to Firestore each round
        #
        # On exit → final menu pushed to server via save_menu MCP tool
        #         → final state saved to Firestore (final_menu/turn_{N})
        #
        # target_audience = "Galactic Explorer"  # TODO: infer from game state
        # final_state = await run_waiting_loop(
        #     memory=memory,
        #     target_audience=target_audience,
        #     max_iterations=3,
        # )
        # sse_client.log("LOOP", (
        #     f"Loop done in {final_state['iterations']} iter(s). "
        #     f"Menu: {len(final_state['menu'])} dishes. "
        #     f"Final inventory: {final_state['inventory']}"
        # ))
        pass

    elif phase == "serving":
        # Dishes are served here using the menu committed during waiting.
        # Serving logic (prepare_dish / serve_dish) goes here.
        pass


# Override template handlers with memory-aware versions
sse_client.EVENT_HANDLERS["game_started"] = custom_game_started
sse_client.EVENT_HANDLERS["game_phase_changed"] = custom_phase_changed


async def main():
    if not api.TEAM_ID or not api.TEAM_API_KEY:
        sse_client.log("ERROR", "Please set TEAM_ID and TEAM_API_KEY environment variables.")
        sys.exit(1)

    sse_client.log("INIT", "Starting Hackapizza Memory Agent...")

    # Pre-fetch data before connecting to SSE
    await fetch_initial_data()

    # Start SSE listener (blocking until connection drops)
    await sse_client.listen_once_and_exit_on_drop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sse_client.log("INIT", "Client stopped by user")
