"""
trend_analyst/trend_analyst_agent.py
--------------------------------------
Datapizza-AI Agent that analyzes the current run and produces a strategic report:

  OUTPUT STRUCTURE:
  ─────────────────
  1. 💰 Customer WTP        — estimated spend per archetype
  2. 🥗 Top 20 Ingredients  — most contested from bid history
  3. 🍕 Top 10 Recipes      — best ROI based on demand + ingredients
  4. 🎯 Recommended Recipes — concrete pick list with prices for next run
  5. 🏅 Top Competitors      — what the leading restaurants are doing
  6. ⚡ Extra Intelligence   — market arbitrage, gaps, bid strategy
  7. 🏆 3 Actions            — immediately executable next-turn moves

Usage (standalone test):
    python -m trend_analyst.test_trend_analyst [turn_id]

Usage (async, from SSE handlers):
    from trend_analyst.trend_analyst_agent import a_run_trend_analysis
    report = await a_run_trend_analysis(turn_id=current_turn_id)
"""

import os
import asyncio
import logging
import concurrent.futures
from typing import Dict, List, Optional

# Load .env from project root BEFORE api is used at module level
from dotenv import load_dotenv
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH)

from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents import Agent
from datapizza.tools import tool

import api

logger = logging.getLogger(__name__)

# ── Lazy env reads (after load_dotenv above) ───────────────────────────────────
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")
TEAM_ID = os.getenv("TEAM_ID", api.TEAM_ID or "")

_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ── System Prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a ruthless competitive strategist for an intergalactic restaurant in Hackapizza 2.0.
Your goal: produce a structured intelligence report to dominate the next turn.

The report MUST contain ALL of the following sections, filled with real data from your tools:

---

## 💰 CUSTOMER WILLINGNESS TO PAY (WTP)
Analyze meals data and menus to understand price sensitivity per customer archetype:
- 🚀 Esploratore Galattico: low budget, low time → cheap & fast dishes (estimated spend: X cr)
- 💰 Astrobarone: high budget, fast → premium & quick (estimated spend: X cr)
- 🔭 Saggio del Cosmo: high quality, time-rich → prestige dishes (estimated spend: X cr)
- 👨‍👩‍👧‍👦 Famiglia Orbitale: value-for-money, time-rich → balanced (estimated spend: X cr)
Infer WTP from actual menu prices, which dishes were served, and competitor pricing.

## 🥗 TOP 20 INGREDIENTS
From bid history: rank the 20 most contested/valuable ingredients.
For each: rank, name, bidders count, avg bid, total qty wanted, scarcity assessment.

## 🏅 TOP COMPETITORS DEEP DIVE
Sort all restaurants by balance descending. For each of the TOP 3:
- Name, ID, balance, reputation
- Menu strategy: which dishes, price range, implied target archetype
- Ingredient focus: what they seem to bid heavily on
- ⚠️ BLIND SPOTS: Which customer archetypes or recipe categories are they ignoring? Identify high-demand dishes they do NOT offer or price poorly.

## 🎯 RECOMMENDED RECIPES FOR NEXT RUN (Strictly MAX 2)
Identify the ultimate "Moneyball" plays. You must suggest exactly 1 or 2 recipes to put on our menu that exploit the blind spots of the Top 3 competitors. Focus on high-demand items that top competitors under-invest in.
For each:
- Recipe name
- Suggested price (credits)
- Target archetype
- Rationale: Why does this exploit a specific weakness of the top competitors?

## ⚡ EXTRA INTELLIGENCE
- Market arbitrage: SELL offers cheap enough to resell at profit?
- Top 5 bid targets for next auction: exactly which ingredients to buy to securely craft your 2 recommended recipes.
- Suggested bid amounts derived from recent bid competition data.

## 🤖 JSON EXECUTION PLAN
Provide a strict JSON block (inside ```json ... ```) representing your exact recommended menu and bid strategy for the next turn. This will be copy-pasted directly into the game client.
CRITICAL BUDGET RULE: The total cost of all bids (sum of amount * quantity) MUST be strictly less than our current Balance (fetched from `get_our_restaurant_state`). Do not bankrupt the restaurant.

Use this exact format:
```json
{
  "menu": [
    { "name": "Exact Recipe Name 1", "price": 150 },
    { "name": "Exact Recipe Name 2", "price": 60 }
  ],
  "bids": {
    "Ingredient Name 1": {"amount": 25, "quantity": 2},
    "Ingredient Name 2": {"amount": 12, "quantity": 5}
  }
}
```

---

Use ALL available tools before writing the report. Do not skip any section.
"""


# ── Server availability check ──────────────────────────────────────────────────
class ServerUnavailableError(Exception):
    """Raised when the game server is down or not in an active game state."""
    pass


def _check_api_result(result, context: str = ""):
    """Returns result or raises ServerUnavailableError if the call failed."""
    if result is None:
        raise ServerUnavailableError(
            f"Server returned no data for '{context}'. "
            "The server may be down, the game may not be active, or authentication failed."
        )
    return result


# ── Async bridge ───────────────────────────────────────────────────────────────
def _sync_call(coro):
    """Run an async coroutine safely from a sync tool context (new thread+loop)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ── Menu item extractor (handles both list and {'items': [...]} schemas) ────────
def _extract_menu_items(menu_data) -> list:
    if not menu_data:
        return []
    if isinstance(menu_data, list):
        return menu_data
    if isinstance(menu_data, dict):
        return menu_data.get("items", [])
    return []


# ── Tools ──────────────────────────────────────────────────────────────────────

@tool
def get_all_restaurants_menus() -> str:
    """Fetches menus and state of ALL restaurants (our team + competitors).
    Returns: id, name, open status, balance, reputation, menu items with prices.
    Use to understand competitive landscape and pricing strategies."""
    try:
        restaurants = _check_api_result(_sync_call(api.get_restaurants()), "GET /restaurants")
    except ServerUnavailableError as e:
        return f"⚠️ {e}"
    except Exception as e:
        return f"Error fetching restaurants: {e}"

    # Sort by balance descending so the LLM sees ranking immediately
    restaurants = sorted(restaurants, key=lambda r: r.get("balance", 0), reverse=True)

    parts = []
    for rank, r in enumerate(restaurants, 1):
        rid = r.get("id", "?")
        name = r.get("name", f"Restaurant {rid}")
        is_open = r.get("isOpen", False)
        balance = r.get("balance", "?")
        reputation = r.get("reputation", "?")

        # menu is embedded in the restaurant object
        menu_items = _extract_menu_items(r.get("menu"))
        if not menu_items:
            # Fallback: try fetching separately
            try:
                raw_menu = _sync_call(api._make_request("GET", f"/restaurant/{rid}/menu"))
                menu_items = _extract_menu_items(raw_menu)
            except Exception:
                pass

        if menu_items:
            menu_str = " | ".join(
                f"{item.get('name', '?')} @ {item.get('price', '?')} cr"
                for item in menu_items
            )
        else:
            menu_str = "(empty menu)"

        is_us = " ← OUR RESTAURANT" if str(rid) == str(TEAM_ID) else ""
        parts.append(
            f"#{rank} [R{rid}] {name}{is_us}\n"
            f"  balance={balance} cr | reputation={reputation} | open={is_open}\n"
            f"  menu: {menu_str}"
        )

    return "\n\n".join(parts) or "No restaurants found."


@tool
def get_meals_for_turn(turn_id: int) -> str:
    """Fetches ALL meal orders for OUR restaurant in a given turn.
    Includes: client name/archetype, order text, dish served, executed=True means served.
    Use to measure demand and conversion rate per dish/archetype."""
    try:
        meals = _sync_call(api.get_meals(turn_id))
    except Exception as e:
        return f"Error fetching meals for turn {turn_id}: {e}"

    if not meals:
        return f"No meals data for turn {turn_id} (turn may not exist yet or server returned empty)."

    total = len(meals)
    served = sum(1 for m in meals if m.get("executed", False))
    lines = [f"Turn {turn_id}: {total} orders — ✅ {served} served, ❌ {total - served} missed\n"]

    for m in meals:
        status = "✅" if m.get("executed", False) else "❌"
        lines.append(
            f"  {status} [{m.get('clientName', '?')}] "
            f"ordered='{m.get('orderText', '?')}' "
            f"→ dish={m.get('dish', 'N/A')}"
        )
    return "\n".join(lines)


@tool
def get_bid_history_for_turn(turn_id: int) -> str:
    """Fetches the closed bid history for ALL restaurants in a given turn.
    Returns ingredient-grouped view: bidder count, price range, quantities.
    Critical for identifying scarce ingredients and calibrating future bids.
    Returns a warning if the turn has no bid history (turn didn't happen yet)."""
    try:
        bids = _sync_call(api.get_bid_history(turn_id))
    except Exception as e:
        return f"Error fetching bids for turn {turn_id}: {e}"

    if not bids:
        return (
            f"⚠️ No bid history for turn {turn_id}. "
            "The turn may not have occurred yet, or the closed_bid phase hasn't run."
        )

    by_ingredient: Dict[str, list] = {}
    for b in bids:
        ing_raw = b.get("ingredient", b.get("ingredientName", b.get("ingredient_name", "Unknown")))
        if isinstance(ing_raw, dict):
            ing = ing_raw.get("name", str(ing_raw))
        else:
            ing = str(ing_raw)
        by_ingredient.setdefault(ing, []).append(b)

    lines = [f"Bid History Turn {turn_id} — {len(bids)} bids on {len(by_ingredient)} ingredients:\n"]
    for ing, ibids in sorted(by_ingredient.items(), key=lambda x: len(x[1]), reverse=True):
        # Correctly extract bid price/amount
        prices = [b.get("bid", b.get("bidAmount", b.get("price", 0))) for b in ibids]
        total_qty = sum(b.get("quantity", 0) for b in ibids)
        lines.append(
            f"  {ing}: {len(ibids)} bidders | qty_wanted={total_qty} | "
            f"bid min={min(prices)} avg={round(sum(prices)/len(prices),1)} max={max(prices)}"
        )
        for b in sorted(ibids, key=lambda x: x.get("bid", x.get("bidAmount", 0)), reverse=True)[:4]:
            # Correctly extract restaurant name/id
            r_id = b.get("restaurant_name", b.get("team_name", b.get("restaurantId", b.get("restaurant_id", b.get("team_id", "?")))))
            amt = b.get("bid", b.get("bidAmount", b.get("price", "?")))
            qty = b.get("quantity", "?")
            won = b.get("won", b.get("fulfilled", b.get("executed", None)))
            won_tag = " [WON]" if won is True else (" [LOST]" if won is False else "")
            lines.append(f"      {r_id}: bid={amt} qty={qty}{won_tag}")

    return "\n".join(lines)


@tool
def get_our_restaurant_state() -> str:
    """Returns current state of OUR restaurant: balance, reputation, inventory, menu.
    Use as baseline before analyzing competitors."""
    try:
        info = _check_api_result(_sync_call(api.get_my_restaurant()), "GET /restaurant/{id}")
    except ServerUnavailableError as e:
        return f"⚠️ {e}"
    except Exception as e:
        return f"Error: {e}"

    inv_raw = info.get("inventory", {})
    if isinstance(inv_raw, dict):
        inv_str = ", ".join(f"{k}:{v}" for k, v in inv_raw.items() if v > 0) or "empty"
    elif isinstance(inv_raw, list):
        inv_str = ", ".join(
            f"{i.get('ingredient', i.get('name', '?'))}:{i.get('quantity', '?')}"
            for i in inv_raw
        ) or "empty"
    else:
        inv_str = str(inv_raw)

    menu_items = _extract_menu_items(info.get("menu"))
    menu_str = " | ".join(
        f"{i.get('name','?')} @ {i.get('price','?')} cr" for i in menu_items
    ) or "(no menu set)"

    return (
        f"OUR Restaurant [{info.get('name', 'Desf-AI')}] ID={TEAM_ID}\n"
        f"  Balance: {info.get('balance', '?')} cr | "
        f"Reputation: {info.get('reputation', '?')} | "
        f"Open: {info.get('isOpen', False)}\n"
        f"  Inventory: {inv_str}\n"
        f"  Current Menu: {menu_str}"
    )


@tool
def get_all_recipes() -> str:
    """Returns ALL available recipes sorted by prestige descending.
    Each entry: prestige, name, prep time (seconds), ingredients needed.
    Use to identify high-value recipes and map ingredient requirements."""
    try:
        recipes = _check_api_result(_sync_call(api.get_recipes()), "GET /recipes")
    except ServerUnavailableError as e:
        return f"⚠️ {e}"
    except Exception as e:
        return f"Error fetching recipes: {e}"

    lines = [f"{len(recipes)} recipes (sorted by prestige desc):\n"]
    for r in sorted(recipes, key=lambda x: x.get("prestige", 0), reverse=True):
        name = r.get("name", "?")
        prestige = r.get("prestige", 0)
        ms = r.get("preparationTimeMs", r.get("preparationTime", 0))
        sec = ms // 1000 if ms > 1000 else ms

        ings = r.get("ingredients", r.get("ingredientsList", {}))
        if isinstance(ings, dict):
            ing_str = ", ".join(f"{k}×{v}" for k, v in ings.items())
        elif isinstance(ings, list):
            ing_str = ", ".join(
                f"{i.get('name', i.get('ingredient', '?'))}×{i.get('quantity', 1)}"
                for i in ings
            )
        else:
            ing_str = str(ings)

        lines.append(f"  [{prestige}★] {name} ({sec}s) — {ing_str}")

    return "\n".join(lines)


@tool
def get_market_entries() -> str:
    """Returns all current open market buy/sell entries.
    SELL = ingredients available to buy now.
    BUY = what competitors desperately need (sell at premium).
    Use for arbitrage and alliance/sabotage detection."""
    try:
        entries = _sync_call(api.get_market_entries())
    except Exception as e:
        return f"Error fetching market: {e}"

    if not entries:
        return "No open market entries at this time."

    sells = [e for e in entries if e.get("side", "").upper() == "SELL"]
    buys = [e for e in entries if e.get("side", "").upper() == "BUY"]

    lines = [f"Market: {len(sells)} SELL offers, {len(buys)} BUY requests\n"]

    if sells:
        lines.append("── SELL offers (can buy these NOW) ──")
        for e in sorted(sells, key=lambda x: x.get("totalPrice", x.get("price", 9999))):
            ing_name = e.get("ingredient_name", e.get("ingredientName", "Unknown"))
            if isinstance(e.get("ingredient"), dict):
                ing_name = e.get("ingredient").get("name", ing_name)
            
            lines.append(
                f"  [#{e.get('id','?')}] {ing_name} "
                f"×{e.get('quantity','?')} @ {e.get('totalPrice', e.get('price','?'))} cr "
                f"(from R{e.get('createdByRestaurantId', e.get('restaurantId', e.get('restaurant_id', '?')))})"
            )

    if buys:
        lines.append("\n── BUY requests (sell to them) ──")
        for e in sorted(buys, key=lambda x: x.get("totalPrice", x.get("price", 0)), reverse=True):
            ing_name = e.get("ingredient_name", e.get("ingredientName", "Unknown"))
            if isinstance(e.get("ingredient"), dict):
                ing_name = e.get("ingredient").get("name", ing_name)

            lines.append(
                f"  [#{e.get('id','?')}] {ing_name} "
                f"×{e.get('quantity','?')} @ {e.get('totalPrice', e.get('price','?'))} cr "
                f"(R{e.get('createdByRestaurantId', e.get('restaurantId', e.get('restaurant_id', '?')))} is buying)"
            )

    return "\n".join(lines)


# ── Agent factory ──────────────────────────────────────────────────────────────
def _build_agent() -> Agent:
    return Agent(
        name="TrendAnalyst",
        client=_client,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            get_our_restaurant_state,
            get_all_restaurants_menus,
            get_meals_for_turn,
            get_bid_history_for_turn,
            get_all_recipes,
            get_market_entries,
        ],
        max_steps=16,
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def _dump_justification_data(turn_id: int, previous_turns: Optional[List[int]]):
    """Dumps all the tool outputs to a markdown file to justify the LLM's choices."""
    try:
        data = [
            f"# Raw Data Justification for Turn {turn_id}",
            "This file contains the raw data fetched by the Trend Analyst to justify its strategic choices.\n",
            "## 1. Our Restaurant State",
            "```\n" + get_our_restaurant_state() + "\n```\n",
            "## 2. All Restaurants Menus & Balances",
            "```\n" + get_all_restaurants_menus() + "\n```\n",
            f"## 3. Meals for Turn {turn_id}",
            "```\n" + get_meals_for_turn(turn_id) + "\n```\n",
        ]

        turns = sorted(set([turn_id] + (previous_turns or [])))
        data.append("## 4. Bid History")
        for t in turns:
            data.append(f"### Turn {t}")
            data.append("```\n" + get_bid_history_for_turn(t) + "\n```\n")

        data.extend([
            "## 5. Market Entries",
            "```\n" + get_market_entries() + "\n```\n",
            "## 6. Available Recipes",
            "```\n" + get_all_recipes() + "\n```\n"
        ])

        filename = f"trend_data_justification_turn_{turn_id}.md"
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        with open(filepath, "w") as f:
            f.write("\n".join(data))
        
        logger.info(f"Saved justification data to {filename}")
    except Exception as e:
        logger.error(f"Failed to dump justification data: {e}")

def run_trend_analysis(turn_id: int, previous_turns: Optional[List[int]] = None) -> str:
    """
    Synchronous full competitive intelligence analysis.

    Args:
        turn_id: Current (or most recent) game turn ID.
        previous_turns: Additional turn IDs for historical bid/meal data.

    Returns:
        Structured markdown report.
    """
    agent = _build_agent()
    turns = sorted(set([turn_id] + (previous_turns or [])))
    turns_str = ", ".join(str(t) for t in turns)

    # ── NEW: Dump the raw data fetched by the tools to a markdown file
    _dump_justification_data(turn_id, previous_turns)

    task = f"""
Produce the full strategic intelligence report for Hackapizza 2.0.

Fetch data in this order (execute ALL steps):
1. get_our_restaurant_state()
2. get_all_restaurants_menus()
3. get_meals_for_turn(turn_id={turn_id})
4. get_bid_history_for_turn(turn_id=X) for each of: [{turns_str}]
5. get_all_recipes()
6. get_market_entries()

If any tool returns a ⚠️ warning (server unavailable / no data), note it clearly in the
report section and continue with what IS available.

Then write the FULL report including every section:
💰 WTP | 🥗 Top 20 ingredients | 🍕 Top 10 recipes | 🎯 Recommended menu |
🏅 Top competitors deep dive | ⚡ Extra intelligence | 🏆 3 actions

Our restaurant ID = {TEAM_ID}. Turns analyzed: {turns_str}.
"""

    try:
        result = agent.run(task)
        return result.text
    except Exception as e:
        logger.error(f"TrendAnalyst failed: {e}")
        return f"❌ Analysis failed: {e}"


async def a_run_trend_analysis(turn_id: int, previous_turns: Optional[List[int]] = None) -> str:
    """Async-safe wrapper. Runs sync analysis in a thread pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_trend_analysis, turn_id, previous_turns)
