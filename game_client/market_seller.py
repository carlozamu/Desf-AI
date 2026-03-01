from __future__ import annotations

from typing import Any

from game_client.logging_utils import log


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _entry_id(entry: dict[str, Any]) -> str | None:
    raw = entry.get("id") or entry.get("entryId") or entry.get("entry_id")
    if raw is None:
        return None
    return str(raw)


def _ingredient_name(entry: dict[str, Any]) -> str:
    return str(entry.get("ingredient_name", entry.get("ingredient", ""))).strip()


def _is_entry_active(entry: dict[str, Any]) -> bool:
    status = str(entry.get("status", "")).strip().lower()
    if status in {"closed", "executed", "deleted", "cancelled", "canceled", "expired"}:
        return False
    if "is_active" in entry and bool(entry.get("is_active")) is False:
        return False
    return True


def _normalize_inventory(inventory: dict[str, int] | None) -> dict[str, int]:
    if not isinstance(inventory, dict):
        return {}
    out: dict[str, int] = {}
    for ingredient, qty in inventory.items():
        name = str(ingredient).strip()
        if not name:
            continue
        parsed_qty = _parse_int(qty, default=0)
        if parsed_qty > 0:
            out[name] = parsed_qty
    return out


def _build_buy_index(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buy_index: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if str(entry.get("side", "")).upper() != "BUY":
            continue
        if not _is_entry_active(entry):
            continue
        ingredient = _ingredient_name(entry)
        entry_id = _entry_id(entry)
        if not ingredient or not entry_id:
            continue
        quantity = max(1, _parse_int(entry.get("quantity", 1), default=1))
        price = max(0.0, _parse_float(entry.get("price", 0), default=0.0))
        buy_index.setdefault(ingredient.lower(), []).append(
            {
                "id": entry_id,
                "ingredient": ingredient,
                "quantity": quantity,
                "price": price,
            }
        )

    for ingredient in buy_index:
        buy_index[ingredient].sort(
            key=lambda row: (float(row["price"]), int(row["quantity"])),
            reverse=True,
        )
    return buy_index


def _build_bid_price_index(ingredient_bids: dict[str, int] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(ingredient_bids, dict):
        return out
    for ingredient, price in ingredient_bids.items():
        name = str(ingredient).strip().lower()
        if not name:
            continue
        parsed_price = _parse_int(price, default=0)
        if parsed_price > 0:
            out[name] = parsed_price
    return out


def _choose_sell_price(
    *,
    ingredient: str,
    buy_offers: list[dict[str, Any]],
    bid_price_index: dict[str, int],
    default_price: int,
    min_sell_price: int,
) -> int:
    candidates: list[int] = []
    bid_price = bid_price_index.get(str(ingredient).strip().lower())
    if bid_price is not None and bid_price > 0:
        candidates.append(int(bid_price))

    if buy_offers:
        max_buy_price = max(_parse_int(offer.get("price"), default=0) for offer in buy_offers)
        if max_buy_price > 0:
            candidates.append(max_buy_price)

    if candidates:
        target_price = max(candidates)
    else:
        target_price = max(1, _parse_int(default_price, default=1))

    return max(int(min_sell_price), int(target_price))


async def liquidate_inventory_on_stockout(
    *,
    api_client: Any,
    mcp_gateway: Any,
    inventory: dict[str, int] | None,
    ingredient_bids: dict[str, int] | None,
    default_price: int,
    min_sell_price: int,
) -> dict[str, Any]:
    """
    Stockout fallback strategy:
    1) If there are active BUY offers for our ingredients, execute those first.
    2) For leftovers, create SELL offers so inventory does not expire unused.
    """
    normalized_inventory = _normalize_inventory(inventory)
    summary: dict[str, Any] = {
        "sold_to_existing_buy": {},
        "listed_sell_entries": {},
        "sold_units": 0,
        "listed_units": 0,
        "failed_transactions": 0,
        "failed_entries": 0,
        "estimated_revenue": 0,
    }
    if not normalized_inventory:
        log(
            "MARKET",
            "Liquidazione stockout saltata: inventario vuoto",
            event="market.sell.stockout.skipped.empty_inventory",
        )
        return summary

    market_entries = await api_client.get_market_entries()
    if not isinstance(market_entries, list):
        market_entries = []
    buy_index = _build_buy_index(market_entries)
    bid_price_index = _build_bid_price_index(ingredient_bids)

    remaining: dict[str, int] = dict(normalized_inventory)

    # Step 1: satisfy active BUY entries (highest price first).
    for ingredient in sorted(remaining.keys()):
        available = int(remaining[ingredient])
        if available <= 0:
            continue

        buy_offers = buy_index.get(ingredient.lower(), [])
        if not buy_offers:
            continue

        for offer in buy_offers:
            if available <= 0:
                break
            quantity = int(offer["quantity"])
            offer_id = str(offer["id"])
            price = float(offer["price"])

            if quantity > available:
                log(
                    "MARKET",
                    (
                        f"BUY entry {offer_id} per '{ingredient}' richiede {quantity}, "
                        f"ma disponibili {available}: skip"
                    ),
                    event="market.sell.stockout.buy_offer.skipped.insufficient_stock",
                    ingredient=ingredient,
                    market_entry_id=offer_id,
                    requested_quantity=quantity,
                    available_quantity=available,
                )
                continue

            result = await mcp_gateway.call_tool(
                "execute_transaction",
                {"market_entry_id": offer_id},
            )
            if result is None:
                summary["failed_transactions"] += 1
                log(
                    "MARKET",
                    f"Transazione BUY fallita (entry {offer_id}, ingrediente '{ingredient}')",
                    event="market.sell.stockout.buy_offer.failed",
                    level="WARN",
                    ingredient=ingredient,
                    market_entry_id=offer_id,
                )
                continue

            available -= quantity
            summary["sold_to_existing_buy"][ingredient] = (
                int(summary["sold_to_existing_buy"].get(ingredient, 0)) + quantity
            )
            summary["sold_units"] += quantity
            summary["estimated_revenue"] += int(round(price * quantity))
            log(
                "MARKET",
                (
                    f"Vendita diretta su BUY esistente: '{ingredient}' x{quantity} "
                    f"@ {price:.1f}cr (entry {offer_id})"
                ),
                event="market.sell.stockout.buy_offer.executed",
                ingredient=ingredient,
                quantity=quantity,
                price=price,
                market_entry_id=offer_id,
            )

        remaining[ingredient] = available

    # Step 2: list leftovers as SELL entries.
    for ingredient in sorted(remaining.keys()):
        quantity = int(remaining[ingredient])
        if quantity <= 0:
            continue

        buy_offers = buy_index.get(ingredient.lower(), [])
        price = _choose_sell_price(
            ingredient=ingredient,
            buy_offers=buy_offers,
            bid_price_index=bid_price_index,
            default_price=default_price,
            min_sell_price=min_sell_price,
        )
        result = await mcp_gateway.call_tool(
            "create_market_entry",
            {
                "side": "SELL",
                "ingredient_name": ingredient,
                "quantity": quantity,
                "price": int(price),
            },
        )
        if result is None:
            summary["failed_entries"] += 1
            log(
                "MARKET",
                f"Listing SELL fallito per '{ingredient}' x{quantity} @ {price}cr",
                event="market.sell.stockout.listing.failed",
                level="WARN",
                ingredient=ingredient,
                quantity=quantity,
                price=price,
            )
            continue

        summary["listed_sell_entries"][ingredient] = {
            "quantity": quantity,
            "price": int(price),
        }
        summary["listed_units"] += quantity
        log(
            "MARKET",
            f"Listing SELL creato: '{ingredient}' x{quantity} @ {price}cr",
            event="market.sell.stockout.listing.created",
            ingredient=ingredient,
            quantity=quantity,
            price=price,
        )

    return summary
