from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ClientConfig:
    team_id: str
    team_api_key: str
    base_url: str
    run_id: str
    pricing_mode: str
    pricing_test_turn: int
    sse_retry_delay: int
    sse_conflict_retry_delay: int
    min_ingredient_bid: int = 20
    max_ingredient_bid: int = 30
    portions_per_recipe: int = 2
    max_target_recipes: int = 3



def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default



def load_config() -> ClientConfig:
    team_id = os.getenv("TEAM_ID", "").strip()
    team_api_key = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", "")).strip()
    base_url = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech").strip()
    run_id = os.getenv("RUN_ID", "main").strip() or "main"

    if not team_id or not team_api_key:
        raise SystemExit("Set TEAM_ID and SERVER_API_KEY (or TEAM_API_KEY) in .env")

    pricing_test_turn_raw = os.getenv("PRICING_TEST_TURN", "").strip()
    try:
        pricing_test_turn = int(pricing_test_turn_raw) if pricing_test_turn_raw else -1
    except ValueError:
        pricing_test_turn = -1

    return ClientConfig(
        team_id=team_id,
        team_api_key=team_api_key,
        base_url=base_url,
        run_id=run_id,
        pricing_mode=os.getenv("PRICING_MODE", "").strip().lower(),
        pricing_test_turn=pricing_test_turn,
        sse_retry_delay=_parse_int_env("SSE_RETRY_DELAY", 3),
        sse_conflict_retry_delay=_parse_int_env("SSE_CONFLICT_RETRY_DELAY", 6),
    )



def get_firebase_cred_path() -> str:
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json").strip()
    return path or "firebase_credentials.json"
