from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TrendPlan:
    source_turn_id: int
    target_turn_id: int
    menu: list[dict[str, int | str]]
    bids: dict[str, dict[str, int]]
    raw_report: str


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)


def _extract_json_object(report: str) -> dict[str, Any] | None:
    text = str(report or "").strip()
    if not text:
        return None

    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(1).strip() if match else text

    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # Fallback: find first balanced object in the text.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _normalize_menu(raw_menu: Any) -> list[dict[str, int | str]]:
    if not isinstance(raw_menu, list):
        return []

    menu: list[dict[str, int | str]] = []
    for item in raw_menu:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        if not name:
            continue

        price_raw = item.get("price")
        try:
            price = int(price_raw)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        menu.append({"name": name, "price": price})

    return menu


def _normalize_bids(raw_bids: Any) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}

    if isinstance(raw_bids, dict):
        items = raw_bids.items()
        for ingredient, row in items:
            if not isinstance(row, dict):
                continue
            name = str(ingredient).strip()
            if not name:
                continue

            amount_raw = row.get("amount", row.get("bid"))
            qty_raw = row.get("quantity")
            try:
                amount = int(amount_raw)
                quantity = int(qty_raw)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or quantity <= 0:
                continue
            out[name] = {"amount": amount, "quantity": quantity}
        return out

    if isinstance(raw_bids, list):
        for row in raw_bids:
            if not isinstance(row, dict):
                continue
            name = str(row.get("ingredient", "")).strip()
            if not name:
                continue
            amount_raw = row.get("amount", row.get("bid"))
            qty_raw = row.get("quantity")
            try:
                amount = int(amount_raw)
                quantity = int(qty_raw)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or quantity <= 0:
                continue
            out[name] = {"amount": amount, "quantity": quantity}

    return out


def parse_trend_plan(
    source_turn_id: int,
    target_turn_id: int,
    report: str,
) -> tuple[TrendPlan | None, str]:
    parsed = _extract_json_object(report)
    if not parsed:
        return None, "no valid JSON plan found in report"

    menu = _normalize_menu(parsed.get("menu"))
    bids = _normalize_bids(parsed.get("bids"))
    if not menu and not bids:
        return None, "trend plan JSON found but menu and bids are unusable"

    plan = TrendPlan(
        source_turn_id=int(source_turn_id),
        target_turn_id=int(target_turn_id),
        menu=menu,
        bids=bids,
        raw_report=str(report or ""),
    )
    return plan, ""


def plan_to_closed_bid_payload(plan: TrendPlan) -> list[dict[str, int | str]]:
    payload: list[dict[str, int | str]] = []
    for ingredient, row in sorted(plan.bids.items()):
        amount = int(row.get("amount", 0))
        quantity = int(row.get("quantity", 0))
        if amount <= 0 or quantity <= 0:
            continue
        payload.append(
            {
                "ingredient": ingredient,
                "bid": amount,
                "quantity": quantity,
            }
        )
    return payload
