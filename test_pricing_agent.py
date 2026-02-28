"""
test_pricing_agent.py
─────────────────────
Test standalone per il Price Analyst Agent.
Modifica TURN_ID qui sotto e lancia:

    python test_pricing_agent.py
"""

import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

# ─── Parametri di test ────────────────────────────────────────────────────────
TURN_ID      = 24    # ← turno corrente (usato solo come label nel prompt)
MAX_HISTORY  = 5     # ← quante run storiche analizzare (default = _MAX_HISTORY_RUNS)
# ─────────────────────────────────────────────────────────────────────────────

from price_analyst.agent import get_bidding_prices, _MAX_HISTORY_RUNS, AGENT_DIR


async def main() -> None:
    TEAM_API_KEY = os.getenv("SERVER_API_KEY")
    CRED_PATH    = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")

    print("=" * 60)
    print("  🤖 Price Analyst Agent — TEST")
    print(f"  TURN_ID    : {TURN_ID}")
    print(f"  MAX_HISTORY: {MAX_HISTORY} run (costante = {_MAX_HISTORY_RUNS})")
    print(f"  CRED_PATH  : {CRED_PATH}")
    print("=" * 60)

    if not TEAM_API_KEY:
        print("❌  SERVER_API_KEY non trovata nel .env — impossibile procedere.")
        return

    try:
        print("\n⏳ Avvio agente... (log completi su price_analyst/execution_log_turn.txt)\n")

        # get_bidding_prices è sincrono → lo eseguiamo in un thread separato
        result = await asyncio.to_thread(get_bidding_prices, TURN_ID, MAX_HISTORY)

        print("\n" + "=" * 60)
        if result:
            print(f"✅ Prezzi generati ({len(result)} item):")
            for item, price in sorted(result.items()):
                print(f"   {item:45s} → {price} cr")
            print(f"\n📄 Log completo → {AGENT_DIR}/execution_log_turn.txt")
            print(f"🔥 Dati salvati su Firebase → runs/run_{TURN_ID}/bids_prices")
        else:
            print("⚠️  Nessun prezzo generato. Controlla il log per i dettagli.")

    except Exception as exc:
        print(f"\n❌ Eccezione: {exc}")


if __name__ == "__main__":
    asyncio.run(main())