"""
menu_tracker.py
───────────────
Script statico da invocare a fine turno.

Salva su Firebase i menu di tutti i ristoranti nella collezione "menus":
  collezione : menus
  documento  : run_<run_id>_turn_<turn_id>
  struttura  :
    {
      "run_id":   <str>,
      "turn_id":  <int>,
      "timestamp": <ISO8601>,
      "restaurants": {
        "<restaurant_id>": {
          "restaurant_name": <str>,
          "menu": [
            {"name": <str>, "price": <float>},
            ...
          ]
        },
        ...
      }
    }

Utilizzo standalone:
  python serving/menu_tracker.py --turn_id <N> [--run_id <label>]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import aiohttp
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")

load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

BASE_URL     = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
TEAM_API_KEY = os.getenv("SERVER_API_KEY", "")
CRED_PATH    = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(_ROOT, "firebase_credentials.json"),
)


def _require_api_key() -> None:
    if not TEAM_API_KEY:
        sys.exit("❌  SERVER_API_KEY non trovata nel file .env")


# ─────────────────────────────────────────────────────────────────────────────
# Firebase
# ─────────────────────────────────────────────────────────────────────────────

def init_firebase() -> firestore.Client | None:
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as exc:
        print(f"⚠️  Errore Firebase: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Chiamate API
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch(session: aiohttp.ClientSession, url: str, retries: int = 3):
    """GET → JSON con retry su 429."""
    headers = {"x-api-key": TEAM_API_KEY}
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429:
                    wait = 1.5 * (attempt + 1)
                    print(f"   ⏳ 429 su {url.split('/')[-1]} — retry tra {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                print(f"   ⚠️  HTTP {resp.status} — {url}")
                return None
        except aiohttp.ClientError as exc:
            print(f"   ⚠️  Errore di rete: {exc}")
            return None
    print(f"   ❌ Rinuncio dopo {retries} tentativi: {url}")
    return None


async def get_all_restaurants(session: aiohttp.ClientSession) -> list[dict]:
    data = await _fetch(session, f"{BASE_URL}/restaurants")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("restaurants", [])
    return []


async def get_menu(session: aiohttp.ClientSession, restaurant_id) -> list[dict]:
    """Restituisce la lista di voci menu [{name, price}, ...] per un ristorante."""
    data = await _fetch(session, f"{BASE_URL}/restaurant/{restaurant_id}/menu")
    if isinstance(data, dict):
        data = data.get("items", [])
    if isinstance(data, list):
        return [
            {"name": item.get("name", ""), "price": item.get("price")}
            for item in data
            if isinstance(item, dict) and item.get("name")
        ]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Logica principale
# ─────────────────────────────────────────────────────────────────────────────

_MENU_DELAY = 0.4  # secondi tra ogni richiesta menu per evitare 429


async def collect_all_menus() -> dict[str, dict]:
    """
    Fetcha il menu di tutti i ristoranti sequenzialmente.
    Restituisce un dict keyed by restaurant_id (str).
    """
    async with aiohttp.ClientSession() as session:
        restaurants = await get_all_restaurants(session)
        if not restaurants:
            print("⚠️  Nessun ristorante trovato.")
            return {}

        print(f"🍕 Trovati {len(restaurants)} ristoranti.")

        compiled: dict[str, dict] = {}
        for idx, restaurant in enumerate(restaurants):
            rest_id   = str(restaurant.get("id", "unknown"))
            rest_name = restaurant.get("name", f"Restaurant {rest_id}")

            menu = await get_menu(session, restaurant.get("id"))
            compiled[rest_id] = {
                "restaurant_name": rest_name,
                "menu": menu,
            }
            print(f"  ✅ {rest_name} (id={rest_id}): {len(menu)} voci menu")

            if idx < len(restaurants) - 1:
                await asyncio.sleep(_MENU_DELAY)

        return compiled


def save_to_firebase(
    db: firestore.Client,
    turn_id: int,
    run_id: str,
    restaurants_data: dict[str, dict],
) -> bool:
    doc_id  = f"run_{run_id}_turn_{turn_id}"
    payload = {
        "run_id":      run_id,
        "turn_id":     turn_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "restaurants": restaurants_data,
    }
    try:
        db.collection("menus").document(doc_id).set(payload)
        print(f"💾 Dati salvati su Firebase → menus/{doc_id}")
        return True
    except Exception as exc:
        print(f"❌  Errore salvataggio Firebase: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main(turn_id: int, run_id: str) -> None:
    _require_api_key()
    print(f"\n📋 Menu Tracker — turn={turn_id}  run={run_id}")
    print(f"   Server : {BASE_URL}\n")

    db = init_firebase()
    if db is None:
        sys.exit("❌  Firebase non disponibile.")

    restaurants_data = await collect_all_menus()
    if not restaurants_data:
        print("⚠️  Nessun dato raccolto.")
        return

    save_to_firebase(db, turn_id, run_id, restaurants_data)
    print("\n✅ Menu Tracker completato.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Salva su Firebase i menu di tutti i ristoranti a fine turno."
    )
    parser.add_argument("--turn_id", type=int, required=True)
    parser.add_argument("--run_id",  type=str, default="main")
    args = parser.parse_args()
    asyncio.run(main(turn_id=args.turn_id, run_id=args.run_id))
