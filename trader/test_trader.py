"""
trader/test_trader.py
----------------------
Standalone dry-run test for the Trader Agent.
Run from project root: TRADER_DRY_RUN=true python -m trader.test_trader
"""
import asyncio
import os
os.environ.setdefault("TRADER_DRY_RUN", "true")

from dotenv import load_dotenv
load_dotenv()

from memory import GameMemory
from trader import trader_agent

# Mock alerts as if they came from the Market Scanner
MOCK_ALERTS = [
    {
        "type": "SELL",
        "ingredient": "Pomodoro Spaziale",
        "quantity": 10,
        "target_price": 9.0,
        "market_entry_id": None,
        "urgency": "medium",
        "reason": "Surplus stock — will expire if not sold.",
    },
    {
        "type": "BUY",
        "ingredient": "Radici di Gravità",
        "quantity": 5,
        "target_price": 12.0,
        "market_entry_id": 1,
        "urgency": "high",
        "reason": "Critical chef requirement; market SELL available.",
    },
    {
        "type": "BUY",
        "ingredient": "Foglie di Nebulosa",
        "quantity": 5,
        "target_price": 18.5,
        "market_entry_id": 2,
        "urgency": "high",
        "reason": "Half of required amount available.",
    },
]


async def main():
    print("Initializing GameMemory...")
    mem = GameMemory()

    print("Binding memory to Trader Agent...")
    trader_agent.init_trader(mem)

    print("\n" + "=" * 60)
    print("  Trader Agent — Dry Run")
    print("=" * 60)

    markdown_report, inventory_delta = await trader_agent.execute_trades(
        alerts=MOCK_ALERTS, turn_id=0, iteration=1
    )

    print(markdown_report)
    print(f"\n>>> Inventory delta: {inventory_delta}")
    print("  (+ = bought, - = sold)")


if __name__ == "__main__":
    asyncio.run(main())
