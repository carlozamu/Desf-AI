"""
game_client/market_buyer.py
───────────────────────────
Acquista dal mercato gli ingredienti mancanti rispetto alle ricette target.

Algoritmo:
  1. Calcola gli ingredienti richiesti per 1 porzione di ogni target_recipe.
  2. Confronta con l'inventario corrente (fetchato via get_restaurant_info).
  3. Per ogni ingrediente mancante cerca entry SELL nel mercato.
  4. Compra se il prezzo è ≤ soglia (bid_amount × MAX_PRICE_MULTIPLIER).
"""
from __future__ import annotations

from typing import Any

from game_client.logging_utils import log
from game_client.strategy import recipe_ingredients

MAX_PRICE_MULTIPLIER = 1.5


async def buy_missing_ingredients(
    mcp_gateway: Any,
    api_client: Any,
    target_recipes: list[dict[str, Any]],
    ingredient_bids: dict[str, int],
    default_bid: int = 20,
) -> dict[str, int]:
    """Buy ingredients missing from inventory that are needed by target_recipes.

    Returns a dict of {ingredient_name: qty_bought}.
    """
    if not target_recipes:
        return {}

    # 1. Compute required ingredients (1 portion per recipe)
    required: dict[str, int] = {}
    for recipe in target_recipes:
        for ing, qty in recipe_ingredients(recipe).items():
            required[ing] = required.get(ing, 0) + qty

    if not required:
        return {}

    # 2. Fetch current inventory
    restaurant_info = await api_client.get_restaurant_info()
    inventory: dict[str, int] = {}
    if isinstance(restaurant_info, dict):
        raw_inv = restaurant_info.get("inventory", {})
        if isinstance(raw_inv, dict):
            inventory = {str(k): int(v) for k, v in raw_inv.items() if v}

    # 3. Determine what is missing
    missing: dict[str, int] = {}
    for ing, needed in required.items():
        have = inventory.get(ing, 0)
        if have < needed:
            missing[ing] = needed - have

    if not missing:
        log("MARKET", "Nessun ingrediente mancante", event="market.buy.skipped.complete")
        return {}

    log(
        "MARKET",
        f"Ingredienti mancanti: {missing}",
        event="market.buy.missing",
        missing=missing,
    )

    # 4. Fetch market entries (SELL side)
    market_entries = await api_client.get_market_entries()
    sell_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in market_entries:
        if str(entry.get("side", "")).upper() != "SELL":
            continue
        ing_name = str(entry.get("ingredient_name", entry.get("ingredient", ""))).strip().lower()
        if not ing_name:
            continue
        sell_entries.setdefault(ing_name, []).append(entry)

    # Sort each ingredient's entries by price ascending
    for ing_name in sell_entries:
        sell_entries[ing_name].sort(key=lambda e: float(e.get("price", 0) or 0))

    # 5. Buy missing ingredients
    bought: dict[str, int] = {}
    for ing, qty_needed in missing.items():
        ing_lower = ing.lower()
        entries = sell_entries.get(ing_lower, [])
        if not entries:
            log(
                "MARKET",
                f"Nessuna offerta SELL per '{ing}'",
                event="market.buy.no_offer",
                level="WARN",
                ingredient=ing,
            )
            continue

        bid_price = ingredient_bids.get(ing, default_bid)
        price_threshold = bid_price * MAX_PRICE_MULTIPLIER

        for entry in entries:
            entry_price = float(entry.get("price", 0) or 0)
            if entry_price > price_threshold:
                log(
                    "MARKET",
                    f"'{ing}' troppo caro: {entry_price:.1f} > soglia {price_threshold:.1f}",
                    event="market.buy.price_too_high",
                    ingredient=ing,
                    price=entry_price,
                    threshold=price_threshold,
                )
                continue

            entry_id = entry.get("id") or entry.get("entryId") or entry.get("entry_id")
            if not entry_id:
                continue

            result = await mcp_gateway.call_tool(
                "execute_transaction", {"market_entry_id": str(entry_id)}
            )
            if result is not None:
                qty_bought = int(entry.get("quantity", 1))
                bought[ing] = bought.get(ing, 0) + qty_bought
                log(
                    "MARKET",
                    f"Acquistato '{ing}' x{qty_bought} a {entry_price:.1f}cr (entry {entry_id})",
                    event="market.buy.purchased",
                    ingredient=ing,
                    quantity=qty_bought,
                    price=entry_price,
                    entry_id=str(entry_id),
                )
                if bought[ing] >= qty_needed:
                    break
            else:
                log(
                    "MARKET",
                    f"Transazione fallita per entry {entry_id} ('{ing}')",
                    event="market.buy.transaction_failed",
                    level="WARN",
                    entry_id=str(entry_id),
                    ingredient=ing,
                )

    return bought
