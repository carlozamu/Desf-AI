import asyncio
import os
import json
from tinydb import TinyDB, Query
import api

# Inizializza o apre i database locali
recipes_db = TinyDB('data_recipes.json')
restaurants_db = TinyDB('data_restaurants.json')

async def fetch_and_store_recipes():
    """Scarica le ricette dal server e le fa l'upsert su TinyDB"""
    print("Fetching recipes da API...")
    recipes = await api.get_recipes()
    if recipes:
        Recipe = Query()
        count = 0
        for recipe in recipes:
            name = recipe.get("name")
            if name:
                # Upsert inserisce la ricetta se non c'è, altrimenti l'aggiorna
                recipes_db.upsert(recipe, Recipe.name == name)
                count += 1
        print(f"[OK] Salvate {count} ricette in data_recipes.json")
    else:
        print("[ERROR] Nessuna ricetta trovata o errore di connessione")

async def fetch_and_store_restaurants():
    """Scarica lo stato degli altri ristoranti al momento attuale per capire chi è online"""
    print("Fetching status ristoranti da API...")
    restaurants = await api.get_restaurants()
    if restaurants:
        Restaurant = Query()
        count = 0
        for r in restaurants:
            r_id = r.get("id")
            if r_id:
                restaurants_db.upsert(r, Restaurant.id == r_id)
                count += 1
        print(f"[OK] Salvate info di {count} ristoranti in data_restaurants.json")
    else:
         print("[ERROR] Impossibile recuperare info sui ristoranti")

if __name__ == "__main__":
    if not os.getenv("TEAM_API_KEY"):
         print("Assicurati di lanciare con `uv run --env-file .env data_manager.py`")
         exit(1)
         
    async def main():
        await fetch_and_store_recipes()
        await fetch_and_store_restaurants()
        
    asyncio.run(main())
