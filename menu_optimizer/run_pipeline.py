import asyncio
import os
import sys

# Assicuriamoci che il modulo api.py nella root sia visibile
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .data_manager import fetch_and_store_recipes, fetch_and_store_restaurants
from .planner import get_recipes_data, get_competitors_menus, build_and_solve_model
from visualizations.charts import generate_visuals

async def step_1_data_extraction_and_analysis():
    """
    Fase 1: Estrazione dati e salvataggio dei Visual.
    """
    print("\n" + "="*50)
    print("▶ FASE 1: Estrazione Dati e Analisi Grafica")
    print("="*50)
    print("1.1 Aggiornamento DB locale da API...")
    if not os.getenv("SERVER_API_KEY"):
         print("[!] API_KEY non trovata, l'estrazione dati potrebbe fallire se non connessi.")
    
    await fetch_and_store_recipes()
    await fetch_and_store_restaurants()
    
    print("\n1.2 Generazione Visualizzazioni...")
    recipes = get_recipes_data()
    generate_visuals(recipes)
    print("\n[OK] Fase 1 completata.\n")

async def step_2_menu_optimization(target_archetype="astrobarone", menu_size=10):
    """
    Fase 2: Generazione del Menù Ideale (Top {menu_size} Ricette) utilizzando OR-Tools.
    """
    print("\n" + "="*50)
    print(f"▶ FASE 2: Ottimizzazione Menù (Target: {target_archetype.upper()}, {menu_size} Piatti)")
    print("="*50)
    
    recipes = get_recipes_data()
    competitors = get_competitors_menus()
    
    if not recipes:
        print("[!] Errore: Nessuna ricetta trovata per l'ottimizzazione.")
        return []
        
    print(f"Dati in pasto al Solver: {len(recipes)} ricette totali, storico di {len(competitors)} piatti dei competitor.")
    
    base_ingredient_cost = 50 
    
    best_menu = build_and_solve_model(
        recipes=recipes, 
        competitor_counts=competitors, 
        max_menu_size=menu_size, 
        ingredient_penalty=0.5, 
        archetype=target_archetype, 
        base_ingredient_cost=base_ingredient_cost
    )
    
    print("\n" + "="*30)
    print(f"🏆 TOP {menu_size} RICETTE SELEZIONATE DAL PLANNER 🏆")
    print("="*30)
    for i, item in enumerate(best_menu):
        print(f"#{i+1} 🍽️  {item['name']} | Score: {item['score']:.2f} | Prezzo: {item['recommended_price']}cr")
        
    return best_menu

def step_3_save_menu_to_db(menu, target_archetype):
    """
    Fase 3: Salvataggio configurazione menù su Firestore con ID progressivo.
    """
    print("\n" + "="*50)
    print("▶ FASE 3: Persistenza Menù nel Database")
    print("="*50)
    
    if not menu:
        print("[!] Nessun menu trovato, nulla da salvare.")
        return
        
    import firebase_admin
    from firebase_admin import credentials, firestore
    
    if not firebase_admin._apps:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        else:
            firebase_admin.initialize_app()
            
    db = firestore.client()
    
    runs_ref = db.collection('runs')
    existing_runs = list(runs_ref.stream())
    
    next_id = len(existing_runs) + 1
    run_doc_id = f"run_{next_id}"
    
    # Formattiamo per l'API
    api_ready_menu = [{"name": item["name"], "price": item["recommended_price"]} for item in menu]
    
    doc_ref = runs_ref.document(run_doc_id)
    doc_ref.set({
        "status": "initialized",
        "target_archetype": target_archetype,
        "initial_menu": api_ready_menu,  # Salvataggio diretto delle top 10
        "updated_at": firestore.SERVER_TIMESTAMP
    }, merge=True)
    
    print(f"✅ Run salvata con successo nel DB come '{run_doc_id}'.")
    print(f"   Contiene un menù potenziato di {len(api_ready_menu)} ricette.")

async def main():
    print("🚀 Inizializzazione Pipeline 'Hackapizza Agent' 🚀\n")
    
    await step_1_data_extraction_and_analysis()
    
    target = "astrobarone"
    # Adesso passiamo 10 come menu_size per generare una lista grande
    menu = await step_2_menu_optimization(target_archetype=target, menu_size=10)
    
    step_3_save_menu_to_db(menu, target)
    
    print("\n✅ Pipeline terminata. Pronti per la closed bid!")

if __name__ == "__main__":
    asyncio.run(main())
