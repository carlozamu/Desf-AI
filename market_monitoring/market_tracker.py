"""
market_monitoring/market_tracker.py
─────────────────────────────────────
Monitora le entry di mercato (BUY/SELL) di tutti i ristoranti.

Cosa fa:
  1. Chiama GET /market/entries — restituisce tutte le offerte attive/chiuse.
  2. Aggrega per ingrediente: quante richieste d'acquisto (BUY), quante vendite (SELL),
     prezzi medi, minimi e massimi per ciascun lato.
  3. Salva lo snapshot su Firestore nella collezione "market_snapshots":
       doc: run_<run_id>_turn_<turn_id>
       struttura:
         {
           "run_id": str,
           "turn_id": int,
           "timestamp": ISO8601,
           "snapshot_type": "end_of_turn",
           "total_entries": int,
           "by_ingredient": {
             "<ingredient>": {
               "buy": {
                 "count": int,
                 "total_qty": int,
                 "avg_price": float,
                 "min_price": float,
                 "max_price": float,
                 "entries": [{"id", "restaurant_id", "quantity", "price", "status"}, ...]
               },
               "sell": { ... }
             },
             ...
           }
         }

Utilizzo standalone:
  python market_monitoring/market_tracker.py --turn_id <N> [--run_id <label>]

Integrazione programmatica (es. end_turn in client_template.py):
  from market_monitoring.market_tracker import collect_market_snapshot, save_to_firebase, init_firebase
  db = init_firebase()
  snapshot = await collect_market_snapshot()
  save_to_firebase(db, turn_id, run_id, snapshot)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# ── Configurazione ────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
load_dotenv(dotenv_path=_ROOT / ".env")

BASE_URL     = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
TEAM_API_KEY = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", ""))
CRED_PATH    = (
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(_ROOT / "firebase_credentials.json")).strip()
    or str(_ROOT / "firebase_credentials.json")
)

_HEADERS = {"x-api-key": TEAM_API_KEY}

# Retry settings
_MAX_RETRIES   = 3
_RETRY_DELAY   = 1.5   # secondi (backoff: 1.5 → 3.0 → 4.5)


# ── Firebase ──────────────────────────────────────────────────────────────────

def init_firebase() -> Any | None:
    """Inizializza Firebase (idempotente) e restituisce il client Firestore."""
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as exc:
        print(f"⚠️  Firebase init error: {exc}")
        return None


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _fetch(session: aiohttp.ClientSession, url: str) -> Any | None:
    """GET JSON con retry su 429."""
    for attempt in range(_MAX_RETRIES):
        try:
            async with session.get(url, headers=_HEADERS) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429:
                    wait = _RETRY_DELAY * (attempt + 1)
                    print(f"   ⏳ 429 — retry tra {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                print(f"   ⚠️  HTTP {resp.status} — {url}")
                return None
        except aiohttp.ClientError as exc:
            print(f"   ⚠️  Network error: {exc}")
            return None
    return None


# ── Fetch mercato ─────────────────────────────────────────────────────────────

async def fetch_market_entries(session: aiohttp.ClientSession) -> list[dict]:
    """Recupera tutte le entry di mercato via GET /market/entries."""
    data = await _fetch(session, f"{BASE_URL}/market/entries")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Alcune API wrappano in {"entries": [...]} o {"data": [...]}
        for key in ("entries", "data", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


# ── Aggregazione ──────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_ingredient_name(entry: dict) -> str:
    """
    Estrae il nome dell'ingrediente da un'entry di mercato.
    L'API può restituire il campo come stringa semplice oppure
    come oggetto annidato (es. {"name": "Farina Cosmica", "id": 3}).
    """
    for key in ("ingredient_name", "ingredientName", "ingredient", "item"):
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            # Prova i campi "name", "ingredientName", "label"
            name = value.get("name") or value.get("ingredientName") or value.get("label")
            if name:
                return str(name).strip()
            # Fallback: prima stringa non vuota del dict
            for v in value.values():
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return "unknown"


def aggregate_entries(entries: list[dict]) -> dict[str, dict]:
    """
    Aggrega le entry per ingrediente e per lato (BUY/SELL).

    Struttura Firebase per ingrediente:
      {
        "Amido di Stellarion": {
          "buy":  { count, total_qty, avg_unit_price, min_unit_price, max_unit_price },
          "sell": { ... }
        },
        ...
      }
    """
    # Debug: stampa la struttura della prima entry (una sola volta)
    if entries:
        import json as _json
        print(f"   🔍 Struttura prima entry: {_json.dumps(entries[0], default=str)[:300]}")

    by_ingredient: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "buy":  {"count": 0, "total_qty": 0, "unit_prices": []},
            "sell": {"count": 0, "total_qty": 0, "unit_prices": []},
        }
    )

    for entry in entries:
        ingredient = _extract_ingredient_name(entry)
        side_raw = str(entry.get("side", entry.get("type", ""))).upper()
        side = "buy" if "BUY" in side_raw else "sell" if "SELL" in side_raw else None
        if not side:
            continue

        qty = _safe_float(entry.get("quantity")) or 0

        # L'API usa totalPrice (prezzo totale), ricaviamo il prezzo unitario
        total_price = _safe_float(entry.get("totalPrice") or entry.get("price"))
        unit_price: float | None = None
        if total_price is not None and qty > 0:
            unit_price = round(total_price / qty, 2)
        elif total_price is not None:
            unit_price = total_price

        bucket = by_ingredient[ingredient][side]
        bucket["count"]     += 1
        bucket["total_qty"] += qty
        if unit_price is not None:
            bucket["unit_prices"].append(unit_price)

    # Calcola statistiche e rimuovi la lista grezza
    result: dict[str, dict] = {}
    for ingredient, sides in by_ingredient.items():
        result[ingredient] = {}
        for side, bucket in sides.items():
            prices = bucket.pop("unit_prices")
            if prices:
                bucket["avg_unit_price"] = round(sum(prices) / len(prices), 2)
                bucket["min_unit_price"] = round(min(prices), 2)
                bucket["max_unit_price"] = round(max(prices), 2)
            else:
                bucket["avg_unit_price"] = None
                bucket["min_unit_price"] = None
                bucket["max_unit_price"] = None
            result[ingredient][side] = bucket

    return result



# ── Entry-point asincrono ─────────────────────────────────────────────────────

async def collect_market_snapshot() -> dict[str, Any]:
    """
    Fetcha e aggrega le entry di mercato.
    Restituisce il dizionario pronto per Firebase.
    """
    async with aiohttp.ClientSession() as session:
        print("📊 Fetching market entries...")
        entries = await fetch_market_entries(session)
        print(f"   📦 {len(entries)} entry totali ricevute.")

    aggregated = aggregate_entries(entries)

    # Stampa riepilogo a console
    buy_total  = sum(v["buy"]["count"]  for v in aggregated.values())
    sell_total = sum(v["sell"]["count"] for v in aggregated.values())
    print(f"   🛒 BUY entries : {buy_total}")
    print(f"   🏷️  SELL entries: {sell_total}")
    print(f"   🥦 Ingredienti distinti: {len(aggregated)}")

    for ingredient, sides in sorted(aggregated.items()):
        b = sides["buy"]
        s = sides["sell"]
        buy_str  = f"BUY ×{b['count']}  qty={b['total_qty']}  avg_price={b['avg_unit_price']}" if b["count"] else "—"
        sell_str = f"SELL×{s['count']}  qty={s['total_qty']}  avg_price={s['avg_unit_price']}" if s["count"] else "—"
        print(f"   • {ingredient:40s}  {buy_str:50s}  {sell_str}")

    return {
        "total_entries": len(entries),
        "buy_count":     buy_total,
        "sell_count":    sell_total,
        "ingredient_count": len(aggregated),
        "by_ingredient": aggregated,
    }


# ── Salvataggio Firebase ──────────────────────────────────────────────────────

def save_to_firebase(
    db: Any,
    turn_id: int,
    run_id: str,
    snapshot: dict[str, Any],
) -> bool:
    """Salva lo snapshot nella collezione 'market_snapshots'."""
    doc_id  = f"run_{run_id}_turn_{turn_id}"
    payload = {
        "run_id":        run_id,
        "turn_id":       turn_id,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "end_of_turn",
        **snapshot,
    }
    try:
        db.collection("market_snapshots").document(doc_id).set(payload)
        print(f"💾 Salvato su Firebase → market_snapshots/{doc_id}")
        return True
    except Exception as exc:
        print(f"❌ Errore Firebase: {exc}")
        return False


# ── CLI standalone ────────────────────────────────────────────────────────────

async def _main_cli(turn_id: int, run_id: str) -> None:
    if not TEAM_API_KEY:
        sys.exit("❌  SERVER_API_KEY non trovata nel .env")

    print("=" * 60)
    print(f"  📈 Market Monitor — turn={turn_id}  run={run_id}")
    print(f"  Server : {BASE_URL}")
    print("=" * 60)

    snapshot = await collect_market_snapshot()

    db = init_firebase()
    if not db:
        print("⚠️  Firebase non disponibile, salvataggio saltato.")
        return

    save_to_firebase(db, turn_id, run_id, snapshot)
    print("\n✅ Market Monitor completato.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Monitor — snapshot mercato a fine turno.")
    parser.add_argument("--turn_id", type=int, required=True, help="Turno corrente")
    parser.add_argument("--run_id",  type=str, default="main",  help="Label della run (default: main)")
    args = parser.parse_args()
    asyncio.run(_main_cli(args.turn_id, args.run_id))
