import aiohttp
import os
import logging
from typing import Dict, Any, List

TEAM_ID = os.getenv("TEAM_ID", "")
TEAM_API_KEY = os.getenv("TEAM_API_KEY", os.getenv("SERVER_API_KEY", ""))
BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")

logger = logging.getLogger(__name__)

async def _make_request(method: str, endpoint: str, params: Dict = None, json: Dict = None) -> Any:
    url = f"{BASE_URL}{endpoint}"
    headers = {"x-api-key": TEAM_API_KEY}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, headers=headers, params=params, json=json) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"API Request failed: {method} {url} - {e}")
            return None

async def get_meals(turn_id: int) -> List[Dict]:
    return await _make_request("GET", "/meals", params={"turn_id": turn_id, "restaurant_id": TEAM_ID})

async def get_restaurants() -> List[Dict]:
    return await _make_request("GET", "/restaurants")

async def get_recipes() -> List[Dict]:
    return await _make_request("GET", "/recipes")

async def get_bid_history(turn_id: int) -> List[Dict]:
    return await _make_request("GET", "/bid_history", params={"turn_id": turn_id})

async def get_my_restaurant() -> Dict:
    return await _make_request("GET", f"/restaurant/{TEAM_ID}")

async def get_my_menu() -> List[Dict]:
    return await _make_request("GET", f"/restaurant/{TEAM_ID}/menu")

async def get_market_entries() -> List[Dict]:
    return await _make_request("GET", "/market/entries")

async def call_mcp_tool(tool_name: str, arguments: Dict) -> Dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    return await _make_request("POST", "/mcp", json=payload)
