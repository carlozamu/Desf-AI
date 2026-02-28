import sys
import os
import json
import asyncio
from typing import Dict, List, Any

# Assicuriamoci che il modulo api.py nella root sia visibile
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore
from datapizza.tools import tool

# --- Moduli di Root ---
try:
    import api
    from api import call_mcp_tool
except ImportError:
    print("Warning: api.py not found, API features will fail.")
    api = None
    async def call_mcp_tool(*args, **kwargs): return {"error": "api module missing"}


# ==========================================
# FIREBASE UTILITIES
# ==========================================

def init_firebase(cred_path: str):
    """Initializes the Firebase app and returns the Firestore client."""
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        print(f"⚠️ Firebase initialization error: {e}. Check the JSON file path.")
        return None

def get_firestore_client():
    """Helper to get a configured firestore client based on env vars."""
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")
    return init_firebase(cred_path)

def get_prices_from_firebase(turn_id: int) -> Dict[str, float]:
    """Fetches the price estimates saved by the price analyst on Firebase."""
    db = get_firestore_client()
    if not db:
        return {}

    try:
        doc = db.collection("runs").document(f"run_{turn_id}").get()
        return doc.to_dict().get("bids_prices", {}) if doc.exists else {}
    except Exception as e:
        print(f"Error fetching prices from Firebase: {e}")
        return {}

def get_initial_menu_from_firebase(turn_id: int) -> List[Dict]:
    """Fetches the initial optimized menu saved by the original pipeline on Firebase."""
    db = get_firestore_client()
    if not db:
        return []

    try:
        doc = db.collection("runs").document(f"run_{turn_id}").get()
        return doc.to_dict().get("initial_menu", []) if doc.exists else {}
    except Exception as e:
        print(f"Error fetching initial menu from Firebase: {e}")
        return []


# ==========================================
# BID GENERATION LOGIC
# ==========================================

def generate_shopping_list_from_menu(menu: List[Dict], recipes: Dict) -> Dict[str, int]:
    """
    Given an ideal menu (list of dishes) and a catalog of recipes,
    calculates the total quantity of each ingredient needed.
    """
    shopping_list: Dict[str, int] = {}
    
    for item in menu:
        dish_name = item.get("name")
        if not dish_name:
            continue
            
        recipe = recipes.get(dish_name)
        if not recipe or "ingredients" not in recipe:
            print(f"Warning: Recipe for '{dish_name}' not found or has no ingredients.")
            continue
            
        for ingredient, quantity in recipe["ingredients"].items():
            shopping_list[ingredient] = shopping_list.get(ingredient, 0) + quantity
            
    return shopping_list

@tool
def calculate_bids(
    shopping_list: Dict[str, int], 
    price_estimates: Dict[str, float],
    risk_multiplier: float = 1.1
) -> List[Dict[str, Any]]:
    """
    Formulates a list of bids for the closed bid phase applying a risk multiplier.
    """
    bids = []
    for ingredient, quantity in shopping_list.items():
        if quantity <= 0:
            continue
            
        base_price = price_estimates.get(ingredient, 1.0)
        bid_price = round(base_price * risk_multiplier, 2)
        
        bids.append({
            "ingredient": ingredient,
            "bid": bid_price,
            "quantity": quantity
        })
    
    return bids


# ==========================================
# MAIN PIPELINE 
# ==========================================

async def place_bids_for_turn(
    turn_id: int, 
    menu: List[Dict] = None, 
    prices: Dict[str, float] = None, 
    risk_multiplier: float = 1.1
) -> Any:
    """
    Executes the bidding pipeline:
    1. Resolve initial menu & fetch recipes to identify required ingredients.
    2. Resolve estimated prices.
    3. Calculate optimal bids.
    4. Transmit bids to the server MCP endpoint.
    """
    print(f"\n--- [BIDDER] Pipeline started for Turn {turn_id} ---")

    # 1. Resolve Initial Menu
    if menu is None:
        print("[BIDDER] Fetching initial menu from Firebase...")
        menu = get_initial_menu_from_firebase(turn_id)

    if not menu:
        print("[BIDDER] No initial menu found. Aborting.")
        return {"status": "no_bids_needed", "reason": "empty_menu"}
        
    # 2. Fetch Recipes and Generate Shopping List
    print("[BIDDER] Fetching recipes catalog from server...")
    try:
        recipes_list = await api.get_recipes() if api else []
    except Exception as e:
        print(f"[BIDDER] ⚠️ Error fetching recipes: {e}")
        recipes_list = []
        
    recipes_map = {r["name"]: r for r in recipes_list} if recipes_list else {}
    shopping_list = generate_shopping_list_from_menu(menu, recipes_map)
    
    if not shopping_list:
        print("[BIDDER] Shopping list is empty after checking recipes. Aborting.")
        return {"status": "no_bids_needed", "reason": "empty_shopping_list"}
    
    print(f"[BIDDER] Calculated Shopping List: {len(shopping_list)} unique ingredients")
    
    # 3. Resolve Estimated Prices
    if prices is None:
        print("[BIDDER] Fetching price estimates from Firebase...")
        prices = get_prices_from_firebase(turn_id)
        
    if not prices:
        print("[BIDDER] ⚠️ Warning: Price estimates are empty. Using base defaults.")
        prices = {}
        
    # 4. Calculate Final Bids
    bids = calculate_bids(shopping_list, prices, risk_multiplier)
    
    if not bids:
        print("[BIDDER] No valid bids to place after calculations. Aborting.")
        return {"status": "no_bids_needed", "reason": "calculation_failed"}
       
    print(f"[BIDDER] Formulated {len(bids)} Bids Payload")

    # 5. Dispatch Bids to Server
    print("[BIDDER] Dispatching 'closed_bid' request to MCP Server...")
    response = await call_mcp_tool("closed_bid", {"bids": bids})
    
    print("[BIDDER] Server Response Received.")
    return response


# ==========================================
# DEBUG/TEST EXECUTION
# ==========================================

async def example_usage():
    print("[TEST] Running Bidder Module standalone execution.")
    # Mocking environment
    os.environ["TEAM_API_KEY"] = "MOCK_API_KEY"
    os.environ["TEAM_ID"] = "1"
    
    mock_turn_id = 7
    await place_bids_for_turn(turn_id=mock_turn_id)

if __name__ == "__main__":
    asyncio.run(example_usage())
