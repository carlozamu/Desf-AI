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