import asyncio
import data_manager
import analysis

async def main():
    print("--- 1. Download persistente dati (Data Manager) ---")
    await data_manager.fetch_and_store_recipes()
    await data_manager.fetch_and_store_restaurants()
    
    print("\n--- 2. Esecuzione Analisi Strategica (Analysis) ---")
    analysis.analyze_ingredients()
    analysis.analyze_recipe_efficiency()
    analysis.analyze_competitors()
    
if __name__ == "__main__":
    asyncio.run(main())
