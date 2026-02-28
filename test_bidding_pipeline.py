import asyncio
import os
import sys
import json
from dotenv import load_dotenv

# Configura i moduli root
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

# Setup variabili d'ambiente di fallback
if not os.getenv("TEAM_ID"):
    print("WARNING: TEAM_ID non settato in .env, uso valore di default '1'")
    os.environ["TEAM_ID"] = "1"
    
if not os.getenv("SERVER_API_KEY") and not os.getenv("TEAM_API_KEY"):
    print("WARNING: SERVER_API_KEY non settato in .env. Le chiamate MCP falliranno.")

# Importiamo la logica dell'agente e del bidder
try:
    from price_analyst.agent import get_bidding_prices
    from bidder.bidder import place_bids_for_turn
except ImportError as e:
    print(f"❌ Errore di importazione: {e}")
    sys.exit(1)


async def test_pipeline_turn(turn_id: int):
    """
    Simula l'esecuzione end-to-end della pipeline per la closed bid phase.
    """
    print(f"\n{'='*60}")
    print(f"🚀 INIZIO TEST PIPELINE DI BIDDING - TURNO {turn_id}")
    print(f"{'='*60}\n")
    
    # ---------------------------------------------------------
    # FASE 1: Price Analyst
    # ---------------------------------------------------------
    print(">>> FASE 1: Price Analyst (Calcolo Dinamico dei Prezzi Storici)")
    try:
        prices = await asyncio.to_thread(get_bidding_prices, turn_id)
        if not prices:
            print("❌ Nessun prezzo restituito. Test interrotto.")
            return
        print(f"✅ Prezzi calcolati per {len(prices)} ingredienti.")
    except Exception as e:
        print(f"❌ Errore in FASE 1: {e}")
        return
        
    # ---------------------------------------------------------
    # FASE 2: Bidder Pipeline
    # ---------------------------------------------------------
    print("\n>>> FASE 2: Bidder (Shopping List & Offerte)")
    try:
        response = await place_bids_for_turn(turn_id=turn_id, prices=prices)
        print("\n✅ Pipeline Completata! Risposta dal server:")
        print(json.dumps(response, indent=2))
    except Exception as e:
         print(f"❌ Errore in FASE 2: {e}")


if __name__ == "__main__":
    asyncio.run(test_pipeline_turn(7))
