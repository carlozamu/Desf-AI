"""
seller/agent.py
───────────────
Agente Datapizza AI per vendere gli ingredienti in eccesso a fine serving.

Strategia:
  1. Legge il menu attivo del nostro ristorante.
  2. Legge le ricette di tutti i piatti del menu → trova gli ingredienti necessari.
  3. Legge l'inventario corrente del ristorante.
  4. Calcola l'eccesso: ingredienti in inventario NON usati da nessuna ricetta del menu.
  5. Recupera il prezzo d'acquisto da bids_log.jsonl.
  6. Chiama il tool MCP `sell_market` per ogni ingrediente in eccesso.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any

import aiohttp
from dotenv import load_dotenv

from datapizza.agents import Agent
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.tools import Tool
from game_client.logging_utils import log, log_context, set_log_context

# ─── Aggiunge la root del progetto al path ────────────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(dotenv_path=_ROOT / ".env")

# ─── Configurazione ───────────────────────────────────────────────────────────

REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")
TEAM_API_KEY   = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", ""))
TEAM_ID        = os.getenv("TEAM_ID", "")
BASE_URL       = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")

# Nome del tool MCP per vendere un ingrediente sul mercato
# Adatta qui se l'API usa un nome diverso (es. "create_market_listing")
MCP_SELL_TOOL  = "sell_market"

# File con lo storico dei bid per recuperare i prezzi d'acquisto
BID_LOG_FILE   = _ROOT / "bids_log.jsonl"

AGENT_DIR = str(_HERE)

# ─── Client AI ────────────────────────────────────────────────────────────────

_ai_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ─── Helpers HTTP ─────────────────────────────────────────────────────────────

def _sync_get(path: str, params: dict = None) -> Any:
    """Chiamata GET sincrona (usata dentro i Tool sincroni)."""
    import urllib.request, urllib.parse as urlp

    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlp.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": TEAM_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def _sync_mcp(tool_name: str, arguments: dict) -> Any:
    """Chiamata POST MCP sincrona."""
    import urllib.request as urlreq

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()
    req = urlreq.Request(
        f"{BASE_URL}/mcp",
        data=payload,
        headers={"x-api-key": TEAM_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


# ─── Tool functions ───────────────────────────────────────────────────────────

def get_inventory_and_menu() -> str:
    """
    Restituisce in JSON l'inventario corrente del ristorante
    e i piatti del menu attivo.
    """
    restaurant = _sync_get(f"/restaurant/{TEAM_ID}")
    menu_raw   = _sync_get(f"/restaurant/{TEAM_ID}/menu")

    # il menu può essere lista diretta o {"items": [...]}
    if isinstance(menu_raw, dict):
        menu_items = menu_raw.get("items", [])
    elif isinstance(menu_raw, list):
        menu_items = menu_raw
    else:
        menu_items = []

    inventory = {}
    if isinstance(restaurant, dict):
        inventory = restaurant.get("inventory", {})

    result = {
        "inventory": inventory,
        "menu": [{"name": i.get("name", ""), "price": i.get("price")} for i in menu_items],
    }
    return json.dumps(result, ensure_ascii=False)


def get_recipe_ingredients(recipe_names: List[str]) -> str:
    """
    Dato un array di nomi di ricette, restituisce in JSON
    un dict {recipe_name: {ingredient: qty_needed}} per
    ognuna delle ricette richieste.
    Input: recipe_names — lista di nomi (stringhe)
    """
    all_recipes = _sync_get("/recipes")
    if not isinstance(all_recipes, list):
        return json.dumps({"error": "Could not fetch recipes"})

    recipe_map = {r["name"]: r for r in all_recipes if "name" in r}
    result: Dict[str, Any] = {}
    for name in recipe_names:
        recipe = recipe_map.get(name)
        if recipe:
            result[name] = recipe.get("ingredients", {})
        else:
            result[name] = {}   # ricetta non trovata

    return json.dumps(result, ensure_ascii=False)


def get_bid_prices() -> str:
    """
    Legge bids_log.jsonl e restituisce un dict {ingredient: last_bid_price}
    basato sull'ultimo round di bid registrato.
    """
    if not BID_LOG_FILE.exists():
        return json.dumps({})
    try:
        lines = [l for l in BID_LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return json.dumps({})
        # Usa l'ultimo record disponibile
        last_record = json.loads(lines[-1])
        prices: Dict[str, float] = {}
        for bid in last_record.get("bids", []):
            ing   = bid.get("ingredient")
            price = bid.get("bid")
            if ing and price is not None:
                prices[ing] = float(price)
        return json.dumps(prices, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def sell_ingredient(ingredient: str, quantity: int, price: float) -> str:
    """
    Vende `quantity` unità di `ingredient` sul mercato a `price` per unità.
    Chiama il tool MCP 'sell_market'.
    Input:
      ingredient: nome dell'ingrediente
      quantity:   quantità intera da vendere
      price:      prezzo per unità (float)

    ⚠️  DRY RUN: la chiamata MCP reale è commentata per debug.
         Rimuovi il commento per attivare la vendita effettiva.
    """
    if quantity <= 0:
        return json.dumps({"skipped": f"quantity={quantity} ≤ 0"})

    # ── DRY RUN ─────────────────────────────────────────────────────────────
    # Decommenta la riga sotto per attivare la vendita reale sul mercato.
    # result = _sync_mcp(MCP_SELL_TOOL, {
    #     "ingredient": ingredient,
    #     "quantity":   int(quantity),
    #     "price":      float(price),
    # })
    # return json.dumps(result, ensure_ascii=False)
    # ────────────────────────────────────────────────────────────────────────

    dry_run = {"dry_run": True, "ingredient": ingredient, "quantity": quantity, "price": price}
    log("SELLER", f"Would sell (dry-run): {dry_run}", event="seller.dry_run.sell")
    return json.dumps(dry_run)


# ─── Agent setup ──────────────────────────────────────────────────────────────

_tools = [
    Tool(
        name="get_inventory_and_menu",
        func=get_inventory_and_menu,
        description=(
            "No input required. Returns a JSON with two keys: "
            "'inventory' (dict of ingredient→qty in stock) and "
            "'menu' (list of dishes currently on the menu). "
            "Call this FIRST to understand what we have and what we sell."
        ),
    ),
    Tool(
        name="get_recipe_ingredients",
        func=get_recipe_ingredients,
        description=(
            "Input: recipe_names (list of strings) — the dish names from the menu. "
            "Returns a JSON dict mapping each recipe to its required ingredients and quantities. "
            "Use this to figure out which ingredients are NEEDED by the menu dishes."
        ),
    ),
    Tool(
        name="get_bid_prices",
        func=get_bid_prices,
        description=(
            "No input required. Returns a JSON dict {ingredient: bid_price} "
            "with the price we paid per unit in the last closed bid phase. "
            "Use these prices as the sell price for surplus items."
        ),
    ),
    Tool(
        name="sell_ingredient",
        func=sell_ingredient,
        description=(
            "Input: ingredient (str), quantity (int), price (float). "
            "Lists the ingredient on the market at the given price per unit. "
            "Only call this for SURPLUS items — ingredients NOT needed by any menu recipe."
        ),
    ),
]

seller_agent = Agent(
    name="surplus_seller",
    client=_ai_client,
    system_prompt="""You are a surplus inventory seller for a restaurant in an auction game.

Your task:
1. Call get_inventory_and_menu() to see what ingredients we have and which dishes are on our menu.
2. Call get_recipe_ingredients() with the list of menu dish names to find out which ingredients are needed.
3. Compute which inventory ingredients are NOT needed by ANY menu recipe (pure surplus).
   - An ingredient is surplus if it is in the inventory but NOT in ANY recipe's ingredient list.
4. Call get_bid_prices() to retrieve the price we paid for each ingredient.
5. For each SURPLUS ingredient with quantity > 0, call sell_ingredient() using:
   - the surplus quantity (entire stock, since we don't need it)
   - the bid price we paid (or 1.0 if unknown)
6. Finally, output a short plain-text summary listing what you sold and at what price.

IMPORTANT:
- Never sell ingredients that are needed by a menu recipe, even partially.
- If the inventory is empty or no surplus exists, just say so and stop.
- Do NOT ask for confirmation — act immediately.
""",
    tools=_tools,
)


# ─── Public API ───────────────────────────────────────────────────────────────

async def run_seller_agent(turn_id: int) -> str:
    """
    Entry-point da chiamare durante serving_phase_started.
    Esegue l'agente in un thread separato (è sincrono internamente).
    Restituisce il testo di output dell'agente.
    """
    set_log_context(turn_id=turn_id, phase="serving")
    with log_context(component="seller.agent"):
        log("SELLER", f"Starting surplus seller for turn {turn_id}...", event="seller.started")

        prompt = (
            f"It is turn {turn_id}. Check the current inventory and menu, "
            f"identify surplus ingredients (not needed by any menu recipe), "
            f"and sell them at the bid price."
        )

        LOG_FILE = _HERE / "seller_agent_log.txt"

        def _run():
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                resp = seller_agent.run(prompt)
            captured = buf.getvalue()
            output = resp.text or captured

            # Salva log su file
            try:
                with LOG_FILE.open("a", encoding="utf-8") as f:
                    from datetime import datetime
                    f.write(f"\n{'='*60}\n")
                    f.write(f"TURN {turn_id} — {datetime.now().isoformat()}\n")
                    f.write(f"{'='*60}\n")
                    f.write(captured)         # output grezzo della libreria
                    f.write("\n--- AGENT RESPONSE ---\n")
                    f.write(output)
                    f.write("\n")
                log("SELLER", f"Log saved -> {LOG_FILE}", event="seller.log.saved")
            except Exception as log_exc:
                log(
                    "SELLER",
                    f"Could not save log: {log_exc}",
                    event="seller.log.save_failed",
                    level="WARN",
                    error_type=type(log_exc).__name__,
                    error_message=str(log_exc),
                )

            return output

        result = await asyncio.to_thread(_run)
        log("SELLER", "Seller agent completed", event="seller.completed", output_preview=str(result)[:240])
        return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Surplus Seller Agent")
    parser.add_argument("--turn_id", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run_seller_agent(args.turn_id))
