"""
game_client/client_agent.py
----------------------------
Fast LLM-based client categorizer using datapizza-ai.

Given a client's order text and the active menu, classifies the client
archetype and selects the BEST available dish — handling ingredient-based
orders ("I want something with X, Y, Z") and dish-name orders.

Uses a_structured_response (single LLM call, no agent loop) for speed.
Timeout: ~8s max so we don't block the serving pipeline.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel

_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH, override=False)

from game_client.logging_utils import log  # noqa: E402
from game_client.strategy import recipe_ingredients  # noqa: E402

_REGOLO_API_KEY: str = os.getenv("REGOLO_API_KEY", "")
_client = None


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    if not _REGOLO_API_KEY:
        return None
    try:
        from datapizza.clients.openai_like import OpenAILikeClient

        _client = OpenAILikeClient(
            api_key=_REGOLO_API_KEY,
            model="gpt-oss-20b",  # Faster model for real-time serving
            base_url="https://api.regolo.ai/v1",
        )
    except Exception as exc:
        log(
            "AGENT",
            f"Impossibile inizializzare datapizza client: {exc}",
            event="client_agent.init.failed",
            level="WARN",
        )
        _client = None
    return _client


# ── Pydantic output schema ─────────────────────────────────────────────────────

class ClientDishSelection(BaseModel):
    dish_index: int = -1
    intolerances: list[str] = []
    reasoning: str = ""


# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the maitre d' of an intergalactic restaurant.
Your job: pick the BEST dish from the menu for the client's order.

IMPORTANT: Clients often order by listing INGREDIENTS they want (e.g. "I want something with X, Y, Z").
In this case, pick the dish whose ingredients have the MOST OVERLAP with the requested ingredients.

Rules:
1. dish_index MUST be the number of a dish from the menu (0, 1, 2, ...).
2. Match by ingredient overlap: count how many requested ingredients appear in each dish.
3. Pick the dish with the highest ingredient overlap.
4. If the client names a specific dish, match it directly.
5. If no dish has any matching ingredients, set dish_index to -1.
6. Extract any food intolerances mentioned into the intolerances list.
7. Keep reasoning very short (max 10 words).
"""


# ── Main async function ────────────────────────────────────────────────────────

_AI_TIMEOUT_SECONDS: float = 8.0


async def pick_dish_with_ai(
    order_text: str,
    client_name: str,
    active_menu: list[dict[str, Any]],
    all_recipes: list[dict[str, Any]] | None = None,
) -> tuple[str | None, list[str]]:
    """Use LLM to classify the client and pick the best dish from the active menu.

    Returns (dish_name, intolerances). dish_name is None if no suitable dish found.
    """
    if not active_menu:
        return None, []

    client = _get_client()
    if client is None:
        log(
            "AGENT",
            "Client agent non disponibile (API key mancante o init fallita)",
            event="client_agent.unavailable",
            level="WARN",
        )
        return None, []

    # Build recipe ingredient lookup
    recipes_by_name: dict[str, dict[str, int]] = {}
    if all_recipes:
        for r in all_recipes:
            rname = str(r.get("name", "")).strip().lower()
            if rname:
                recipes_by_name[rname] = recipe_ingredients(r)

    # Build compact menu with indices and ingredients
    menu_lines: list[str] = []
    menu_index_to_name: dict[int, str] = {}
    for idx, item in enumerate(active_menu):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        menu_index_to_name[idx] = name
        # Find ingredients for this dish
        ings = recipes_by_name.get(name.lower(), {})
        ing_str = ", ".join(ings.keys()) if ings else "unknown"
        menu_lines.append(f"{idx}. {name} | Ingredients: {ing_str}")
    menu_str = "\n".join(menu_lines)

    prompt = (
        f"Client: {client_name}\n"
        f"Order: {order_text}\n\n"
        f"Menu:\n{menu_str}\n\n"
        "Pick the best dish index."
    )

    try:
        response = await asyncio.wait_for(
            client.a_structured_response(
                input=prompt,
                output_cls=ClientDishSelection,
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=200,
                temperature=0.1,
            ),
            timeout=_AI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log(
            "AGENT",
            f"AI dish selection timeout dopo {_AI_TIMEOUT_SECONDS}s per '{client_name}'",
            event="client_agent.timeout",
            level="WARN",
            client_name=client_name,
        )
        return None, []
    except Exception as exc:
        log(
            "AGENT",
            f"AI dish selection fallita per '{client_name}': {exc}",
            event="client_agent.failed",
            level="ERROR",
            client_name=client_name,
            error_type=type(exc).__name__,
        )
        return None, []

    if not response or not response.structured_data:
        return None, []

    result: ClientDishSelection = response.structured_data[0]
    intolerances: list[str] = [str(i).strip().lower() for i in (result.intolerances or []) if i]
    dish_idx = result.dish_index

    if dish_idx < 0 or dish_idx not in menu_index_to_name:
        log(
            "AGENT",
            f"AI non ha scelto alcun piatto per '{client_name}' (idx={dish_idx})",
            event="client_agent.no_dish",
            level="WARN",
            client_name=client_name,
        )
        return None, intolerances

    dish = menu_index_to_name[dish_idx]
    log(
        "AGENT",
        f"'{client_name}' → '{dish}' (idx={dish_idx})",
        event="client_agent.match",
        client_name=client_name,
        dish=dish,
    )
    return dish, intolerances
