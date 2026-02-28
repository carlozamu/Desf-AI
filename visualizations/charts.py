import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os

def generate_visuals(recipes):
    """
    Crea visualizzazioni per l'analisi e salva nella cartella visuals.
    Crea un fronte di Pareto per Efficienza (Tempo vs Prestigio) e un grafico degli ingredienti.
    """
    os.makedirs("visualizations", exist_ok=True)
    
    if not recipes:
        print("[!] Nessuna ricetta da visualizzare.")
        return
        
    print("🎨 Generazione Grafici Analitici (Visuals) in corso...")
    
    # 1. Preparazione DataFrame
    data = []
    for r in recipes:
        prep_time_s = r.get("preparationTimeMs", 1) / 1000.0
        prestige = r.get("prestige", 0)
        name = r.get("name", "Unknown")
        ingredients_count = len(r.get("ingredients", {}))
        data.append({
            "Name": name,
            "Tempo (s)": prep_time_s,
            "Prestigio": prestige,
            "Ingredienti": ingredients_count
        })
        
    df = pd.DataFrame(data)
    
    # Rimuoviamo eventuali outlier assurdi se presenti
    df = df[df["Tempo (s)"] > 0]
    
    # 2. Scatter Plot: Tempo vs Prestigio (Pareto Frontier concept)
    plt.figure(figsize=(12, 8))
    sns.scatterplot(
        data=df, 
        x="Tempo (s)", 
        y="Prestigio", 
        hue="Ingredienti", 
        size="Ingredienti", 
        sizes=(50, 200),
        palette="viridis",
        alpha=0.8
    )
    plt.title("Efficienza Ricette: Fronte di Pareto (Tempo vs Prestigio)", fontsize=16)
    plt.xlabel("Tempo di Preparazione (Secondi) - Cerca Valori Bassi", fontsize=12)
    plt.ylabel("Punteggio Prestigio - Cerca Valori Alti", fontsize=12)
    
    # Evidenzia i punti migliori (Alta Resa, Basso Tempo) in alto a sinistra
    plt.axvline(x=df["Tempo (s)"].median(), color='red', linestyle='--', alpha=0.5, label='Tempo Mediano')
    plt.axhline(y=df["Prestigio"].median(), color='blue', linestyle='--', alpha=0.5, label='Prestigio Mediano')
    
    plt.legend()
    plt.grid(True, linestyle=':', alpha=0.6)
    plt.tight_layout()
    
    out_path_pareto = os.path.join("visualizations", "pareto_sfida.png")
    plt.savefig(out_path_pareto, dpi=300)
    plt.close()
    print(f"✅ Grafico Efficienza salvato in {out_path_pareto}")
    
    # 3. Grafico Frequenza Ingredienti
    from collections import Counter
    ingredient_counter = Counter()
    for r in recipes:
        for ing in r.get("ingredients", {}).keys():
            ingredient_counter[ing] += 1
            
    top_ingredients = dict(ingredient_counter.most_common(15))
    df_ing = pd.DataFrame(list(top_ingredients.items()), columns=["Ingrediente", "Frequenza Nelle Ricette"])
    
    plt.figure(figsize=(12, 8))
    sns.barplot(data=df_ing, x="Frequenza Nelle Ricette", y="Ingrediente", palette="magma")
    plt.title("Top 15 Ingredienti Più Richiesti nel MultiVerso", fontsize=16)
    plt.xlabel("Numero di Ricette che lo richiedono")
    plt.ylabel("")
    plt.tight_layout()
    
    out_path_ing = os.path.join("visualizations", "ingredienti_frequenza.png")
    plt.savefig(out_path_ing, dpi=300)
    plt.close()
    print(f"✅ Grafico Ingredienti salvato in {out_path_ing}")
