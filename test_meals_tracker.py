"""
test_meals_tracker.py
─────────────────────
Script di test per il meals tracker.
Modifica le variabili qui sotto e lancia:

    python test_meals_tracker.py
"""

import asyncio
import os
import sys

# ─── Parametri di test ────────────────────────────────────────────────────────

TURN_ID = 17          # ← turno da interrogare
RUN_ID  = "test"     # ← etichetta della run (usata come parte del doc_id in Firebase)

# ─────────────────────────────────────────────────────────────────────────────

# Aggiungi la root del progetto al path per poter importare i moduli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from serving.meals_tracker import (
    collect_turn_meals,
    save_to_firebase,
    init_firebase,
    TEAM_API_KEY,
    BASE_URL,
)


async def run_test() -> None:
    print("=" * 60)
    print(f"  🧪 Meals Tracker — TEST")
    print(f"  TURN_ID : {TURN_ID}")
    print(f"  RUN_ID  : {RUN_ID}")
    print(f"  Server  : {BASE_URL}")
    print("=" * 60)

    if not TEAM_API_KEY:
        print("❌  SERVER_API_KEY non trovata nel .env — impossibile procedere.")
        return

    # 0. DEBUG: probe raw del menu endpoint su restaurant 7 (ha meals, lo sappiamo)
    import aiohttp as _aio
    _test_id = 7
    print(f"\n🔬 Probe raw GET /restaurant/{_test_id}/menu ...")
    async with _aio.ClientSession() as _s:
        async with _s.get(
            f"{BASE_URL}/restaurant/{_test_id}/menu",
            headers={"x-api-key": TEAM_API_KEY},
        ) as _r:
            _body = await _r.text()
            print(f"   Status : {_r.status}")
            print(f"   Body   : {_body[:500]}")
    print()

    # 1. Raccolta dati dal server
    print("📡 Fetching meals da tutti i ristoranti...")
    restaurants_data = await collect_turn_meals(TURN_ID)

    if not restaurants_data:
        print("\n⚠️  Nessun dato ricevuto dall'API. Controlla turn_id e connettività.")
        return

    # 2. Stampa riepilogo locale (senza salvare su Firebase)
    print("\n📋 Riepilogo raccolta:")
    for rest_id, info in restaurants_data.items():
        name    = info["restaurant_name"]
        recipes = info["recipes"]
        print(f"\n  🍽️  {name} (id={rest_id})")
        if recipes:
            for r in recipes:
                price_str = f"€{r['price']:.2f}" if r.get("price") is not None else "N/D"
                print(
                    f"     • {r['recipe_name']:40s} "
                    f"prezzo={price_str:8s}  "
                    f"preparati={r['dishes_prepared']}  "
                    f"venduti={r['dishes_sold']}"
                )
        else:
            print("     (nessun piatto in questo turno)")

    # 3. Salvataggio su Firebase
    print("\n🔥 Inizializzo Firebase...")
    db = init_firebase()
    if db is None:
        print("❌  Firebase non disponibile. Salvataggio saltato.")
        return

    ok = save_to_firebase(db, TURN_ID, RUN_ID, restaurants_data)
    if ok:
        print(f"\n✅ Dati salvati → meals/run_{RUN_ID}_turn_{TURN_ID}")
    else:
        print("\n❌ Errore durante il salvataggio su Firebase.")


if __name__ == "__main__":
    asyncio.run(run_test())
