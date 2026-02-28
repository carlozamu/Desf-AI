"""
chef/chef_agent.py
------------------
Chef Agent — decides the daily menu to maximize profit.

Input  : target_audience (str)
Output : (markdown_plan: str, shopping_list: dict[str, int])
         shopping_list is also persisted to Firestore under chef_plans/{turn_id}
"""
import os
import json
import re
import logging
from typing import Dict
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents import Agent
from datapizza.tools import Tool

from memory import GameMemory
from chef.prompts import build_chef_prompt

logger = logging.getLogger(__name__)

# ── Firebase (idempotent) ─────────────────────────────────────────────────────
if not firebase_admin._apps:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    firebase_admin.initialize_app(
        credentials.Certificate(cred_path) if cred_path else None
    )

db = firestore.client()

# ── AI Client ─────────────────────────────────────────────────────────────────
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY")
if not REGOLO_API_KEY:
    logger.warning("REGOLO_API_KEY not set — Chef Agent will not work.")

regolo_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1",
)

# ── Shared state ──────────────────────────────────────────────────────────────
_memory: GameMemory = None


def init_chef_agent(mem: GameMemory) -> None:
    """Call once at startup to bind the agent to the shared GameMemory."""
    global _memory
    _memory = mem


# ── Tools ─────────────────────────────────────────────────────────────────────
MOCK_INVENTORY = {
    "Pomodoro Spaziale": 10,
    "Farina Quantica": 5,
    "Formaggio Nebulare": 3,
}


async def get_available_recipes(**kwargs) -> str:
    """Returns all available recipes with ingredients and preparation time."""
    recipes = _memory.get_recipes() if _memory else {}
    return json.dumps(recipes, indent=2)


async def get_current_inventory(**kwargs) -> str:
    """Returns the current ingredient inventory of the restaurant."""
    inventory = {}
    if _memory:
        inventory = _memory.get_current_state().get("inventory", {})
    return json.dumps(inventory or MOCK_INVENTORY, indent=2)


_tools = [
    Tool(
        name="get_available_recipes",
        func=get_available_recipes,
        description="Returns all available recipes with ingredients and prep time.",
    ),
    Tool(
        name="get_current_inventory",
        func=get_current_inventory,
        description="Returns the current ingredient inventory of the restaurant.",
    ),
]


# ── Shopping list parser ───────────────────────────────────────────────────────
def _parse_shopping_list(markdown_text: str) -> Dict[str, int]:
    """
    Extracts the shopping list from the agent's markdown output.
    Looks for ```text ... ``` blocks where each line is:
        <Ingredient Name>  <quantity>
    (two or more spaces between name and number)
    """
    shopping_list: Dict[str, int] = {}
    code_blocks = re.findall(r"```(?:text)?\s*\n(.*?)```", markdown_text, re.DOTALL)
    for block in code_blocks:
        for line in block.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(.+?)\s{2,}(\d+)\s*$", line)
            if m:
                ingredient = m.group(1).strip()
                qty = int(m.group(2))
                shopping_list[ingredient] = shopping_list.get(ingredient, 0) + qty
    return shopping_list


# ── Firebase persistence ───────────────────────────────────────────────────────
def save_shopping_list_to_firebase(
    shopping_list: Dict[str, int],
    turn_id: int = 0,
    target_audience: str = "",
) -> None:
    """
    Saves the shopping list to Firestore:
      Collection : chef_plans
      Document   : turn_{turn_id}
    """
    if not shopping_list:
        logger.warning("Shopping list is empty — skipping Firebase save.")
        return

    payload = {
        "shopping_list": shopping_list,
        "target_audience": target_audience,
        "turn_id": turn_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        db.collection("chef_plans").document(f"turn_{turn_id}").set(payload, merge=True)
        logger.info(f"Shopping list saved → Firestore chef_plans/turn_{turn_id}")
    except Exception as e:
        logger.error(f"Failed to save shopping list to Firebase: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
def plan_daily_menu(target_audience: str) -> tuple[str, Dict[str, int]]:
    """
    Run the Chef Agent for the given target audience.

    Returns
    -------
    markdown_plan : str
        Full LLM output (Markdown) with menu, profit estimate, backup dishes
        and shopping list.
    shopping_list : dict[str, int]
        Parsed ingredient → quantity dict; also persisted to Firestore.
    """
    turn_id: int = 0
    if _memory:
        turn_id = _memory.get_current_state().get("turn_id", 0)

    agent = Agent(
        name="ChefAgent",
        client=regolo_client,
        system_prompt=build_chef_prompt(target_audience),
        tools=_tools,
    )

    try:
        response = agent.run(
            "Plan today's menu based on the target audience and current inventory."
        )
        markdown_plan: str = getattr(response, "text", str(response))
    except Exception as e:
        logger.error(f"Chef Agent failed: {e}")
        return f"Error: {e}", {}

    shopping_list = _parse_shopping_list(markdown_plan)
    logger.info(f"Parsed shopping list: {shopping_list}")

    save_shopping_list_to_firebase(
        shopping_list, turn_id=turn_id, target_audience=target_audience
    )

    return markdown_plan, shopping_list
