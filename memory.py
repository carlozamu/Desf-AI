import json
import os
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

MEMORY_FILE = "game_memory.json"

class GameMemory:
    def __init__(self):
        self.state = {
            "recipes": {},
            "ingredients_knowledge": {},
            "competitors_intel": {},
            "customer_insights": {},
            "current_turn_state": {
                "turn_id": 0,
                "phase": "stopped",
                "inventory": {},
                "active_orders": []
            }
        }
        self.load()

    def load(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r") as f:
                    data = json.load(f)
                    # Merge loaded data with default structure to prevent missing keys
                    for key in self.state:
                        if key in data:
                            self.state[key] = data[key]
                logger.info("Memory loaded successfully.")
            except Exception as e:
                logger.error(f"Failed to load memory: {e}")

    def save(self):
        try:
            with open(MEMORY_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")

    # --- Turn & Phase ---
    def update_turn(self, turn_id: int):
        self.state["current_turn_state"]["turn_id"] = turn_id
        self.save()

    def update_phase(self, phase: str):
        self.state["current_turn_state"]["phase"] = phase
        self.save()

    # --- Recipes ---
    def update_recipes(self, recipes_list: List[Dict]):
        for recipe in recipes_list:
            name = recipe.get("name")
            if name:
                self.state["recipes"][name] = recipe
        self.save()
        
    def get_recipes(self) -> Dict:
         return self.state["recipes"]

    # --- Ingredients Knowledge ---
    def update_ingredient_bid(self, ingredient: str, winning_bid: float):
        if ingredient not in self.state["ingredients_knowledge"]:
            self.state["ingredients_knowledge"][ingredient] = {
                "avg_winning_bid": winning_bid,
                "bid_history": []
            }
        
        history = self.state["ingredients_knowledge"][ingredient]["bid_history"]
        history.append(winning_bid)
        
        # Keep only the last 5 turns for moving average
        if len(history) > 5:
            history.pop(0)
            
        avg = sum(history) / len(history)
        self.state["ingredients_knowledge"][ingredient]["avg_winning_bid"] = avg
        self.save()

    # --- Competitor Intel ---
    def log_competitor_market_action(self, team_id: str, action: str, ingredient: str, price: float, quantity: int):
         if team_id not in self.state["competitors_intel"]:
             self.state["competitors_intel"][team_id] = {
                 "market_actions": []
             }
             
         self.state["competitors_intel"][team_id]["market_actions"].append({
             "action": action, # 'BUY' or 'SELL'
             "ingredient": ingredient,
             "price": price,
             "quantity": quantity
         })
         self.save()

    # --- Helper methods for LLM Tools ---
    def get_ingredient_insight(self, ingredient: str) -> Dict:
        return self.state["ingredients_knowledge"].get(ingredient, {"avg_winning_bid": "Unknown"})
        
    def get_current_state(self) -> Dict:
        return self.state["current_turn_state"]
