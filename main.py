import asyncio
import os
import sys
from memory import GameMemory
import api

# Carica dal client_template l'infrastruttura di base (necessario copiare qui i metodi essenziali per non sovrascrivere tutto)
import client_template as sse_client

memory = GameMemory()

async def fetch_initial_data():
    """Recupera le ricette all'avvio e le salva nella memoria"""
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

async def custom_game_started(data):
    turn_id = data.get("turn_id", 0)
    memory.update_turn(turn_id)
    sse_client.log("EVENT", f"Game started, turn id: {turn_id}")
    await fetch_initial_data()

async def custom_phase_changed(data):
    phase = data.get("phase", "unknown")
    memory.update_phase(phase)
    
    # Delegare all'handler originale del template per i log
    await sse_client.game_phase_changed(data)
    
    # Custom logic based on phase
    if phase == "speaking":
        pass
    elif phase == "closed_bid":
        pass
    elif phase == "waiting":
         # In waiting we can check what we won in the bid
         pass
    elif phase == "serving":
        pass

# Override the default handlers with our memory-aware handlers
sse_client.EVENT_HANDLERS["game_started"] = custom_game_started
sse_client.EVENT_HANDLERS["game_phase_changed"] = custom_phase_changed

async def main():
    if not api.TEAM_ID or not api.TEAM_API_KEY:
         sse_client.log("ERROR", "Please set TEAM_ID and TEAM_API_KEY environment variables.")
         sys.exit(1)
         
    sse_client.log("INIT", "Starting Hackapizza Memory Agent...")
    
    # Fetch data prima di connettersi a SSE per sicurezza
    await fetch_initial_data()
    
    # Start SSE listener (bloccante finché non cade la connessione)
    await sse_client.listen_once_and_exit_on_drop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sse_client.log("INIT", "Client stopped by user")
