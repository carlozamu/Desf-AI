import os
import aiohttp
import asyncio
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

def init_firebase(cred_path: str):
    """
    Initializes the Firebase app and returns the Firestore client.
    Ensures it only initializes once per python process.
    """
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        print(f"⚠️ Firebase initialization error: {e}. Check the JSON file path.")
        return None


async def fetch_and_save_bid_history(turn_id: int, api_key: str, base_url: str, cred_path: str):
    """
    Fetches concluded bids for a specific turn and saves them to Firebase Firestore.
    The DB client is initialized internally.
    """
    if int(turn_id) <= 0:
        print(f"⚠️ Skipping bid history fetch: invalid turn_id={turn_id}")
        return None

    resolved_cred_path = cred_path or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")

    # 1. Initialize DB inside the function
    db = init_firebase(resolved_cred_path)
    if not db:
        print("❌ No Firestore client available. Aborting.")
        return None

    # 2. The endpoint requires the turn_id query
    url = f"{base_url}/bid_history?turn_id={turn_id}"
    
    # 3. All team calls require the x-api-key header
    headers = {"x-api-key": api_key}
    
    print(f"📡 Fetching bid history for turn {turn_id}...")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                new_bids = await response.json()
                print(f"✅ Obtained {len(new_bids)} concluded bids for turn {turn_id}.")
                
                # 4. Save to Firebase Firestore in a separate thread to avoid blocking the async event loop
                try:
                    await asyncio.to_thread(
                        db.collection("bid_history").document(f"turn_{turn_id}").set,
                        {
                            "turn_id": turn_id,
                            "bids": new_bids
                        }
                    )
                    print(f"💾 Turn {turn_id} history successfully saved to Firebase!")
                    return new_bids
                except Exception as e:
                    print(f"❌ Error saving to Firebase: {e}")
                    return None
                    
            elif response.status == 401:
                print("❌ Unauthorized (401): Check your SERVER_API_KEY.")
                return None
            elif response.status == 400:
                error_text = await response.text()
                print(f"⚠️ Bid history request ignored (HTTP 400): {error_text}")
                return None
            else:
                error_text = await response.text()
                print(f"❌ HTTP Error {response.status} - {error_text}")
                return None

# --- Standalone test execution ---
if __name__ == "__main__":
    # 1. Resolve paths to the parent directory
    parent_dir = os.path.join(os.path.dirname(__file__), '..')
    env_path = os.path.join(parent_dir, '.env')
    firebase_cred_path = os.path.join(parent_dir, 'firebase_credentials.json')

    # 2. Load the .env file from the parent folder
    load_dotenv(dotenv_path=env_path)

    # 3. Read variables
    TEAM_API_KEY = os.getenv("SERVER_API_KEY")
    BASE_URL = os.getenv("HACKAPIZZA_BASE_URL", "https://hackapizza.datapizza.tech")

    if not TEAM_API_KEY:
        print("⚠️ WARNING: SERVER_API_KEY not found in environment variables.")
    else:
        # 4. Run the async function passing the path to the credentials, not the db_client
        test_turn_id = 1
        asyncio.run(fetch_and_save_bid_history(
            turn_id=test_turn_id,
            api_key=TEAM_API_KEY,
            base_url=BASE_URL,
            cred_path=firebase_cred_path
        ))
