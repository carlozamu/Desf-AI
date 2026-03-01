from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Force .env values to override inherited shell variables so runtime tuning
# (budget caps / bid bounds) is deterministic between runs.
load_dotenv(override=True)


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
    min_ingredient_bid: int = 2
    max_ingredient_bid: int = 12
    portions_per_recipe: int = 2
    max_target_recipes: int = 12
    turn_spend_cap: int = 5000
    initial_credits: int = 1000
    credits_per_turn_income: int = 1000
    auction_spend_floor_ratio: float = 0.90
    auction_only_mode: bool = False
    max_menu_items: int = 220
    prewarm_menu_fraction: float = 0.40
    prewarm_concurrency: int = 4
    seller_timeout_seconds: int = 10
    max_active_clients_before_close: int = 5
    stockout_liquidation_enabled: bool = True
    stockout_sell_min_price: int = 1



def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
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

    min_ingredient_bid = max(1, _parse_int_env("MIN_INGREDIENT_BID", 2))
    max_ingredient_bid = max(min_ingredient_bid + 1, _parse_int_env("MAX_INGREDIENT_BID", 12))
    portions_per_recipe = max(1, min(6, _parse_int_env("PORTIONS_PER_RECIPE", 2)))
    prewarm_menu_fraction = _parse_float_env("PREWARM_MENU_FRACTION", 0.40)
    prewarm_menu_fraction = max(0.0, min(1.0, prewarm_menu_fraction))
    auction_spend_floor_ratio = _parse_float_env("AUCTION_SPEND_FLOOR_RATIO", 0.90)
    auction_spend_floor_ratio = max(0.0, min(1.0, auction_spend_floor_ratio))
    prewarm_concurrency = _parse_int_env(
        "PREWARM_CONCURRENCY",
        _parse_int_env("MAX_PARALLEL_PREPARATION_PER_DISH", 4),
    )
    prewarm_concurrency = max(1, min(32, prewarm_concurrency))
    seller_timeout_seconds = max(2, min(120, _parse_int_env("SELLER_TIMEOUT_SECONDS", 10)))
    max_active_clients_before_close = max(
        0,
        min(100, _parse_int_env("MAX_ACTIVE_CLIENTS_BEFORE_CLOSE", 5)),
    )
    stockout_liquidation_enabled = _parse_bool_env("ENABLE_STOCKOUT_LIQUIDATION", True)
    stockout_sell_min_price = max(1, min(999, _parse_int_env("STOCKOUT_SELL_MIN_PRICE", 1)))

    return ClientConfig(
        team_id=team_id,
        team_api_key=team_api_key,
        base_url=base_url,
        run_id=run_id,
        pricing_mode=os.getenv("PRICING_MODE", "").strip().lower(),
        pricing_test_turn=pricing_test_turn,
        sse_retry_delay=_parse_int_env("SSE_RETRY_DELAY", 3),
        sse_conflict_retry_delay=_parse_int_env("SSE_CONFLICT_RETRY_DELAY", 6),
        min_ingredient_bid=min_ingredient_bid,
        max_ingredient_bid=max_ingredient_bid,
        portions_per_recipe=portions_per_recipe,
        max_target_recipes=max(6, min(24, _parse_int_env("MAX_TARGET_RECIPES", 12))),
        turn_spend_cap=max(100, _parse_int_env("TURN_SPEND_CAP", 5000)),
        initial_credits=max(0, _parse_int_env("INITIAL_CREDITS", 1000)),
        credits_per_turn_income=max(0, _parse_int_env("CREDITS_PER_TURN_INCOME", 1000)),
        auction_spend_floor_ratio=auction_spend_floor_ratio,
        auction_only_mode=_parse_bool_env("AUCTION_ONLY_MODE", False),
        max_menu_items=max(10, min(300, _parse_int_env("MAX_MENU_ITEMS", 220))),
        prewarm_menu_fraction=prewarm_menu_fraction,
        prewarm_concurrency=prewarm_concurrency,
        seller_timeout_seconds=seller_timeout_seconds,
        max_active_clients_before_close=max_active_clients_before_close,
        stockout_liquidation_enabled=stockout_liquidation_enabled,
        stockout_sell_min_price=stockout_sell_min_price,
    )



def get_firebase_cred_path() -> str:
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "firebase_credentials.json").strip()
    return path or "firebase_credentials.json"
