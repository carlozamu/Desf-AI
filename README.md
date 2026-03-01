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

## Trend Analyst Integration

La strategia runtime usa in priorità i valori del Trend Analyst:

- `closed_bid`: se il report contiene il blocco JSON `bids`, il client invia quei `amount/quantity` (niente prezzi hardcoded).
- `waiting`: se il report contiene il blocco JSON `menu`, il client salva quei `price` nel menu (niente tier fissi).

Fallback:

- Se il JSON del trend è mancante/non valido, il sistema usa il fallback locale.
- Il fallback pricing locale non usa più i tier hardcoded `2/50/250`: ora è dinamico (`ingredient_cost * 3`, clampato).

## Structured Logging (JSON)

Il runtime emette log strutturati JSON su `stdout` (una riga per evento).
Campi principali:

- `ts`: timestamp UTC ISO-8601 con millisecondi
- `level`: `DEBUG | INFO | WARN | ERROR`
- `event`: nome evento stabile (es. `phase.changed`, `bid.submitted`, `serve.submitted`)
- `message`: descrizione breve
- `component`: modulo sorgente
- `run_id`: identificativo run corrente (se disponibile)
- `turn_id`: turno corrente (se disponibile)
- `phase`: fase corrente (`speaking`, `closed_bid`, `waiting`, `serving`, `stopped`)
- `correlation_id`: ID per correlare eventi dello stesso flusso SSE
- `legacy_tag`: alias storico compatibile (`BID`, `SERVE`, `MCP`, ...)

Verbosity runtime:

- `LOG_PROFILE=compact` (default): log sintetico, mantiene eventi chiave + warning/error.
- `LOG_PROFILE=verbose`: log completo (tutti gli eventi).
- `LOG_LEVEL` opzionale (`DEBUG|INFO|WARN|ERROR`) per soglia livello.

Esempio:

```json
{"ts":"2026-03-01T10:22:11.123+00:00","level":"INFO","event":"bid.submitted","message":"closed_bid submitted","component":"game_client.phases","run_id":"main","turn_id":12,"phase":"closed_bid","correlation_id":"...","legacy_tag":"BID","bids_count":25}
```

### Legacy Tag Mapping

- `INIT` -> `app.init`
- `PHASE` -> `phase.changed`
- `SSE` -> `sse.lifecycle`
- `HTTP` -> `http.request`
- `MCP` -> `mcp.call`
- `BID` -> `bid.lifecycle`
- `MENU` -> `menu.lifecycle`
- `MENU-PRICE` -> `menu.price.calculated`
- `SERVE` -> `serve.lifecycle`
- `TRACK` -> `tracker.lifecycle`
- `SELLER` -> `seller.lifecycle`
- `STRATEGY` -> `strategy.lifecycle`
- `MSG` -> `message.received`
- `DM` -> `dm.received`
- `ERROR` -> `error`
- `WARN` -> `warning`
