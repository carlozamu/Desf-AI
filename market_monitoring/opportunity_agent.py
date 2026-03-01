"""
market_monitoring/opportunity_agent.py
───────────────────────────────────────
Agente Datapizza AI che analizza le ultime N snapshot di mercato da Firebase
e identifica opportunità di acquisto:

  📌 Definizione di "opportunità":
     Un ingrediente con ALTA domanda (molti BUY da più ristoranti, alto qty totale)
     ma BASSA offerta (pochi SELL, qty bassa, o prezzo di vendita elevato).

     Profilo opportunità: buy_count >> sell_count, o sell_qty << buy_qty.

Utilizzo:
  python market_monitoring/opportunity_agent.py [--n_runs 10] [--run_id main]
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

import firebase_admin
from firebase_admin import credentials, firestore

from datapizza.agents import Agent
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.tools import Tool

# ── Config ────────────────────────────────────────────────────────────────────

REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")
CRED_PATH = (
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(Path(__file__).parent.parent / "firebase_credentials.json")).strip()
    or str(Path(__file__).parent.parent / "firebase_credentials.json")
)
_HERE = Path(__file__).parent
LOG_FILE = _HERE / "opportunity_agent_log.txt"

_N_RUNS = 10  # quante snapshot analizzare di default

# ── AI Client ─────────────────────────────────────────────────────────────────

_ai_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ── Firebase ──────────────────────────────────────────────────────────────────

def _init_firebase():
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as exc:
        return None


# ── Tool: legge le ultime N snapshot ─────────────────────────────────────────

def get_market_history(n_runs: int = _N_RUNS) -> str:
    """
    Legge le ultime `n_runs` snapshot di mercato da Firebase
    (collezione market_snapshots, ordinate per turn_id decrescente).
    Restituisce un JSON aggregato per ingrediente con la media delle metriche
    su tutti i turni analizzati.

    Input: n_runs (int) — numero di snapshot da analizzare (default 10)
    """
    db = _init_firebase()
    if not db:
        return json.dumps({"error": "Firebase non disponibile."})

    try:
        docs = (
            db.collection("market_snapshots")
            .order_by("turn_id", direction="DESCENDING")
            .limit(n_runs)
            .stream()
        )

        snapshots = []
        for doc in docs:
            d = doc.to_dict()
            snapshots.append(d)

        if not snapshots:
            return json.dumps({"error": "Nessuna snapshot trovata in market_snapshots."})

        # Consolida le snapshot: calcola media di count/qty per ingrediente
        # su tutti i turni disponibili
        from collections import defaultdict
        agg: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "buy_count_total":  0,
            "buy_qty_total":    0,
            "buy_avg_price":    [],
            "sell_count_total": 0,
            "sell_qty_total":   0,
            "sell_avg_price":   [],
            "turns_seen":       0,
        })

        turn_ids = []
        for snap in snapshots:
            turn_ids.append(snap.get("turn_id", "?"))
            by_ing = snap.get("by_ingredient", {})
            for ingredient, sides in by_ing.items():
                entry = agg[ingredient]
                entry["turns_seen"] += 1

                b = sides.get("buy", {})
                entry["buy_count_total"]  += b.get("count", 0)
                entry["buy_qty_total"]    += b.get("total_qty", 0)
                if b.get("avg_unit_price") is not None:
                    entry["buy_avg_price"].append(b["avg_unit_price"])

                s = sides.get("sell", {})
                entry["sell_count_total"]  += s.get("count", 0)
                entry["sell_qty_total"]    += s.get("total_qty", 0)
                if s.get("avg_unit_price") is not None:
                    entry["sell_avg_price"].append(s["avg_unit_price"])

        # Calcola ratios e finalizza
        result = {}
        n = len(snapshots)
        for ingredient, e in agg.items():
            buy_avg_price  = round(sum(e["buy_avg_price"])  / len(e["buy_avg_price"]),  2) if e["buy_avg_price"]  else None
            sell_avg_price = round(sum(e["sell_avg_price"]) / len(e["sell_avg_price"]), 2) if e["sell_avg_price"] else None

            demand = e["buy_count_total"]
            supply = e["sell_count_total"]
            # Rapporto domanda/offerta (più alto = più opportunità)
            supply_ratio = round(demand / supply, 2) if supply > 0 else float("inf")

            result[ingredient] = {
                "turns_analyzed":      n,
                "turns_seen":          e["turns_seen"],
                "buy_count_total":     e["buy_count_total"],
                "buy_qty_total":       round(e["buy_qty_total"], 1),
                "buy_avg_unit_price":  buy_avg_price,
                "sell_count_total":    e["sell_count_total"],
                "sell_qty_total":      round(e["sell_qty_total"], 1),
                "sell_avg_unit_price": sell_avg_price,
                "demand_supply_ratio": supply_ratio,  # ∞ = nessuna offerta
            }

        summary = {
            "turns_analyzed": n,
            "turn_ids":        sorted(turn_ids),
            "ingredient_count": len(result),
            "by_ingredient":   result,
        }
        return json.dumps(summary, default=str, ensure_ascii=False)

    except Exception as exc:
        return json.dumps({"error": str(exc)})

def get_bid_prices_from_history(n_runs: int = _N_RUNS) -> str:
    """
    Legge le ultime `n_runs` run di bid history da Firebase (collezione bid_history)
    e calcola per ogni ingrediente il prezzo medio/min/max delle offerte nel closed bid.

    Input: n_runs (int) — numero di run da leggere (default 10)

    Ogni entry nei bids ha la forma:
      { ingredient, bid (prezzo offerto), quantity, restaurantId, status, ... }

    Restituisce un JSON:
      {
        "turns_analyzed": N,
        "by_ingredient": {
          "Polvere di Crononite": {
            "avg_bid_price": float,
            "min_bid_price": float,
            "max_bid_price": float,
            "total_bids":    int,
            "total_qty":     float
          },
          ...
        }
      }
    """
    db = _init_firebase()
    if not db:
        return json.dumps({"error": "Firebase non disponibile."})

    try:
        docs = (
            db.collection("bid_history")
            .order_by("turn_id", direction="DESCENDING")
            .limit(n_runs)
            .stream()
        )

        from collections import defaultdict
        agg: dict[str, dict] = defaultdict(lambda: {"bids": [], "qty": 0, "count": 0})
        turn_ids = []

        for doc in docs:
            data = doc.to_dict()
            turn_ids.append(data.get("turn_id", "?"))
            bids_list = data.get("bids", [])

            for bid_entry in bids_list:
                # Il nome dell'ingrediente può essere in "ingredient" (str o dict)
                raw = bid_entry.get("ingredient") or bid_entry.get("ingredientName") or ""
                if isinstance(raw, dict):
                    ingredient = raw.get("name") or str(next(iter(raw.values()), "unknown"))
                else:
                    ingredient = str(raw).strip() or "unknown"

                price = None
                for pkey in ("bid", "price", "bidPrice", "amount"):
                    v = bid_entry.get(pkey)
                    if v is not None:
                        try:
                            price = float(v)
                            break
                        except (TypeError, ValueError):
                            pass

                qty = float(bid_entry.get("quantity", 1) or 1)

                if price is not None:
                    agg[ingredient]["bids"].append(price)
                    agg[ingredient]["qty"] += qty
                    agg[ingredient]["count"] += 1

        if not agg:
            return json.dumps({"error": "Nessun dato di bid history trovato.", "turn_ids": turn_ids})

        result = {}
        for ingredient, data in agg.items():
            prices = data["bids"]
            result[ingredient] = {
                "avg_bid_price": round(sum(prices) / len(prices), 2) if prices else None,
                "min_bid_price": round(min(prices), 2) if prices else None,
                "max_bid_price": round(max(prices), 2) if prices else None,
                "total_bids":    data["count"],
                "total_qty":     round(data["qty"], 1),
            }

        return json.dumps({
            "turns_analyzed": len(turn_ids),
            "turn_ids":       sorted(turn_ids),
            "by_ingredient":  result,
        }, default=str, ensure_ascii=False)

    except Exception as exc:
        return json.dumps({"error": str(exc)})



_tools = [
    Tool(
        name="get_market_history",
        func=get_market_history,
        description=(
            f"No input required (or pass n_runs: int). "
            f"Returns aggregated BUY/SELL stats per ingredient across the last "
            f"{_N_RUNS} market snapshots. Key fields: buy_count_total, sell_count_total, "
            f"demand_supply_ratio (higher = more opportunity), buy_avg_unit_price, sell_avg_unit_price."
        ),
    ),
    Tool(
        name="get_bid_prices_from_history",
        func=get_bid_prices_from_history,
        description=(
            f"No input required (or pass n_runs: int). "
            f"Returns avg/min/max closed-bid prices per ingredient from the last "
            f"{_N_RUNS} turns of the bid_history Firebase collection. "
            f"Use this to compare open-market buy prices vs closed-bid prices "
            f"to identify where bidding in the closed phase is cheaper."
        ),
    ),
]

_opportunity_agent = Agent(
    name="market_opportunity_analyst",
    client=_ai_client,
    system_prompt=f"""You are a market analyst for a restaurant game.
You have two data sources available:
1. `get_market_history()` — open market BUY/SELL entries (what restaurants pay to trade with each other)
2. `get_bid_prices_from_history()` — closed bid prices (what restaurants paid in the sealed-bid auction)

Your task:
1. Call BOTH tools to load the data.
2. Note that the closed bid data may cover FOREWER turns than the open market data (e.g., only the last 2-3 turns). This is expected. You should STILL compare the overlapping data.
3. For each ingredient, compare:
   - `buy_avg_unit_price` (open market): how much buyers are willing to pay on the open market
   - `avg_bid_price` (closed bid): what the average winning bid was in the sealed auction
   - If avg_bid_price < buy_avg_unit_price → buying via closed bid is CHEAPER than the open market
     → "closed-bid advantage" in credits saved per unit
   - If avg_bid_price is None → ingredient not available in closed bid, open market is the only option
4. Identify TOP OPPORTUNITIES — ingredients where ALL of these are true:
   - HIGH demand (high buy_count_total, high demand_supply_ratio)
   - LOW supply on open market (few SELL entries)
   - Closed bid price is LOWER than open market price (or ingredient has ∞ ratio = no sellers)
5. For each opportunity output:
   - Ingredient name
   - Open market: demand/supply ratio, avg buy price
   - Closed bid: avg bid price, saving per unit (market_buy - bid_price)
   - Recommendation: whether to prioritize closed bid OR open market purchasing

Output format:
## Market Opportunities — Bid vs Market Analysis

### 🔥 [Ingredient] (demand/supply ratio: X)
| Metric | Value |
|---|---|
| Open market avg buy price | X cr/unit |
| Closed bid avg price | X cr/unit |
| Saving per unit (bid cheaper by) | X cr/unit |
| Total BUY demand (10 turns) | X orders / Y units |
| Open market SELL supply | X entries / Y units |
- **Strategy**: [closed bid recommended / open market only / monitor]
- **Why**: [brief rationale]

List at least 5 top opportunities. Sort by demand_supply_ratio descending within each priority tier.
Priority tiers: ∞ ratio > ratio ≥ 10 > ratio ≥ 5 > ratio ≥ 2.
""",
    tools=_tools,
)


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_opportunity_agent(n_runs: int = _N_RUNS) -> str:
    print(f"[OPPORTUNITY] 🔍 Analizzando le snapshot di mercato (e bid limits)...")

    prompt = (
        f"Analyze up to {n_runs} market snapshots. "
        f"Call BOTH get_market_history() AND get_bid_prices_from_history() to compare "
        f"open-market prices with closed-bid prices. "
        f"(Note: bid history might return fewer turns than market history, rely on whatever data is returned). "
        f"Identify the top opportunities (high demand, low supply, and where bid price is cheaper than market price)."
    )

    def _run() -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            resp = _opportunity_agent.run(prompt)
        captured = buf.getvalue()
        output = resp.text or captured

        # Log su file
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"RUN {datetime.now().isoformat()}\n")
                f.write(f"{'='*60}\n")
                f.write(captured)
                f.write("\n--- AGENT RESPONSE ---\n")
                f.write(output)
                f.write("\n")
        except Exception as log_exc:
            print(f"[OPPORTUNITY] ⚠️  Log error: {log_exc}")

        return output

    result = await asyncio.to_thread(_run)
    print(f"[OPPORTUNITY] ✅ Analisi completata.\n")
    print(result)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Market Opportunity Agent")
    parser.add_argument("--n_runs", type=int, default=_N_RUNS,
                        help=f"Quante snapshot analizzare (default: {_N_RUNS})")
    args = parser.parse_args()
    asyncio.run(run_opportunity_agent(args.n_runs))
