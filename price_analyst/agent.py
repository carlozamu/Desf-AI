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

# Numero massimo di run storiche da analizzare
_MAX_HISTORY_RUNS = 5

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
    """Legacy: legge una singola run da Firebase. Usa get_recent_bids() per l'analisi normale."""
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


def get_recent_bids() -> str:
    """
    Restituisce le ultime _MAX_HISTORY_RUNS run di bid history da Firebase,
    ordinate dalla più recente alla più vecchia.
    Non richiede input.
    """
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")
    db = init_firebase(cred_path)
    if not db:
        return json.dumps({"error": "No Firestore client available."})

    try:
        docs = (
            db.collection("bid_history")
            .order_by("timestamp", direction="DESCENDING")
            .limit(_MAX_HISTORY_RUNS)
            .stream()
        )
        runs = []
        for doc in docs:
            d = doc.to_dict()
            d["_doc_id"] = doc.id
            runs.append(d)
        if not runs:
            return json.dumps({"message": "No bid history found in Firebase."})
        log_message(f"📦 Caricate {len(runs)} run recenti da Firebase (max={_MAX_HISTORY_RUNS}).")
        return json.dumps(runs, default=str)
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
    Tool(
        name="get_recent_bids",
        func=get_recent_bids,
        description=(
            f"No input required. Returns the last {_MAX_HISTORY_RUNS} bid-history runs from Firebase, "
            "ordered from most recent to oldest. Each entry contains item names and their winning "
            "bid prices. Use this as your ONLY source of historical price data."
        ),
    ),
    Tool(
        name="get_item_utility_score",
        func=get_item_utility_score,
        description="Input item name as string. Retrieves the utility score for a given item.",
    ),
]

price_analyst_agent = Agent(
    name="price_analyst_expert",
    client=client,
    system_prompt=f'''You are an expert pricing analyst for an auction-based game.
    Your goal is to determine the optimal bid price for ALL available food items for the current turn.

    RULES:
    - Call get_recent_bids() ONCE to load the last {_MAX_HISTORY_RUNS} historical runs.
    - Do NOT call get_past_bids. Do NOT loop over turn IDs. One call to get_recent_bids is enough.
    - Use the historical data to compute a weighted average price per item (recent runs weigh more).
    - Optionally call get_item_utility_score for items whose utility you want to check.
    - Output ONLY a valid JSON object: keys = item names, values = suggested bid price (integer).
    - Do not include any explanation or extra text outside the JSON.

    Example output:
    {{
       "Farina Cosmica": 12,
       "Acqua di Cometa": 8
    }}
    ''',
    tools=tools,
)



# --- MAIN EXECUTION ---
def get_bidding_prices(current_turn: int, max_history: int = _MAX_HISTORY_RUNS) -> dict:
    """
    Esegue l'agente catturandone l'output a terminale, salva su Firebase e crea il file di log.
    Analizza solo le ultime `max_history` run (default: _MAX_HISTORY_RUNS).
    """
    log_message(f"🤖 Inizio calcolo prezzi per il turno {current_turn} (storia={max_history} run)...")
    prompt = (
        f"Calculate the optimal bid prices for all items for turn {current_turn}. "
        f"Use get_recent_bids() to load the last {max_history} historical runs — "
        f"do NOT fetch individual turns by ID."
    )
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
    log_filename = os.path.join(AGENT_DIR, f"execution_log_turn.txt")
    save_logs_to_file(log_filename)
    
    return prices