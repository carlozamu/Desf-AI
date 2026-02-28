"""
chef/test_chef_agent.py
-----------------------
Manual test script for the Chef Agent.
Run from the project root: python -m chef.test_chef_agent
"""
import os
from dotenv import load_dotenv

load_dotenv()

from memory import GameMemory
from chef import chef_agent


def main():
    print("Initializing GameMemory...")
    mem = GameMemory()

    # Seed mock recipes if memory is empty
    if not mem.get_recipes():
        mem.update_recipes([
            {
                "name": "Galactic Margherita",
                "ingredients": {"Space Tomato": 2, "Quantum Flour": 1, "Nebula Cheese": 1},
                "preparationTimeMs": 3000,
                "prestige": 30,
            },
            {
                "name": "Void Sandwich",
                "ingredients": {"Quantum Flour": 1, "Synthetic Meat": 1},
                "preparationTimeMs": 1000,
                "prestige": 15,
            },
            {
                "name": "Star Soup",
                "ingredients": {"Star Dust": 2, "Asteroid Water": 1},
                "preparationTimeMs": 4000,
                "prestige": 55,
            },
        ])

    print("Binding memory to Chef Agent...")
    chef_agent.init_chef_agent(mem)

    for audience in ["Astrobaron", "Galactic Explorer"]:
        print(f"\n{'='*60}")
        print(f"  Chef Agent — Target: {audience}")
        print(f"{'='*60}")
        markdown_plan, shopping_list = chef_agent.plan_daily_menu(audience)
        print(markdown_plan)
        print(f"\n>>> Parsed shopping list: {shopping_list}")


if __name__ == "__main__":
    main()
