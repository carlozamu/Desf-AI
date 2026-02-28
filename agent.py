import os
import logging
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents import Agent
from datapizza.tools import Tool
import api
from memory import GameMemory

logger = logging.getLogger(__name__)

REGOLO_API_KEY = os.getenv("REGOLO_API_KEY")
if not REGOLO_API_KEY:
    logger.warning("REGOLO_API_KEY not found in environment, agent might not work.")

# Initialize AI Client
regolo_client = OpenAILikeClient(
    api_key=REGOLO_API_KEY,
    model="gpt-oss-120b",
    base_url="https://api.regolo.ai/v1"
)

# Reference to GameMemory
_memory: GameMemory = None

def init_agent(mem: GameMemory):
    global _memory
    _memory = mem

# --- Define custom tools for the Agent ---
async def get_ingredient_insight(ingredient_name: str, **kwargs) -> str:
    """Returns insights about an ingredient (average winning bid, scarcity)."""
    if not _memory:
        return "Memory not initialized."
    insight = _memory.get_ingredient_insight(ingredient_name)
    return f"Insight for {ingredient_name}: {insight}"

async def get_all_recipes(**kwargs) -> str:
     """Returns all available recipes from memory."""
     if not _memory:
        return "Memory not initialized."
     return str(_memory.get_recipes())

async def get_current_turn_state(**kwargs) -> str:
    """Returns the current turn ID, phase, inventory, and active orders."""
    if not _memory:
        return "Memory not initialized."
    return str(_memory.get_current_state())

async def get_my_restaurant_info(**kwargs) -> str:
     """Returns my restaurant info directly from the API."""
     info = await api.get_my_restaurant()
     return str(info) if info else "Failed to get info."

# Create Tool objects for Datapizza AI
tools = [
     Tool(name="get_ingredient_insight", func=get_ingredient_insight, description="Input ingredient name as string. Returns insights about an ingredient (average winning bid, scarcity)."),
     Tool(name="get_all_recipes", func=get_all_recipes, description="Returns all available recipes with ingredients and time needed."),
     Tool(name="get_current_turn_state", func=get_current_turn_state, description="Returns the current turn ID, phase, inventory, and active orders."),
     Tool(name="get_my_restaurant_info", func=get_my_restaurant_info, description="Returns my restaurant balance, reputation, inventory, menu.")
]

def make_decision(prompt: str) -> str:
    """Invokes the Agent to make a decision based on the current situation."""
    system_prompt = """
    You are the manager of an intergalactic restaurant participating in Hackapizza 2.0.
    Your goal is to maximize the restaurant's balance by making smart decisions based on data.
    You manage a dynamic universe where:
    - Ingredients expire every turn.
    - Closed bid auctions must be smart, buying only what is needed without overpaying.
    - The Open market can be used for exploiting competitor weaknesses.
    - The Menu must be tailored to specific customer archetypes.
    Use the available tools to query the current state and decide what to do.
    """
    
    agent = Agent(
        name="RestaurantManager",
        client=regolo_client,
        system_prompt=system_prompt,
        tools=tools,
    )
    
    try:
        response = agent.run(prompt)
        return response
    except Exception as e:
         logger.error(f"Agent decision failed: {e}")
         return f"Error: {e}"
