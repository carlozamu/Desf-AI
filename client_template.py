# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like"
# ]
# ///

from __future__ import annotations

import asyncio

from game_client.app import main
from game_client.logging_utils import log


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "client stopped")
