import os
import asyncio
from dotenv import load_dotenv

from price_analyst.agent import get_bidding_prices

# Load environment variables from .env file
load_dotenv()

async def main():
    # Verify we have API Key
    TEAM_API_KEY = os.getenv("SERVER_API_KEY") 
    CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")
    
    if not TEAM_API_KEY:
        print("⚠️ WARNING: SERVER_API_KEY not found in environment variables.")
        return

    print("=======================================")
    print("Testing get_bidding_prices Agent logic")
    print("=======================================")
    print(f"Using CRED_PATH: {CRED_PATH}")
    
    test_turn_id = 7 
    
    try:
        print("Waiting for Datapizza Agent... (il terminale rimarrà pulito, i log andranno su file)")
        
        # L'agente farà tutto: cattura log, firebase e salvataggio file testuale
        result = await asyncio.to_thread(get_bidding_prices, test_turn_id)
        
        print("\nTest completed.")
        if result:
             print("Prices Object:", result)
             print(f"Verifica su Firebase se in runs/turn_{test_turn_id}/bids_prices ci sono i dati!")
             print(f"Controlla il file execution_log_turn_{test_turn_id}.txt per i dettagli del prompt.")
        else:
             print("Data not fetched or parsing failed.")
             
    except Exception as e:
        print(f"\nException occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())