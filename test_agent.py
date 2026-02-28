import asyncio
import os
import agent
import api
from memory import GameMemory

async def main():
    print("Initializing memory...")
    memory = GameMemory()
    agent.init_agent(memory)
    
    # Load initial recipes just to test memory
    print("Fetching recipes...")
    recipes = await api.get_recipes()
    if recipes:
        memory.update_recipes(recipes)
        print(f"Loaded {len(recipes)} recipes.")
        
    print("Testing Agent Decision Process...")
    prompt = "I just opened the restaurant. Look at my restaurant info and tell me how much balance I have and what recipes I could potentially cook if I had ingredients."
    
    # Needs to run async so we use asyncio.to_thread since agent.run is sync
    try:
        response = await asyncio.to_thread(agent.make_decision, prompt)
        print(f"\n--- Agent Response ---\n{response}\n----------------------")
    except Exception as e:
        print(f"Agent failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
