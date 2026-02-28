import asyncio
from menu_optimizer.run_pipeline import main as run_menu_optimization

if __name__ == "__main__":
    print("🤖 Avvio del setup iniziale Desf-AI (Menu Optimizer) 🤖")
    
    # Esegue la pipeline di estrazione dati, analisi e ottimizzazione del menu
    asyncio.run(run_menu_optimization())
