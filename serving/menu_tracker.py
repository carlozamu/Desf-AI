"""
Tracker menu per turno.

Obiettivi:
- leggere i menu di tutti i ristoranti (anche altrui) tramite GET /restaurant/:id/menu;
- salvare snapshot su Firestore;
- offrire helper di confronto prezzi competitor.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
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

_MENU_DELAY = 0.35


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
        print(f"⚠️ Errore Firebase: {exc}")
        return None


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = 3,
) -> list | dict | None:
    headers = {"x-api-key": TEAM_API_KEY}

    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()

                if resp.status == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
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


async def get_menu(session: aiohttp.ClientSession, restaurant_id: int | str) -> list[dict[str, Any]]:
    data = await _fetch_json(session, f"{BASE_URL}/restaurant/{restaurant_id}/menu")

    items: list[dict[str, Any]]
    if isinstance(data, dict):
        raw = data.get("items", [])
        items = raw if isinstance(raw, list) else []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        price = item.get("price")
        cleaned.append({"name": str(name), "price": float(price) if isinstance(price, (int, float)) else None})

    return cleaned


async def collect_all_menus(
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

        for idx, restaurant in enumerate(restaurants):
            rest_id = str(restaurant.get("id", "unknown"))
            rest_name = restaurant.get("name", f"Restaurant {rest_id}")
            is_open = bool(restaurant.get("isOpen", False))

            menu = await get_menu(session, restaurant.get("id"))
            compiled[rest_id] = {
                "restaurant_name": rest_name,
                "is_open": is_open,
                "menu": menu,
            }

            if idx < len(restaurants) - 1:
                await asyncio.sleep(_MENU_DELAY)

        return compiled


def build_competitor_price_index(
    restaurants_data: dict[str, dict[str, Any]],
    *,
    my_restaurant_id: int | str | None = None,
) -> dict[str, list[float]]:
    my_id = str(my_restaurant_id) if my_restaurant_id is not None else None
    index: dict[str, list[float]] = {}

    for rest_id, info in restaurants_data.items():
        if my_id is not None and str(rest_id) == my_id:
            continue

        for item in info.get("menu", []):
            dish_name = str(item.get("name", "")).strip()
            price = item.get("price")
            if not dish_name or not isinstance(price, (int, float)):
                continue
            index.setdefault(dish_name, []).append(float(price))

    for dish_name in list(index.keys()):
        index[dish_name].sort()

    return index


def get_lowest_competitor_price(
    restaurants_data: dict[str, dict[str, Any]],
    dish_name: str,
    *,
    my_restaurant_id: int | str | None = None,
) -> float | None:
    index = build_competitor_price_index(restaurants_data, my_restaurant_id=my_restaurant_id)
    prices = index.get(dish_name, [])
    return prices[0] if prices else None


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
    }
    try:
        db.collection("menus").document(doc_id).set(payload)
        print(f"💾 Dati salvati su Firebase -> menus/{doc_id}")
        return True
    except Exception as exc:
        print(f"❌ Errore salvataggio Firebase: {exc}")
        return False


async def main(turn_id: int, run_id: str) -> None:
    _require_api_key()

    db = init_firebase()
    if db is None:
        sys.exit("❌ Firebase non disponibile")

    restaurants_data = await collect_all_menus()
    if not restaurants_data:
        print("⚠️ Nessun dato raccolto")
        return

    save_to_firebase(db, turn_id, run_id, restaurants_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tracker menu competitor")
    parser.add_argument("--turn_id", type=int, required=True)
    parser.add_argument("--run_id", type=str, default="main")
    args = parser.parse_args()
    asyncio.run(main(turn_id=args.turn_id, run_id=args.run_id))
