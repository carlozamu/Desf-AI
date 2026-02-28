"""
Tracker pasti per turno.

Obiettivi:
- leggere GET /meals per tutti i ristoranti;
- evidenziare ricette richieste ma non eseguite (executed == false);
- salvare lo snapshot su Firestore.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import aiohttp
import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")

load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))

BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
TEAM_API_KEY = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", ""))
CRED_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(_ROOT, "firebase_credentials.json"),
)

_RECIPE_FIELD_CANDIDATES = (
    "request",
    "recipe_name",
    "recipe",
    "dish",
    "dish_name",
    "name",
    "orderText",
    "order_text",
)

_REQUEST_PATTERN = re.compile(r"^\s*i want to eat\s+(.+?)(?:\.\s*i'?m intolerant to.+)?\s*$", re.IGNORECASE)

_BATCH_SIZE = 3
_BATCH_DELAY = 0.8


def _require_api_key() -> None:
    if not TEAM_API_KEY:
        sys.exit("❌ SERVER_API_KEY (o TEAM_API_KEY) non trovata nel file .env")


def init_firebase() -> firestore.Client | None:
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as exc:
        print(f"⚠️ Errore inizializzazione Firebase: {exc}")
        return None


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    retries: int = 3,
) -> list | dict | None:
    headers = {"x-api-key": TEAM_API_KEY}

    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()

                if resp.status == 429:
                    wait_seconds = 1.5 * (attempt + 1)
                    await asyncio.sleep(wait_seconds)
                    continue

                body = await resp.text()
                print(f"⚠️ HTTP {resp.status} su {url}: {body[:200]}")
                return None
        except aiohttp.ClientError as exc:
            print(f"⚠️ Errore rete su {url}: {exc}")
            return None

    print(f"❌ Rinuncio dopo {retries} tentativi: {url}")
    return None


async def get_all_restaurants(
    session: aiohttp.ClientSession,
    *,
    include_closed: bool = True,
) -> list[dict[str, Any]]:
    data = await _fetch_json(session, f"{BASE_URL}/restaurants")

    if isinstance(data, list):
        restaurants = data
    elif isinstance(data, dict):
        restaurants = data.get("restaurants", [])
    else:
        restaurants = []

    if include_closed:
        return restaurants

    return [r for r in restaurants if r.get("isOpen") is True]


async def get_meals_for_restaurant(
    session: aiohttp.ClientSession,
    turn_id: int,
    restaurant_id: int | str,
) -> list[dict[str, Any]]:
    data = await _fetch_json(
        session,
        f"{BASE_URL}/meals",
        params={"turn_id": turn_id, "restaurant_id": restaurant_id},
    )
    return data if isinstance(data, list) else []


def _normalize_recipe_name(raw: str) -> str:
    text = str(raw).strip()
    if not text:
        return "unknown"

    match = _REQUEST_PATTERN.match(text)
    if match:
        return match.group(1).strip()
    return text


def extract_recipe_name(meal: dict[str, Any]) -> str:
    for field in _RECIPE_FIELD_CANDIDATES:
        value = meal.get(field)
        if isinstance(value, str) and value.strip():
            return _normalize_recipe_name(value)

    for value in meal.values():
        if isinstance(value, str) and value.strip():
            return _normalize_recipe_name(value)

    return "unknown"


def aggregate_meals(
    meals: list[dict[str, Any]],
    price_map: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if price_map is None:
        price_map = {}
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "total_requests": 0,
            "executed_requests": 0,
            "unserved_requests": 0,
        }
    )

    for meal in meals:
        recipe_name = extract_recipe_name(meal)
        executed = bool(meal.get("executed", False))

        entry = stats[recipe_name]
        entry["total_requests"] += 1
        if executed:
            entry["executed_requests"] += 1
        else:
            entry["unserved_requests"] += 1

    rows: list[dict[str, Any]] = []
    for recipe_name, row in stats.items():
        total = row["total_requests"]
        executed = row["executed_requests"]
        unserved = row["unserved_requests"]
        execution_rate = round((executed / total) * 100, 2) if total else 0.0
        rows.append(
            {
                "recipe_name": recipe_name,
                "price": price_map.get(recipe_name),
                "total_requests": total,
                "executed_requests": executed,
                "unserved_requests": unserved,
                "execution_rate": execution_rate,
                # Compatibilità con naming precedente
                "dishes_prepared": total,
                "dishes_sold": executed,
            }
        )

    rows.sort(
        key=lambda x: (
            -x["unserved_requests"],
            -x["total_requests"],
            x["recipe_name"],
        )
    )
    return rows


async def collect_turn_meals(
    turn_id: int,
    *,
    include_closed_restaurants: bool = True,
) -> dict[str, dict[str, Any]]:
    async with aiohttp.ClientSession() as session:
        restaurants = await get_all_restaurants(
            session,
            include_closed=include_closed_restaurants,
        )
        if not restaurants:
            print("⚠️ Nessun ristorante trovato.")
            return {}

        compiled: dict[str, dict[str, Any]] = {}

        for i in range(0, len(restaurants), _BATCH_SIZE):
            batch = restaurants[i : i + _BATCH_SIZE]
            tasks = [
                get_meals_for_restaurant(session, turn_id, restaurant.get("id"))
                for restaurant in batch
            ]
            batch_results = await asyncio.gather(*tasks)

            for restaurant, meals in zip(batch, batch_results):
                rest_id = str(restaurant.get("id", "unknown"))
                rest_name = restaurant.get("name", f"Restaurant {rest_id}")

                total_meals = len(meals)
                executed_meals = sum(1 for meal in meals if meal.get("executed") is True)
                unserved_meals = total_meals - executed_meals

                compiled[rest_id] = {
                    "restaurant_name": rest_name,
                    "total_meals": total_meals,
                    "executed_meals": executed_meals,
                    "unserved_meals": unserved_meals,
                    "recipes": aggregate_meals(meals),
                }

            if i + _BATCH_SIZE < len(restaurants):
                await asyncio.sleep(_BATCH_DELAY)

        return compiled


def summarize_unserved_requests(restaurants_data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_recipe: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "recipe_name": "",
            "total_requests": 0,
            "executed_requests": 0,
            "unserved_requests": 0,
            "restaurants": set(),
        }
    )

    for rest_id, info in restaurants_data.items():
        rest_name = info.get("restaurant_name", f"Restaurant {rest_id}")
        for recipe in info.get("recipes", []):
            recipe_name = str(recipe.get("recipe_name", "unknown"))
            entry = by_recipe[recipe_name]
            entry["recipe_name"] = recipe_name
            entry["total_requests"] += int(recipe.get("total_requests", 0))
            entry["executed_requests"] += int(recipe.get("executed_requests", 0))
            entry["unserved_requests"] += int(recipe.get("unserved_requests", 0))
            if int(recipe.get("unserved_requests", 0)) > 0:
                entry["restaurants"].add(rest_name)

    rows: list[dict[str, Any]] = []
    for entry in by_recipe.values():
        rows.append(
            {
                "recipe_name": entry["recipe_name"],
                "total_requests": entry["total_requests"],
                "executed_requests": entry["executed_requests"],
                "unserved_requests": entry["unserved_requests"],
                "restaurants": sorted(entry["restaurants"]),
            }
        )

    rows.sort(
        key=lambda x: (
            -x["unserved_requests"],
            -x["total_requests"],
            x["recipe_name"],
        )
    )
    return rows


async def get_top_unserved_recipes(
    turn_id: int,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    data = await collect_turn_meals(turn_id)
    ranking = summarize_unserved_requests(data)
    return ranking[: max(0, limit)]


def save_to_firebase(
    db: firestore.Client,
    turn_id: int,
    run_id: str,
    restaurants_data: dict[str, dict[str, Any]],
) -> bool:
    doc_id = f"run_{run_id}_turn_{turn_id}"
    payload = {
        "run_id": run_id,
        "turn_id": turn_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "restaurants": restaurants_data,
        "top_unserved": summarize_unserved_requests(restaurants_data)[:20],
    }
    try:
        db.collection("meals").document(doc_id).set(payload)
        print(f"💾 Dati salvati su Firebase -> meals/{doc_id}")
        return True
    except Exception as exc:
        print(f"❌ Errore salvataggio Firebase: {exc}")
        return False


async def main(turn_id: int, run_id: str) -> None:
    _require_api_key()

    db = init_firebase()
    if db is None:
        sys.exit("❌ Firebase non disponibile")

    restaurants_data = await collect_turn_meals(turn_id)
    if not restaurants_data:
        print("⚠️ Nessun dato raccolto")
        return

    save_to_firebase(db, turn_id, run_id, restaurants_data)

    ranking = summarize_unserved_requests(restaurants_data)[:10]
    if ranking:
        print("\nTop ricette richieste ma non eseguite:")
        for row in ranking:
            if row["unserved_requests"] <= 0:
                continue
            print(
                f"  - {row['recipe_name']}: unserved={row['unserved_requests']} "
                f"(tot={row['total_requests']}, served={row['executed_requests']})"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tracker richieste clienti via GET /meals")
    parser.add_argument("--turn_id", type=int, required=True)
    parser.add_argument("--run_id", type=str, default="main")
    args = parser.parse_args()
    asyncio.run(main(turn_id=args.turn_id, run_id=args.run_id))
