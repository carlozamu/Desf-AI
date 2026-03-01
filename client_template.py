# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like"
# ]
# ///

import asyncio
import os
from dotenv import load_dotenv

# Optional fallback logic, but primary loading is handled by config
load_dotenv()

async def main() -> None:
    # Delegate execution exclusively to the modular application architect.
    # Legacy fallbacks have been removed.
    
    try:
        from game_client.app import main as app_main
        await app_main()
    except Exception as exc:
        print(f"ERROR: Modular runtime failed during bootstrap: {exc}")
        print(f"Exception Type: {type(exc).__name__}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nClient successfully stopped by user (KeyboardInterrupt).")
