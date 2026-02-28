"""
meals_tracker.py
────────────────
Script statico da invocare a fine turno.

Funzionamento:
  1. Recupera la lista di tutti i ristoranti dal server.
  2. Per ogni ristorante, chiama GET /meals?turn_id=<id>&restaurant_id=<id>
     e raccoglie:
       - recipe: il nome del piatto/ricetta richiesta
       - executed: True se il piatto è stato servito
  3. Per ogni ristorante aggrega i dati per ricetta:
       - dishes_prepared: quante volte la ricetta è stata preparata (ogni meal è un piatto)
       - dishes_sold: quante volte la ricetta è stata venduta (executed == True)
  4. Salva tutto su Firebase Firestore nella collezione "meals":
       collezione: meals
       documento:  run_<run_id>_turn_<turn_id>
       struttura documento:
         {
           "run_id": <str>,
           "turn_id": <int>,
           "timestamp": <ISO8601>,
           "restaurants": {
             "<restaurant_id>": {
               "restaurant_name": <str>,
               "recipes": [
                 {
                   "recipe_name": <str>,
                   "dishes_prepared": <int>,
                   "dishes_sold": <int>
                 },
                 ...
               ]
             },
             ...
           }
         }

Utilizzo:
  python meals_tracker.py --turn_id <N> [--run_id <run_label>]

Dipendenze (già presenti nel progetto):
  aiohttp, firebase_admin, python-dotenv
"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

# Risolvi i percorsi relativi alla directory del progetto (parent di serving/)
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
    """Usato solo in esecuzione standalone per uscire con un messaggio chiaro."""
    if not TEAM_API_KEY:
        sys.exit("❌  SERVER_API_KEY non trovata nel file .env")


# ─────────────────────────────────────────────────────────────────────────────
# Firebase
# ─────────────────────────────────────────────────────────────────────────────

def init_firebase() -> firestore.Client | None:
    """Inizializza Firebase (una sola volta per processo) e restituisce il client."""
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as exc:
        print(f"⚠️  Errore inizializzazione Firebase: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Chiamate API
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None, retries: int = 3) -> list | dict | None:
    """Helper generico per GET → JSON. Riprova automaticamente su HTTP 429."""
    headers = {"x-api-key": TEAM_API_KEY}
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429:
                    wait = 1.5 * (attempt + 1)  # backoff: 1.5s, 3s, 4.5s
                    print(f"⏳ 429 su {url.split('/')[-1]} — retry tra {wait:.1f}s (tentativo {attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                text = await resp.text()
                print(f"⚠️  HTTP {resp.status} - {url} - {text[:200]}")
                return None
        except aiohttp.ClientError as exc:
            print(f"⚠️  Errore di rete ({url}): {exc}")
            return None
    print(f"❌ Rinuncio dopo {retries} tentativi: {url}")
    return None


async def get_all_restaurants(session: aiohttp.ClientSession) -> list[dict]:
    """Restituisce tutti i ristoranti in gioco."""
    data = await fetch_json(session, f"{BASE_URL}/restaurants")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "restaurants" in data:
        return data["restaurants"]
    return []


async def get_meals_for_restaurant(session: aiohttp.ClientSession, turn_id: int, restaurant_id: int | str) -> list[dict]:
    """Restituisce i pasti di un ristorante per un dato turno."""
    data = await fetch_json(
        session,
        f"{BASE_URL}/meals",
        params={"turn_id": turn_id, "restaurant_id": restaurant_id},
    )
    if isinstance(data, list):
        return data
    return []


async def get_menu_for_restaurant(session: aiohttp.ClientSession, restaurant_id: int | str) -> dict[str, float]:
    """
    Restituisce un dict {dish_name: price} per il ristorante dato.
    Usa GET /restaurant/:id/menu  →  {"items": [{name, price}, ...]}
    """
    data = await fetch_json(session, f"{BASE_URL}/restaurant/{restaurant_id}/menu")
    # L'API ritorna {"items": [...]} non una lista diretta
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        return {}
    return {
        item["name"]: item.get("price", 0.0)
        for item in data
        if isinstance(item, dict) and "name" in item
    }


# ─────────────────────────────────────────────────────────────────────────────
# Logica principale
# ─────────────────────────────────────────────────────────────────────────────

# Candidati per il campo "nome ricetta" nell'oggetto meal (in ordine di priorità)
# 'request' è il campo confermato dall'API di Hackapizza
_RECIPE_FIELD_CANDIDATES = [
    "request", "recipe_name", "recipe", "dish", "dish_name", "name", "orderText", "order_text",
]
import re as _re

_INTOLERANCE_PATTERN = _re.compile(
    r"^i want to eat\s+(.+?)\s*\.\s*i'?m intolerant to\s+.+$",
    _re.IGNORECASE,
)

def _normalize_request(text: str) -> str:
    """
    Normalizza il testo di un ordine cliente.
    "I want to eat X. I'm intolerant to Y"  →  "X"
    Tutto il resto rimane invariato.
    """
    m = _INTOLERANCE_PATTERN.match(text.strip())
    return m.group(1).strip() if m else text


def aggregate_meals(meals: list[dict], price_map: dict[str, float] | None = None) -> list[dict]:
    """
    Aggrega i meals per nome ricetta.
    price_map: dict {dish_name: price} ricavato dal menu del ristorante.
    """
    if price_map is None:
        price_map = {}
    recipe_counts: dict[str, dict] = defaultdict(lambda: {"dishes_prepared": 0, "dishes_sold": 0})

    for meal in meals:
        recipe_name = next(
            (meal[f] for f in _RECIPE_FIELD_CANDIDATES if meal.get(f)),
            None,
        )
        if recipe_name is None:
            recipe_name = next(
                (str(v) for v in meal.values() if isinstance(v, str) and v),
                "unknown",
            )
        # Rimuovi il wrapper "I want to eat X. I'm intolerant to Y"
        recipe_name = _normalize_request(recipe_name)

        recipe_counts[recipe_name]["dishes_prepared"] += 1
        if meal.get("executed", False):
            recipe_counts[recipe_name]["dishes_sold"] += 1

    return [
        {
            "recipe_name": name,
            "price": price_map.get(name),   # None se il piatto non è nel menu (es. request grezzo)
            **counts,
        }
        for name, counts in sorted(recipe_counts.items())
    ]


# Numero di richieste /meals da inviare in parallelo per volta
# L'API ha un rate limit basso: tenere a 2 per evitare 429
_BATCH_SIZE  = 2
_BATCH_DELAY = 1.0   # secondi tra un batch e il successivo


async def collect_turn_meals(turn_id: int) -> dict[str, dict]:
    """
    Fetcha i meals di tutti i ristoranti per il turno dato.
    Usa batch sequenziali per evitare HTTP 429.
    Restituisce un dict keyed by restaurant_id (str).
    """
    async with aiohttp.ClientSession() as session:
        restaurants = await get_all_restaurants(session)
        if not restaurants:
            print("⚠️  Nessun ristorante trovato.")
            return {}

        print(f"🍕 Trovati {len(restaurants)} ristoranti (batch size={_BATCH_SIZE}).")

        # ── Fase 1: fetch menu di tutti i ristoranti (sequenziale con delay) ────
        # Il menu è indipendente dal turno, lo fetchiamo separatamente per
        # non raddoppiare il rate hit su /meals
        print("   📋 Caricamento menu ristoranti...")
        all_menus: list[dict[str, float]] = []
        _printed_menu_sample = False
        for idx, r in enumerate(restaurants):
            menu = await get_menu_for_restaurant(session, r.get("id"))
            all_menus.append(menu)
            if menu and not _printed_menu_sample:
                sample_item = next(iter(menu.items()))
                print(f"   🔍 Esempio menu raw (id={r.get('id')}): primo item → {sample_item}")
                _printed_menu_sample = True
            if idx < len(restaurants) - 1:
                await asyncio.sleep(0.4)   # 0.4s tra ogni richiesta menu

        # ── Fase 2: fetch meals in batch (come prima) ─────────────────────────
        all_meals: list[list[dict]] = []
        for i in range(0, len(restaurants), _BATCH_SIZE):
            batch = restaurants[i : i + _BATCH_SIZE]
            tasks = [
                get_meals_for_restaurant(session, turn_id, r.get("id"))
                for r in batch
            ]
            batch_results = await asyncio.gather(*tasks)
            all_meals.extend(batch_results)
            if i + _BATCH_SIZE < len(restaurants):
                await asyncio.sleep(_BATCH_DELAY)

        # Debug: stampa il primo meal non vuoto per capire la struttura
        _printed_sample = False

        compiled: dict[str, dict] = {}
        for restaurant, meals, menu in zip(restaurants, all_meals, all_menus):
            rest_id   = str(restaurant.get("id", "unknown"))
            rest_name = restaurant.get("name", f"Restaurant {rest_id}")

            if meals and not _printed_sample:
                print(f"\n🔍 Esempio raw meal (id={rest_id}): {meals[0]}\n")
                _printed_sample = True

            aggregated = aggregate_meals(meals, price_map=menu)
            compiled[rest_id] = {
                "restaurant_name": rest_name,
                "recipes": aggregated,
            }
            print(
                f"  ✅ {rest_name} (id={rest_id}): "
                f"{len(meals)} meals → {len(aggregated)} ricette distinte"
            )

        return compiled


def save_to_firebase(
    db: firestore.Client,
    turn_id: int,
    run_id: str,
    restaurants_data: dict[str, dict],
) -> bool:
    """Salva il documento nella collezione 'meals' di Firestore."""
    doc_id = f"run_{run_id}_turn_{turn_id}"
    payload = {
        "run_id":      run_id,
        "turn_id":     turn_id,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "restaurants": restaurants_data,
    }
    try:
        db.collection("meals").document(doc_id).set(payload)
        print(f"💾 Dati salvati su Firebase → meals/{doc_id}")
        return True
    except Exception as exc:
        print(f"❌  Errore salvataggio Firebase: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main(turn_id: int, run_id: str) -> None:
    _require_api_key()
    print(f"\n📊 Meals Tracker — turn={turn_id}  run={run_id}")
    print(f"   Server : {BASE_URL}")
    print(f"   Cred   : {CRED_PATH}\n")

    # 1. Firebase
    db = init_firebase()
    if db is None:
        sys.exit("❌  Impossibile connettersi a Firebase. Controlla le credenziali.")

    # 2. Raccolta dati
    restaurants_data = await collect_turn_meals(turn_id)

    if not restaurants_data:
        print("⚠️  Nessun dato raccolto, nulla da salvare.")
        return

    # 3. Persistenza
    save_to_firebase(db, turn_id, run_id, restaurants_data)

    print("\n✅ Meals Tracker completato.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Salva su Firebase i meals di fine turno per ogni ristorante."
    )
    parser.add_argument(
        "--turn_id",
        type=int,
        required=True,
        help="ID del turno appena concluso (es. 3)",
    )
    parser.add_argument(
        "--run_id",
        type=str,
        default="main",
        help="Etichetta della run di gioco (es. 'official_run_1'). Default: 'main'",
    )
    args = parser.parse_args()

    asyncio.run(main(turn_id=args.turn_id, run_id=args.run_id))
