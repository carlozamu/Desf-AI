import asyncio
import os
import sys
from memory import GameMemory
import api

# Carica dal client_template l'infrastruttura SSE di base
import client_template as sse_client

# ── Chef Agent ────────────────────────────────────────────────────────────────
# Menu planning + shopping list generation (run during speaking/waiting phase)
from chef.chef_agent import init_chef_agent, plan_daily_menu

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

    # Bind chef agent to the shared memory once at game start
    init_chef_agent(memory)


async def custom_phase_changed(data):
    phase = data.get("phase", "unknown")
    memory.update_phase(phase)

    # Delegate logging to the template handler
    await sse_client.game_phase_changed(data)

    if phase == "speaking":
        # ── CHEF AGENT ──────────────────────────────────────────────────────
        # Run menu analysis and optimization during the speaking phase.
        # Determines the best dishes to prepare based on target audience,
        # computes quantities, and saves the shopping list to Firestore for
        # the Market Scanner / Trader agents to consume.
        #
        target_audience = "Galactic Explorer"  # TODO: infer from game state
        markdown_plan, shopping_list = plan_daily_menu(target_audience)
        sse_client.log("CHEF", f"Menu plan ready. Shopping list: {shopping_list}")


    elif phase == "closed_bid":
        # TODO: Market Scanner + Trader agents will run here,
        #       consuming the shopping list saved by the Chef Agent.
        pass

    elif phase == "waiting":
        # Inventory updated after bid settlement.
        pass

    elif phase == "serving":
        # Serve dishes based on the menu set during waiting phase.
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
