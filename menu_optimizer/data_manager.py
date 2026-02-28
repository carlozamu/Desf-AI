import asyncio
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

import api

if not firebase_admin._apps:
    import os
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
    else:
        firebase_admin.initialize_app()

db = firestore.client()

async def fetch_and_store_recipes():
    """Scarica le ricette dal server e le fa l'upsert su TinyDB"""
    print("Fetching recipes da API...")
    recipes = await api.get_recipes()
    if recipes:
        count = 0
        for recipe in recipes:
            name = recipe.get("name")
            if name:
                # Upsert su Firestore
                db.collection('recipes').document(str(name)).set(recipe, merge=True)
                count += 1
        print(f"[OK] Salvate {count} ricette in data_recipes.json")
    else:
        print("[ERROR] Nessuna ricetta trovata o errore di connessione")

async def fetch_and_store_restaurants():
    """Scarica lo stato degli altri ristoranti al momento attuale per capire chi è online"""
    print("Fetching status ristoranti da API...")
    restaurants = await api.get_restaurants()
    if restaurants:
        count = 0
        for r in restaurants:
            r_id = r.get("id")
            if r_id:
                db.collection('restaurants').document(str(r_id)).set(r, merge=True)
                count += 1
        print(f"[OK] Salvate info di {count} ristoranti in data_restaurants.json")
    else:
         print("[ERROR] Impossibile recuperare info sui ristoranti")

if __name__ == "__main__":
    if not os.getenv("SERVER_API_KEY"):
         print("Assicurati di esportare la variabile SERVER_API_KEY o caricare le variabili d'ambiente prima di eseguire lo script.")
         exit(1)
         
    async def main():
        await fetch_and_store_recipes()
        await fetch_and_store_restaurants()
        
    asyncio.run(main())
