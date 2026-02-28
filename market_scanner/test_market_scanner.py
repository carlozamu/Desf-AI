"""
market_scanner/test_market_scanner.py
--------------------------------------
Standalone test for the Market Scanner Agent.
Run from project root: python -m market_scanner.test_market_scanner
"""
import os
from dotenv import load_dotenv

load_dotenv()

from memory import GameMemory
from market_scanner import market_scanner_agent

# Mock shopping list (as if it came from the Chef Agent)
MOCK_SHOPPING_LIST = {
    "Radici di Gravità": 5,
    "Foglie di Nebulosa": 10,
    "Essenza di Tachioni": 5,
    "Plasma Vitale": 5,
    "Sale Temporale": 5,
}


def main():
    print("Initializing GameMemory...")
    mem = GameMemory()

    print("Binding memory to Market Scanner Agent...")
    market_scanner_agent.init_market_scanner(mem)

    print("\n" + "=" * 60)
    print("  Market Scanner Agent — Running analysis")
    print("=" * 60)
    markdown_report, alerts = market_scanner_agent.scan_market(MOCK_SHOPPING_LIST)

    print(markdown_report)
    print(f"\n>>> Parsed alerts ({len(alerts)} total):")
    for alert in alerts:
        urgency_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(alert.get("urgency", ""), "⚪")
        print(
            f"  {urgency_icon} [{alert.get('type')}] {alert.get('ingredient')} "
            f"x{alert.get('quantity')} @ {alert.get('target_price')} — {alert.get('reason')}"
        )


if __name__ == "__main__":
    main()
