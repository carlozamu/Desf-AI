# Desf-AI

## Setup Database (Firebase Firestore)

Questo progetto utilizza Firebase Firestore per il salvataggio dei dati (Ricette e Ristoranti). Per configurare correttamente la connessione al database, segui questi passaggi:

1. **Credenziali Firebase**
   - Vai nella Console di Firebase -> Impostazioni progetto -> Account di servizio.
   - Clicca su "Genera nuova chiave privata" per scaricare il file JSON con le credenziali.
   - Rinomina il file in `firebase_credentials.json` e posizionalo nella cartella principale del progetto **(è già ignorato in `.gitignore`)**.

2. **Configurazione `.env`**
   - Assicurati di avere un file `.env` nella root del progetto.
   - Aggiungi la seguente variabile al file `.env`:
     ```env
     GOOGLE_APPLICATION_CREDENTIALS=firebase_credentials.json
     SERVER_API_KEY=tua_chiave_server
     ```

3. **Installazione ed Esecuzione**
   - Le dipendenze (incluso `firebase-admin` e `python-dotenv`) vengono installate tramite:
     ```bash
     pip install -r requirements_server.txt
     ```
   - Lo script caricherà automaticamente le credenziali e le variabili di ambiente. Puoi lanciare l'aggiornamento dei dati tramite:
     ```bash
     python data_manager.py
     ```

## Client Architecture (Refactor)

L'entrypoint resta `client_template.py`, ma la logica è stata separata nel package `game_client/`:

- `game_client/config.py`: parsing variabili ambiente e configurazione runtime.
- `game_client/state.py`: stato di partita condiviso.
- `game_client/strategy.py`: funzioni pure di strategia (bidding/menu/serving helpers).
- `game_client/http_api.py`: accesso endpoint REST (`/recipes`, `/meals`, ...).
- `game_client/mcp_gateway.py`: wrapper MCP (`call_tool`).
- `game_client/phases.py`: handlers delle fasi (`closed_bid`, `waiting`, `serving`, `stopped`).
- `game_client/events.py`: routing eventi di gioco.
- `game_client/sse.py`: parsing SSE e lifecycle connessione/retry.
- `game_client/app.py`: wiring finale delle dipendenze.

Questo mantiene il comportamento del client invariato, riducendo l'accoppiamento rispetto al vecchio file monolitico.
