import os
import firebase_admin
from firebase_admin import credentials, firestore
from collections import defaultdict
from ortools.linear_solver import pywraplp
from dotenv import load_dotenv

load_dotenv()

def get_db():
    if not firebase_admin._apps:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        else:
            firebase_admin.initialize_app()
    return firestore.client()

def get_recipes_data():
    """Recupera le ricette dal DB e le analizza."""
    db = get_db()
    docs = db.collection('recipes').stream()
    recipes = []
    for doc in docs:
        r = doc.to_dict()
        recipes.append(r)
    return recipes

def get_competitors_menus():
    """Legge i menu dei competitor dal DB se disponibili per calcolare la penalita' di competizione."""
    db = get_db()
    docs = db.collection('restaurants').stream()
    competitor_recipe_counts = defaultdict(int)
    my_team_id = os.getenv("TEAM_ID", "")
    
    for doc in docs:
        r = doc.to_dict()
        if r.get("id") == my_team_id:
            continue
        # Se il ristorante ha un menu (nello stato corrente o storico)
        menu = r.get("menu", [])
        if menu:
            for item in menu:
                # Se l'item è una stringa (il nome della ricetta) o un dizionario
                name = item if isinstance(item, str) else item.get("name")
                if name:
                    competitor_recipe_counts[name] += 1
                    
    return dict(competitor_recipe_counts)

def build_and_solve_model(recipes, competitor_counts, max_menu_size=10, ingredient_penalty=1.0, archetype="astrobarone", base_ingredient_cost=50):
    """
    Risolve il problema di ottimizzazione del menu con OR-Tools in base all'archetipo cliente target.
    Astrobarone: Massimizza prestigio, minimizza tempo fortemente, ignora costo.
    Saggio: Massimizza prestigio, ignora tempo.
    Esploratore: Minimizza tempo, minimizza costo ingredienti, ignora prestigio.
    
    Seleziona le top N ricette per un UNICO menù espanso (es. 10 piatti).
    """
    solver = pywraplp.Solver.CreateSolver('SCIP')
    if not solver:
        print("Scip solver non disponibile.")
        return []

    x = {}
    
    all_ingredients = set()
    for r in recipes:
        all_ingredients.update(r.get("ingredients", {}).keys())
        
    y = {}
    for i in all_ingredients:
        y[i] = solver.IntVar(0, 1, f"y_{i}")
        
    recipe_scores = {}
    
    for i, r in enumerate(recipes):
        name = r.get("name", f"Recipe_{i}")
        x[name] = solver.IntVar(0, 1, f"x_{name}")
        
        prestige = r.get("prestige", 0)
        prep_time_s = r.get("preparationTimeMs", 1) / 1000.0
        ingredient_count = len(r.get("ingredients", {}))
        est_cost = ingredient_count * base_ingredient_cost
        
        # Calcolo Score base su archetipo
        if archetype == "astrobarone":
            efficiency = (prestige * 2.0) - (prep_time_s * 1.5)
        elif archetype == "saggio":
             efficiency = prestige * 3.0
        elif archetype == "esploratore":
             efficiency = -(prep_time_s * 2.0) - (est_cost * 0.5)
        else:
            efficiency = prestige / prep_time_s if prep_time_s > 0 else 0
        
        comp_penalty = competitor_counts.get(name, 0) * 0.5 
        
        score = efficiency - comp_penalty
        recipe_scores[name] = score
        
        for ing in r.get("ingredients", {}).keys():
            solver.Add(x[name] <= y[ing])

    # Ora il menù è più grande, default 10
    solver.Add(sum(x.values()) <= max_menu_size)
    solver.Add(sum(x.values()) >= 1)

    objective = solver.Objective()
    for name in x:
        # Se vogliamo esplicitamente le migliori "singole", OR-Tools bilancierà ingredienti in base alla penalty.
        # Riduciamo l'impatto della penalty se il menu è grande, oppure lo teniamo.
        objective.SetCoefficient(x[name], recipe_scores[name])
        
    for ing in y:
        penalty = ingredient_penalty * 2.0 if archetype == "esploratore" else ingredient_penalty
        objective.SetCoefficient(y[ing], -penalty)
        
    objective.SetMaximization()

    status = solver.Solve()

    chosen_menu = []
    if status == pywraplp.Solver.OPTIMAL or status == pywraplp.Solver.FEASIBLE:
        print(f"Soluzione trovata per {archetype.upper()}! Obiettivo = {objective.Value():.2f}")
        for name in x:
            if x[name].solution_value() > 0.5:
                recipe = next(r for r in recipes if r.get("name") == name)
                
                # Calcolo Prezzo Dinamico
                ingredient_count = len(recipe.get("ingredients", {}))
                est_cost = ingredient_count * base_ingredient_cost
                prestige = recipe.get("prestige", 0)
                
                if archetype == "astrobarone":
                    price = int(est_cost * 1.5 + prestige * 2.5) # Ricarico altissimo per status
                elif archetype == "saggio":
                    price = int(est_cost * 1.2 + prestige * 1.5) # Ricarico medio-alto per qualità
                elif archetype == "esploratore":
                    price = int(est_cost * 1.1 + prestige * 0.2) # Ricarico minimo per volume
                else:
                    price = int(est_cost * 1.3 + prestige * 1.0)
                    
                chosen_menu.append({
                    "name": name,
                    "score": recipe_scores[name],
                    "recommended_price": price,
                    "ingredients": list(recipe.get("ingredients", {}).keys())
                })
        
        # Ordiniamo il risultato finale internamente per far vedere prima il top assoluto
        chosen_menu = sorted(chosen_menu, key=lambda x: x["score"], reverse=True)
    else:
        print("Nessuna soluzione trovata.")
        
    return chosen_menu

def main():
    print("Avvio estrazione dati...")
    recipes = get_recipes_data()
    competitors = get_competitors_menus()
    
    if not recipes:
        print("Nessuna ricetta trovata nel DB. Assicurati che data_manager.py abbia popolato i dati.")
        return
        
    print(f"Trovate {len(recipes)} ricette. Trovati menu per {len(competitors)} piatti competitors.")
    
    menu_size = 10
    print(f"\n[ASTROBARONE TARGET - {menu_size} Piatti]")
    ottimo_menu_astro = build_and_solve_model(recipes, competitors, max_menu_size=menu_size, ingredient_penalty=0.5, archetype="astrobarone", base_ingredient_cost=50)
    for i, item in enumerate(ottimo_menu_astro):
        print(f"#{i+1} -> {item['name']} | Score: {item['score']:.2f} | Prezzo: {item['recommended_price']}cr")
        print(f"      Ingredienti: {', '.join(item['ingredients'])}")
        
    print(f"\n[ESPLORATORE TARGET - {menu_size} Piatti]")
    ottimo_menu_esplo = build_and_solve_model(recipes, competitors, max_menu_size=menu_size, ingredient_penalty=0.5, archetype="esploratore", base_ingredient_cost=50)
    for i, item in enumerate(ottimo_menu_esplo):
        print(f"#{i+1} -> {item['name']} | Score: {item['score']:.2f} | Prezzo: {item['recommended_price']}cr")
        print(f"      Ingredienti: {', '.join(item['ingredients'])}")

if __name__ == "__main__":
    main()
