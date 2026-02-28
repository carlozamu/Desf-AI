import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from collections import Counter
import os
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

if not firebase_admin._apps:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
    else:
        firebase_admin.initialize_app()

db = firestore.client()

# Create output dir for images
os.makedirs('visualizations', exist_ok=True)

def plot_ingredient_distribution():
    print("Generating Ingredient Distribution Plot...")
    recipes = [doc.to_dict() for doc in db.collection('recipes').stream()]
    ingredient_counter = Counter()
    
    for r in recipes:
        ingredients = r.get("ingredients", {})
        for ing, qty in ingredients.items():
            ingredient_counter[ing] += qty
            
    # Get top 20
    top_ingredients = ingredient_counter.most_common(20)
    names = [i[0] for i in top_ingredients]
    counts = [i[1] for i in top_ingredients]
    
    plt.figure(figsize=(12, 8))
    sns.barplot(x=counts, y=names, palette="viridis")
    plt.title('Top 20 Ingredienti Più Usati nelle Ricette (Frequenza Assoluta)', fontsize=14)
    plt.xlabel('Numero di Ricette in cui appaiono', fontsize=12)
    plt.ylabel('Ingrediente', fontsize=12)
    plt.tight_layout()
    plt.savefig('visualizations/ingredient_distribution.png')
    plt.close()
    return 'visualizations/ingredient_distribution.png'

def plot_efficiency_scatter():
    print("Generating Recipe Efficiency Scatter Plot...")
    recipes = [doc.to_dict() for doc in db.collection('recipes').stream()]
    
    data = []
    
    for r in recipes:
        prep_time_ms = r.get("preparationTimeMs", 1)
        prep_time_sec = prep_time_ms / 1000.0
        prestige = r.get("prestige", 0)
        
        if prep_time_sec > 0:
            efficiency = prestige / prep_time_sec
            num_ingredients = len(r.get("ingredients", {}))
            data.append({
                "Name": r.get("name"),
                "Prestige": prestige,
                "Time (s)": prep_time_sec,
                "Efficiency (Ratio)": efficiency,
                "Num Ingredients": num_ingredients
            })
            
    df = pd.DataFrame(data)
    
    plt.figure(figsize=(12, 8))
    # Scatter plot: Time vs Prestige, size based on Efficiency, color based on number of ingredients
    scatter = sns.scatterplot(
        data=df, 
        x="Time (s)", 
        y="Prestige", 
        size="Efficiency (Ratio)", 
        sizes=(20, 200),
        hue="Num Ingredients",
        palette="coolwarm",
        alpha=0.7
    )
    plt.title('Ricette: Tempo di Preparazione vs Prestigio', fontsize=14)
    
    # Annotate top 5 most efficient recipes
    top_5 = df.nlargest(5, 'Efficiency (Ratio)')
    for idx, row in top_5.iterrows():
        plt.annotate(row['Name'], (row['Time (s)'], row['Prestige']), 
                     xytext=(5, 5), textcoords='offset points', fontsize=8, alpha=0.8)

    plt.tight_layout()
    plt.savefig('visualizations/recipe_efficiency.png')
    plt.close()
    return 'visualizations/recipe_efficiency.png'

def plot_market_overview():
     print("Generating Market Overview...")
     restaurants = [doc.to_dict() for doc in db.collection('restaurants').stream()]
     
     if not restaurants:
         print("Nessun dato sui ristoranti.")
         return None
         
     data = []
     for r in restaurants:
         data.append({
             "Team": r.get("name", "Unknown"),
             "Balance": r.get("balance", 0),
             "Reputation": r.get("reputation", 0)
         })
         
     df = pd.DataFrame(data)
     
     fig, axes = plt.subplots(1, 2, figsize=(15, 6))
     
     sns.histplot(df['Balance'], bins=10, ax=axes[0], color='skyblue', kde=True)
     axes[0].set_title('Distribuzione Saldo (Balance) iniziale dei Team')
     
     sns.histplot(df['Reputation'], bins=10, ax=axes[1], color='salmon', kde=True)
     axes[1].set_title('Distribuzione Reputazione (Reputation) iniziale dei Team')
     
     plt.tight_layout()
     plt.savefig('visualizations/market_overview.png')
     plt.close()
     return 'visualizations/market_overview.png'
     
if __name__ == "__main__":
    recipes = [doc.to_dict() for doc in db.collection('recipes').stream()]
    if len(recipes) == 0:
        print("Il database delle ricette è vuoto. Esegui data_manager.py prima!")
    else:
        plot_ingredient_distribution()
        plot_efficiency_scatter()
        plot_market_overview()
        print("Visualizations saved in 'visualizations/' directory.")
