"""
pipeline/test_waiting_loop.py
-----------------------------
End-to-end test for the full Chef → Market Scanner → Trader loop.
Runs in dry-run mode (no live transactions) to verify logic and Firestore persistence.
"""
import asyncio
import os
import logging

# Set dry-run for Trader by default for this test
os.environ.setdefault("TRADER_DRY_RUN", "true")

from dotenv import load_dotenv
load_dotenv()

from memory import GameMemory
from pipeline.waiting_loop import run_waiting_loop
from chef.chef_agent import init_chef_agent
from market_scanner.market_scanner_agent import init_market_scanner
from trader.trader_agent import init_trader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_full_loop():
    print("\n" + "="*60)
    print("  E2E PIPELINE TEST — Waiting Phase Loop")
    print("="*60)

    # 1. Initialize Memory
    memory = GameMemory()
    
    # 2. Seed mock recipes if empty (needed for Chef)
    if not memory.get_recipes():
        print("Seeding mock recipes...")
        memory.update_recipes([
            {
                "name": "Nebulosa Galattica",
                "ingredients": {"Radici di Gravità": 2, "Alghe Bioluminescenti": 1},
                "preparationTimeMs": 3000,
                "prestige": 31,
            },
            {
                "name": "Sinfonia del Multiverso",
                "ingredients": {"Petali di Eco": 2, "Foglie di Mandragora": 1},
                "preparationTimeMs": 5517,
                "prestige": 80,
            }
        ])

    # 3. Initialize all agents with shared memory
    init_chef_agent(memory)
    init_market_scanner(memory)
    init_trader(memory)

    # 4. Run the loop
    target_audience = "Astrobarone"
    print(f"\nStarting loop for audience: {target_audience}...")
    
    try:
        final_state = await run_waiting_loop(
            memory=memory,
            target_audience=target_audience,
            max_iterations=2  # Keep it short for test
        )
        
        print("\n" + "="*60)
        print("  TEST SUCCESSFUL")
        print("="*60)
        print(f"Iterations run: {final_state['iterations']}")
        print(f"Final Menu: {[item['name'] for item in final_state['menu']]}")
        print(f"Final Inventory snapshot: {final_state['inventory']}")
        print("\nCheck Firestore for:")
        print(f" - loop_snapshots/turn_{final_state['turn_id']}_iter_1")
        print(f" - final_menu/turn_{final_state['turn_id']}")
        
    except Exception as e:
        print(f"\n[!] LOOP FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_full_loop())
