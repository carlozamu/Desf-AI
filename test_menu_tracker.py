"""
test_menu_tracker.py
────────────────────
Script di test per il menu tracker.
Modifica le variabili qui sotto e lancia:

    python test_menu_tracker.py
"""

import asyncio
import os
import sys

# ─── Parametri di test ────────────────────────────────────────────────────────

TURN_ID = 24          # ← turno di riferimento (usato solo per il doc_id Firebase)
RUN_ID  = "test"     # ← etichetta della run

# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from serving.menu_tracker import (
    collect_all_menus,
    save_to_firebase,
    init_firebase,
    TEAM_API_KEY,
    BASE_URL,
)


async def run_test() -> None:
    print("=" * 60)
    print(f"  📋 Menu Tracker — TEST")
    print(f"  TURN_ID : {TURN_ID}")
    print(f"  RUN_ID  : {RUN_ID}")
    print(f"  Server  : {BASE_URL}")
    print("=" * 60)

    if not TEAM_API_KEY:
        print("❌  SERVER_API_KEY non trovata nel .env")
        return

    # 1. Raccolta menu
    print("\n📡 Fetching menu di tutti i ristoranti...")
    restaurants_data = await collect_all_menus()

    if not restaurants_data:
        print("\n⚠️  Nessun dato ricevuto.")
        return

    # 2. Riepilogo locale
    print("\n📋 Riepilogo menu:")
    for rest_id, info in restaurants_data.items():
        name  = info["restaurant_name"]
        items = info["menu"]
        if not items:
            continue   # salta ristoranti con menu vuoto
        print(f"\n  🍽️  {name} (id={rest_id})")
        for item in items:
            price_str = f"€{item['price']:.2f}" if item.get("price") is not None else "N/D"
            print(f"     • {item['name']:50s}  {price_str}")

    # 3. Salvataggio Firebase
    print("\n🔥 Inizializzo Firebase...")
    db = init_firebase()
    if db is None:
        print("❌  Firebase non disponibile. Salvataggio saltato.")
        return

    ok = save_to_firebase(db, TURN_ID, RUN_ID, restaurants_data)
    if ok:
        print(f"\n✅ Dati salvati → menus/run_{RUN_ID}_turn_{TURN_ID}")
    else:
        print("\n❌ Errore durante il salvataggio su Firebase.")


if __name__ == "__main__":
    asyncio.run(run_test())
