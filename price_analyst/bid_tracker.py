import os
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp
else:
    try:
        import aiohttp  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        aiohttp = None  # type: ignore[assignment]

try:
    import firebase_admin  # type: ignore[import-not-found]
    from firebase_admin import credentials, firestore  # type: ignore[import-not-found]
except ModuleNotFoundError:
    firebase_admin = None  # type: ignore[assignment]
    credentials = None  # type: ignore[assignment]
    firestore = None  # type: ignore[assignment]
from dotenv import load_dotenv
from game_client.logging_utils import log, log_context, set_log_context

def init_firebase(cred_path: str):
    """
    Initializes the Firebase app and returns the Firestore client.
    Ensures it only initializes once per python process.
    """
    try:
        if firebase_admin is None or credentials is None or firestore is None:
            log(
                "TRACK",
                "firebase_admin dependency missing",
                event="firebase.init.missing_dependency",
                level="ERROR",
            )
            return None

        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        log(
            "TRACK",
            f"Firebase initialization error: {e}. Check the JSON file path.",
            event="firebase.init.failed",
            level="ERROR",
            error_type=type(e).__name__,
            error_message=str(e),
        )
        return None


async def fetch_and_save_bid_history(turn_id: int, api_key: str, base_url: str, cred_path: str):
    """
    Fetches concluded bids for a specific turn and saves them to Firebase Firestore.
    The DB client is initialized internally.
    """
    try:
        parsed_turn_id = int(turn_id)
    except (TypeError, ValueError):
        parsed_turn_id = -1

    set_log_context(turn_id=(parsed_turn_id if parsed_turn_id > 0 else None), phase="waiting")
    with log_context(component="price_analyst.bid_tracker"):
        if aiohttp is None:
            log(
                "BID",
                "aiohttp dependency missing",
                event="bid_history.fetch.failed.missing_dependency",
                level="ERROR",
            )
            return None

        if parsed_turn_id <= 0:
            log(
                "BID",
                f"Skipping bid history fetch: invalid turn_id={turn_id}",
                event="bid_history.fetch.skipped",
                level="WARN",
            )
            return None

        resolved_cred_path = cred_path or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json")

        # 1. Initialize DB inside the function
        db = init_firebase(resolved_cred_path)
        if not db:
            log(
                "TRACK",
                "No Firestore client available. Aborting.",
                event="bid_history.fetch.failed.no_firestore",
                level="ERROR",
            )
            return None

        # 2. The endpoint requires the turn_id query
        url = f"{base_url}/bid_history?turn_id={parsed_turn_id}"

        # 3. All team calls require the x-api-key header
        headers = {"x-api-key": api_key}

        log("BID", f"Fetching bid history for turn {parsed_turn_id}", event="bid_history.fetch.started")

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    new_bids = await response.json()
                    log(
                        "BID",
                        f"Obtained {len(new_bids)} concluded bids for turn {parsed_turn_id}",
                        event="bid_history.fetch.completed",
                        bids_count=len(new_bids),
                    )

                    # 4. Save to Firebase Firestore in a separate thread to avoid blocking the async event loop
                    try:
                        await asyncio.to_thread(
                            db.collection("bid_history").document(f"turn_{parsed_turn_id}").set,
                            {
                                "turn_id": parsed_turn_id,
                                "bids": new_bids
                            }
                        )
                        log(
                            "TRACK",
                            f"Turn {parsed_turn_id} history successfully saved to Firebase",
                            event="bid_history.save.completed",
                        )
                        return new_bids
                    except Exception as e:
                        log(
                            "TRACK",
                            f"Error saving bid history to Firebase: {e}",
                            event="bid_history.save.failed",
                            level="ERROR",
                            error_type=type(e).__name__,
                            error_message=str(e),
                        )
                        return None

                elif response.status == 401:
                    log(
                        "BID",
                        "Unauthorized (401): Check your SERVER_API_KEY.",
                        event="bid_history.fetch.unauthorized",
                        level="ERROR",
                        status_code=401,
                    )
                    return None
                elif response.status == 400:
                    error_text = await response.text()
                    log(
                        "BID",
                        f"Bid history request ignored (HTTP 400): {error_text}",
                        event="bid_history.fetch.bad_request",
                        level="WARN",
                        status_code=400,
                    )
                    return None
                else:
                    error_text = await response.text()
                    log(
                        "BID",
                        f"HTTP Error {response.status} - {error_text}",
                        event="bid_history.fetch.failed",
                        level="ERROR",
                        status_code=response.status,
                    )
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
        log(
            "WARN",
            "SERVER_API_KEY not found in environment variables.",
            event="bid_history.standalone.missing_api_key",
            level="WARN",
        )
    else:
        # 4. Run the async function passing the path to the credentials, not the db_client
        test_turn_id = 1
        asyncio.run(fetch_and_save_bid_history(
            turn_id=test_turn_id,
            api_key=TEAM_API_KEY,
            base_url=BASE_URL,
            cred_path=firebase_cred_path
        ))
