import os
import json
import io
from contextlib import redirect_stdout
from typing import List, Dict, Any
from dotenv import load_dotenv

from datapizza.agents import Agent
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.tools import Tool

from price_analyst.bid_tracker import init_firebase

load_dotenv()

REGOLO_API_KEY = os.getenv("REGOLO_API_KEY")

# Calcola la directory in cui si trova QUESTO file (agent.py)
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Initialize AI Client
client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1"
)




# --- LOGGING SYSTEM ---
execution_logs: List[str] = []

def log_message(msg: str):
    """Aggiunge un messaggio alla lista dei log in memoria."""
    if msg.strip(): # Evita di salvare righe vuote inutili
        execution_logs.append(msg)

def save_logs_to_file(filename: str):
    """Scrive tutti i log accumulati su un file di testo e pulisce la memoria."""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(execution_logs))
        # Svuota i log in memoria per il prossimo turno
        execution_logs.clear()
    except Exception as e:
        print(f"Errore critico durante il salvataggio dei log: {e}")




# --- FIREBASE TOOLS ---
def get_past_bids(turn_id: int) -> str:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")
    db = init_firebase(cred_path)
    if not db:
        return json.dumps({"error": "No Firestore client available. Cannot fetch bids."})

    try:
        doc_ref = db.collection("bid_history").document(f"turn_{turn_id}")
        doc = doc_ref.get()
        if doc.exists:
            return json.dumps(doc.to_dict())
        else:
            return json.dumps({"error": f"No bid history found for turn {turn_id}."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def get_item_utility_score(item_name: str) -> str:
    return "1.0"

def save_bids_to_firebase(turn_id: int, prices: dict):
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")
    db = init_firebase(cred_path)
    if not db:
        log_message("❌ Errore: Nessun client Firestore disponibile. Impossibile salvare i prezzi.")
        return

    try:
        doc_ref = db.collection("runs").document(f"run_{turn_id}")
        doc_ref.set({"bids_prices": prices}, merge=True)
        log_message(f"💾 Prezzi salvati con successo su Firebase in runs/turn_{turn_id}/bids_prices.")
    except Exception as e:
        log_message(f"❌ Errore durante il salvataggio su Firebase: {e}")


# --- AGENT SETUP ---
tools = [
     Tool(name="get_past_bids", func=get_past_bids, description="Input turn_id as integer. Retrieves concluded bids for a specific past turn from Firebase Firestore. Use this tool to get historical pricing data to analyze trends."),
     Tool(name="get_item_utility_score", func=get_item_utility_score, description="Input item name as string. Retrieves the utility score for a given item. This score indicates the strategic value of the item in the current state of the game.")
]

price_analyst_agent = Agent(
    name="price_analyst_expert",
    client=client,
    system_prompt='''You are an expert pricing analyst for an auction-based game. 
    Your goal is to determine the optimal bid price for ALL available food items for the current turn.
    
    You have access to tools that can fetch past bid history for previous turns and get the utility score for items.
    
    Follow these steps:
    1. Look at the bid history using the get_past_bids tool for recent turns to understand the market trends. 
    2. Consider the utility score of the items using the get_item_utility_score tool. Items with higher utility might warrant a higher bid.
    3. Calculate a new bid price for every item.
    
    Output format MUST be a valid JSON object where keys are item names and values are the suggested bid price (integer). Do not include any other text.
    Example:
    {
       "Pizza Margherita": 150,
       "Pasta Carbonara": 200
    }
    ''',
    tools=tools
)



# --- MAIN EXECUTION ---
def get_bidding_prices(current_turn: int, ) -> dict:
    """
    Esegue l'agente catturandone l'output a terminale, salva su Firebase e crea il file di log in automatico.
    """
    log_message(f"🤖 Inizio calcolo prezzi per il turno {current_turn}...")
    prompt = f"Calculate the optimal bid prices for all items for turn {current_turn}. Review past turns if available."
    log_message(f"📝 Prompt inviato: {prompt}")
    
    # Catturiamo tutto l'output "grafico" della libreria Datapizza
    agent_output_capture = io.StringIO()
    with redirect_stdout(agent_output_capture):
        response = price_analyst_agent.run(prompt)
    
    # Aggiungiamo l'output catturato ai nostri log interni
    log_message(agent_output_capture.getvalue())
    
    prices = {}
    try:
        text = response.text.replace('```json', '').replace('```', '').strip()
        prices = json.loads(text)
        log_message(f"✅ Prezzi generati con successo: {json.dumps(prices)}")
        
        # Salvataggio su Firebase
        save_bids_to_firebase(current_turn, prices)
    except json.JSONDecodeError:
        log_message(f"❌ Failed to parse Agent response as JSON: {response.text}")
    
    # SALVATAGGIO NELLA CARTELLA DELL'AGENTE
    # Creiamo il percorso assoluto unendo AGENT_DIR al nome del file
    log_filename = os.path.join(AGENT_DIR, f"execution_log_turn_{current_turn}.txt")
    save_logs_to_file(log_filename)
    
    return prices