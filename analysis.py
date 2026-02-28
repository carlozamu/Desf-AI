from tinydb import TinyDB
from collections import Counter

recipes_db = TinyDB('data_recipes.json')
restaurants_db = TinyDB('data_restaurants.json')

def analyze_ingredients():
    """Analizza quali ingredienti appaiono più spesso in tutte le ricette"""
    recipes = recipes_db.all()
    ingredient_counter = Counter()
    
    for r in recipes:
        ingredients = r.get("ingredients", {})
        for ing, qty in ingredients.items():
            ingredient_counter[ing] += qty
            
    print("\n==== TOP 15 INGREDIENTI PIÙ FREQUENTI NEL GIOCO ====")
    for ing, count in ingredient_counter.most_common(15):
        print(f"- {ing}: {count} ricette")

def analyze_recipe_efficiency():
    """Analizza l'efficienza delle ricette valutando il rapporto Prestigio / Tempo di Preparazione"""
    recipes = recipes_db.all()
    
    efficiency_list = []
    
    for r in recipes:
        prep_time_ms = r.get("preparationTimeMs", 1)
        # Convert ms to seconds or minutes for readability
        prep_time_sec = prep_time_ms / 1000.0
        
        prestige = r.get("prestige", 0)
        
        if prep_time_sec > 0:
            efficiency = prestige / prep_time_sec
            efficiency_list.append({
                "name": r.get("name"),
                "prestige": prestige,
                "time_sec": round(prep_time_sec, 2),
                "ratio": round(efficiency, 3)
            })
            
    # Sort by ratio (Prestige/Second)
    sorted_efficiency = sorted(efficiency_list, key=lambda x: x["ratio"], reverse=True)
    
    print("\n==== TOP 10 RICETTE PER EFFICIENZA (Prestigio / Tempo) ====")
    for item in sorted_efficiency[:10]:
        print(f"- {item['name']} | Prestigio: {item['prestige']} | Tempo: {item['time_sec']}s | Ratio: {item['ratio']}")
        
    print("\n==== BOTTOM 5 RICETTE (Le meno efficienti) ====")
    for item in sorted_efficiency[-5:]:
        print(f"- {item['name']} | Prestigio: {item['prestige']} | Tempo: {item['time_sec']}s | Ratio: {item['ratio']}")

def analyze_competitors():
     """Mostra i nomi dei team aperti in questo momento e la loro rep"""
     restaurants = restaurants_db.all()
     print(f"\n==== STATO RISTORANTI COMPETITOR Trovati: {len(restaurants)} ====")
     for r in restaurants:
         print(f"- Team {r.get('id')} ({r.get('name')}) | Open: {r.get('isOpen')} | Balance: {r.get('balance')} | Rep: {r.get('reputation')}")

if __name__ == "__main__":
    if len(recipes_db.all()) == 0:
         print("[!] Attenzione: Il DB delle ricette è vuoto. Esegui prima data_manager.py")
    else:
        print(f"Analisi di {len(recipes_db.all())} ricette trovate nel database...")
        analyze_ingredients()
        analyze_recipe_efficiency()
        analyze_competitors()
