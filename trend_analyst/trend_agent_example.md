# Integrare il Trend Analyst nella Pipeline Principale

Questa guida spiega come integrare l'agente analista (`run_trend_analysis` o `a_run_trend_analysis`) e consumare l'output JSON che genera per le fasi successive del ciclo di gioco (pianificazione menu, offerta ingredienti).

## 1. Importazione e Chiamata (Esempio)

Nel tuo loop principale (es. in `run_pipeline.py` / `main.py`) avvia l'analisi passando l'ID dell'ultimo turno noto. Puoi anche passare i turni precedenti per un'analisi più profonda sul bid history.

```python
import asyncio
from trend_analyst.trend_analyst_agent import a_run_trend_analysis, extract_json_plan

async def run_trend_phase(current_turn_id: int):
    print("🧠 Avvio fase Analisi Trend...")
    
    # Avvia l'agente (fornendo anche turni precedenti per contesto e storico offerte)
    report_md = await a_run_trend_analysis(current_turn_id, previous_turns=[current_turn_id-1])
    
    # (Opzionale) Salva il report markdown su disco per poterlo leggere (debug o dashboard)
    with open(f"trend_report_t{current_turn_id}.md", "w") as f:
        f.write(report_md)
        
    # Estrae la parte "azionabile" per aggiornare il gioco
    plan = extract_json_plan(report_md)
    if plan:
        print(f"💰 Trovate {len(plan.get('bids', {}))} offerte d'asta (bids).")
        print(f"🍽️ Menu suggerito: {len(plan.get('menu', []))} item.")
        
        # ... qui passi l'oggetto plan ad Api o ad altri agent (es: esegui_bids(plan["bids"]))
        
    return plan
```

## 2. Utilizzo pratico dell'Output JSON

La funzione `extract_json_plan(report_md)` restituisce un dizionario Python con questa chiave fissa:
```json
{
  "menu": [
    { "name": "Pizza al Tartufo e Oro", "price": 450 },
    { "name": "Schiacciatina Essenziale", "price": 10 }
  ],
  "bids": {
    "Tartufo Nero": { "amount": 60, "quantity": 2 },
    "Farina Semplice": { "amount": 2, "quantity": 10 }
  }
}
```

* **Passaggio Menu:** Puoi prendere `plan["menu"]` e darlo direttamente in pasto all'API del gioco (`api.save_menu(plan["menu"])` se supporta questa forma), oppure formattarlo per un agente `Chef`. Contiene sia gli elementi "Premium" (👑) sia quelli "Economici" (💸).
* **Passaggio Bids:** Puoi iterare il dizionario `plan["bids"]` per costruire l'elenco da inviare all'asta al momento della fase `closed_bid`. L'agente sa di **non sfondare** l'attuale `balance` totale quando costruisce questo piano.

---

## 3. Esempio di Output Generato dall'Agente (.md)

Di seguito un esempio di **report grezzo** (markdown) che produrrà l'agente alla fine della sua elaborazione:

```markdown
# Strategic Intelligence Report - Turn 35

## 💰 CUSTOMER WILLINGNESS TO PAY (WTP)
- 🚀 **Esploratore Galattico**: Highly active. Ordering heavily on menus priced 10-25 cr.
- 💰 **Astrobarone**: Moderate activity, buying 200+ cr dishes but often underserved due to low ingredient stock tracking in competitors.
- 🔭 **Saggio del Cosmo**: Low appearances, WTP estimated at ~500 cr.
- 👨‍👩‍👧‍👦 **Famiglia Orbitale**: Searching for 40-70 cr range.

## 📈 CURRENT MARKET TREND
**Both** (Mixed)
Reasoning: We are seeing a heavy split. R15 and R22 are pushing extreme volume at 12cr, resulting in many Esploratore orders, while R8 just made a massive 1500cr spike serving two Saggio del Cosmo dishes. We should capture both ends.

## 🥗 TOP 20 INGREDIENTS
1. **Tartufo di Andromeda**: 6 bidders, Avg 85 cr (High Scarcity)
2. **Pomodoro Rosso**: 12 bidders, Avg 5 cr (High Demand, Moderate Scarcity)
...

## 🏅 TOP COMPETITORS DEEP DIVE
1. **#1 [R15] Team Alpha** (balance: 4500 cr)
   - Strategy: Extreme volume. Menu ranges 10-25 cr.
   - Ingredient Focus: Farina, Pomodoro.
   - ⚠️ BLIND SPOTS: Completely ignoring the Astrobarone. No dish above 30 cr.
...

## 👑 BEST PRESTIGIOUS RECIPES (Max 2)
1. **Pizza al Tartufo e Oro**
   - Price: 450 cr
   - Target: Astrobarone / Saggio
   - Rationale: Team Alpha and others are fighting for cheap tomatoes. "Oro" and "Tartufo" have far fewer bidders overall. We can corner this market.

## 💸 BEST ECONOMICAL RECIPES (Beat the Competition)
1. **Schiacciatina Essenziale**
   - Price: 10 cr
   - Target: Esploratore Galattico
   - Rationale: Team Alpha sells theirs at 12cr. If we list at 10cr using basic flour and water, we undercut their main revenue stream.

## 🔮 NEXT RUN HINTS & CLIENTELE INTEREST
1. **Untapped Resource: Alghe Spaziali**: Looking at the 200 recipe pool, 14 recipes use "Alghe Spaziali" but NO ONE is bidding on them. Next turn, we should pivot into Algae-based sushi to enjoy zero bid competition.
2. **Raise Prices on Water**: Clientele is buying anything with hydration. We can safely push water-based cheap dishes from 10 cr to 14 cr next run without losing Esploratori.

## ⚡ EXTRA INTELLIGENCE
- Market Arbitrage: R22 is selling 5x "Mozzarella" at 3cr total. BUY NOW. We can resell or use it for our Premium pizza.
- Bids for Next Auction: We need Tartufo (x2) and Farina (x10).

```json
{
  "menu": [
    { "name": "Pizza al Tartufo e Oro", "price": 450 },
    { "name": "Schiacciatina Essenziale", "price": 10 }
  ],
  "bids": {
    "Tartufo di Andromeda": { "amount": 90, "quantity": 2 },
    "Farina Semplice": { "amount": 6, "quantity": 10 },
    "Acqua": { "amount": 2, "quantity": 5 }
  }
}
```
```
