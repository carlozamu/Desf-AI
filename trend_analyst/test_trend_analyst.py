"""
trend_analyst/test_trend_analyst.py
-------------------------------------
Standalone test for the Trend Analyst Agent.

Run from project root with venv active:
    python -m trend_analyst.test_trend_analyst [turn_id]

If no turn_id is provided, it will try to detect the current turn from
the game API, or fall back to turn 1.
"""
import asyncio
import os
import sys
import logging

# Set up basic logging so we see agent steps
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from dotenv import load_dotenv
load_dotenv()

import api
from trend_analyst.trend_analyst_agent import run_trend_analysis


STATE_FILE = os.path.join(os.path.dirname(__file__), ".last_turn_id")

def load_last_turn() -> int:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return int(f.read().strip())
        except ValueError:
            pass
    return 0

def save_last_turn(turn_id: int):
    with open(STATE_FILE, "w") as f:
        f.write(str(turn_id))


async def _get_current_turn() -> int:
    """Try to infer the current turn ID from the game state."""
    try:
        restaurants = await api.get_restaurants()
        if restaurants and isinstance(restaurants, list) and len(restaurants) > 0:
            turn_id = restaurants[0].get("currentTurnId", restaurants[0].get("turn_id", None))
            if turn_id:
                return int(turn_id)
    except Exception as e:
        logging.warning(f"Could not auto-detect turn: {e}")
    return 0


async def get_input_with_timeout(prompt: str, timeout: int = 60) -> str:
    print(prompt, end="", flush=True)
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, sys.stdin.readline), timeout=timeout)
    except asyncio.TimeoutError:
        print("\n[Timeout reached]")
        return ""

async def main():
    last_saved_turn = load_last_turn()
    
    print("Auto-detecting from API...")
    auto_turn_id = await _get_current_turn()
    
    if auto_turn_id > 0:
        print(f"Auto-detected active turn_id: {auto_turn_id}")
        turn_id = auto_turn_id
    elif last_saved_turn > 0:
        print(f"No active turn detected. Using last successful turn_id: {last_saved_turn}")
        turn_id = last_saved_turn
    else:
        turn_id = 0

    while True:
        if turn_id == 0 or (auto_turn_id <= 0 and last_saved_turn <= 0):
            print("\nCould not confidently detect an active turn from the API or state file.")
            user_input = await get_input_with_timeout("Enter the turn_id you want to analyze (or wait 60s to abort): ", 60)
            user_input = user_input.strip()
            if user_input.isdigit():
                turn_id = int(user_input)
            else:
                print("No valid input provided. Exiting.")
                return

        print("\n" + "=" * 70)
        print("  🔭 HACKAPIZZA 2.0 — TREND ANALYST AGENT")
        print("=" * 70)
        print(f"  Analyzing turn: {turn_id}")
        previous_turns = []
        if turn_id > 1:
            previous_turns = list(range(max(1, turn_id - 2), turn_id))
            print(f"  Historical turns for context: {previous_turns}")
        print("=" * 70 + "\n")
        print("Running analysis (this may take 30-60 seconds)...\n")

        report = run_trend_analysis(turn_id=turn_id, previous_turns=previous_turns)

        print("\n" + "=" * 70)
        print("  📊 ANALYSIS REPORT")
        print("=" * 70)
        print(report)
        print("\n" + "=" * 70)

        # Basic heuristic to check if the run actually succeeded or hit api errors
        if "Analysis failed:" in report or "⚠️ No bid history" in report or "Server returned no data" in report:
            print("  ⚠️ Analysis encountered missing data or errors for this turn.")
            save_last = False
        else:
            print("  ✅ Analysis complete. Use these insights for your next turn!")
            save_last = True

        print("=" * 70)

        if save_last:
            save_last_turn(turn_id)
            break
        else:
            print("\nAnalysis was incomplete. Please verify the turn_id is correct and the server is running.")
            user_input = await get_input_with_timeout(f"Enter a new turn_id to retry, or wait 60s to exit: ", 60)
            user_input = user_input.strip()
            if user_input.isdigit():
                turn_id = int(user_input)
            else:
                print("Exiting.")
                break

if __name__ == "__main__":
    asyncio.run(main())
