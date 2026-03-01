import sys
import os
import asyncio
from typing import Dict, List, Any

# Assicuriamoci che il modulo api.py nella root sia visibile
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore
from datapizza.tools import tool
from game_client.logging_utils import log, log_context, set_log_context

# --- Moduli di Root ---
try:
    import api
    from api import call_mcp_tool
except ImportError:
    log(
        "WARN",
        "api.py not found, API features will fail",
        event="bidder.api.unavailable",
        level="WARN",
    )
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
        log(
            "TRACK",
            f"Firebase initialization error: {e}. Check the JSON file path.",
            event="firebase.init.failed",
            level="ERROR",
            error_type=type(e).__name__,
            error_message=str(e),
        )
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
        log(
            "TRACK",
            f"Error fetching prices from Firebase: {e}",
            event="bidder.prices.fetch.failed",
            level="ERROR",
            turn_id=turn_id,
            error_type=type(e).__name__,
            error_message=str(e),
        )
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
        log(
            "TRACK",
            f"Error fetching initial menu from Firebase: {e}",
            event="bidder.menu.fetch.failed",
            level="ERROR",
            turn_id=turn_id,
            error_type=type(e).__name__,
            error_message=str(e),
        )
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
            log(
                "BID",
                f"Recipe for '{dish_name}' not found or has no ingredients.",
                event="bidder.shopping_list.recipe_missing",
                level="WARN",
                dish_name=dish_name,
            )
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
    set_log_context(turn_id=turn_id, phase="closed_bid")
    with log_context(component="bidder.bidder"):
        log("BID", f"Pipeline started for turn {turn_id}", event="bidder.pipeline.started")

        # 1. Resolve Initial Menu
        if menu is None:
            log("BID", "Fetching initial menu from Firebase", event="bidder.menu.fetch.started")
            menu = get_initial_menu_from_firebase(turn_id)

        if not menu:
            log("BID", "No initial menu found. Aborting.", event="bidder.pipeline.no_menu", level="WARN")
            return {"status": "no_bids_needed", "reason": "empty_menu"}

        # 2. Fetch Recipes and Generate Shopping List
        log("BID", "Fetching recipes catalog from server", event="bidder.recipes.fetch.started")
        try:
            recipes_list = await api.get_recipes() if api else []
        except Exception as e:
            log(
                "BID",
                f"Error fetching recipes: {e}",
                event="bidder.recipes.fetch.failed",
                level="ERROR",
                error_type=type(e).__name__,
                error_message=str(e),
            )
            recipes_list = []

        recipes_map = {r["name"]: r for r in recipes_list} if recipes_list else {}
        shopping_list = generate_shopping_list_from_menu(menu, recipes_map)

        if not shopping_list:
            log(
                "BID",
                "Shopping list is empty after checking recipes. Aborting.",
                event="bidder.pipeline.no_shopping_list",
                level="WARN",
            )
            return {"status": "no_bids_needed", "reason": "empty_shopping_list"}

        log(
            "BID",
            f"Calculated shopping list: {len(shopping_list)} unique ingredients",
            event="bidder.shopping_list.generated",
            ingredients_count=len(shopping_list),
        )

        # 3. Resolve Estimated Prices
        if prices is None:
            log("BID", "Fetching price estimates from Firebase", event="bidder.prices.fetch.started")
            prices = get_prices_from_firebase(turn_id)

        if not prices:
            log(
                "BID",
                "Price estimates are empty. Using base defaults.",
                event="bidder.prices.empty_defaults",
                level="WARN",
            )
            prices = {}

        # 4. Calculate Final Bids
        bids = calculate_bids(shopping_list, prices, risk_multiplier)

        if not bids:
            log(
                "BID",
                "No valid bids to place after calculations. Aborting.",
                event="bidder.pipeline.no_bids",
                level="WARN",
            )
            return {"status": "no_bids_needed", "reason": "calculation_failed"}

        log("BID", f"Formulated {len(bids)} bids payload", event="bid.payload.built", bids_count=len(bids))

        # 5. Dispatch Bids to Server
        log("BID", "Dispatching closed_bid request to MCP server", event="bid.submit.started")
        response = await call_mcp_tool("closed_bid", {"bids": bids})
        if isinstance(response, dict) and response.get("error"):
            log(
                "BID",
                f"closed_bid response error: {response.get('error')}",
                event="bid.submit.failed",
                level="ERROR",
            )
        else:
            log("BID", "closed_bid response received", event="bid.submitted")
        return response


# ==========================================
# DEBUG/TEST EXECUTION
# ==========================================

async def example_usage():
    log("INIT", "Running Bidder module standalone execution", event="bidder.example.started")
    # Mocking environment
    os.environ["TEAM_API_KEY"] = "MOCK_API_KEY"
    os.environ["TEAM_ID"] = "1"
    
    mock_turn_id = 7
    await place_bids_for_turn(turn_id=mock_turn_id)

if __name__ == "__main__":
    asyncio.run(example_usage())
