"""
market_monitoring/backfill.py
──────────────────────────────
Esegue il market tracker per le ultime N run e le salva su Firebase.

Utilizzo:
  python market_monitoring/backfill.py --current_turn 36 --n_turns 10 [--run_id main]

Salva su Firebase → market_snapshots/run_<run_id>_turn_<N> per ogni turno.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from market_monitoring.market_tracker import (
    collect_market_snapshot,
    save_to_firebase,
    init_firebase,
    TEAM_API_KEY,
    BASE_URL,
)


async def run_backfill(current_turn: int, n_turns: int, run_id: str) -> None:
    if not TEAM_API_KEY:
        sys.exit("❌  SERVER_API_KEY non trovata nel .env")

    turns = list(range(max(1, current_turn - n_turns + 1), current_turn + 1))

    print("=" * 60)
    print(f"  📈 Market Backfill — turni {turns[0]}→{turns[-1]}")
    print(f"  RUN_ID : {run_id}")
    print(f"  Server : {BASE_URL}")
    print("=" * 60)

    db = init_firebase()
    if not db:
        sys.exit("❌  Firebase non disponibile.")

    # Nota: il market endpoint /market/entries è snapshot live (non filtrato per turno),
    # quindi ogni chiamata ritorna lo stato attuale del mercato.
    # Per i turni passati salviamo lo snapshot storico disponibile al momento.
    for turn_id in turns:
        print(f"\n{'─'*60}")
        print(f"  🔄 Processing turn {turn_id} / {turns[-1]}")
        print(f"{'─'*60}")
        try:
            snapshot = await collect_market_snapshot()
            ok = save_to_firebase(db, turn_id, run_id, snapshot)
            if ok:
                print(f"  ✅ Turn {turn_id} salvato.")
            else:
                print(f"  ❌ Turn {turn_id} — errore salvataggio Firebase.")
        except Exception as exc:
            print(f"  ❌ Turn {turn_id} — eccezione: {exc}")

        # Piccolo delay per non martellare l'API
        if turn_id != turns[-1]:
            await asyncio.sleep(1.0)

    print(f"\n{'='*60}")
    print(f"✅ Backfill completato: {len(turns)} turni processati.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Tracker — backfill ultimi N turni.")
    parser.add_argument("--current_turn", type=int, default=36,
                        help="Turno corrente (default: 36)")
    parser.add_argument("--n_turns",      type=int, default=10,
                        help="Quanti turni indietro processare (default: 10)")
    parser.add_argument("--run_id",       type=str, default="main",
                        help="Label della run Firebase (default: main)")
    args = parser.parse_args()

    asyncio.run(run_backfill(args.current_turn, args.n_turns, args.run_id))
