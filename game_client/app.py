from __future__ import annotations

from typing import Awaitable, Callable

from game_client.config import load_config
from game_client.events import EventRouter
from game_client.http_api import ApiClient
from game_client.logging_utils import log, set_log_context
from game_client.mcp_gateway import MCPGateway
from game_client.phases import PhaseRunner
from game_client.sse import listen_forever
from game_client.state import GameState



def _load_seller_agent() -> Callable[[int], Awaitable[None]]:
    try:
        from seller.agent import run_seller_agent

        return run_seller_agent
    except ImportError as exc:
        log(
            "WARN",
            "seller agent not available",
            event="seller.agent.unavailable",
            level="WARN",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

        async def _run_seller_agent(turn_id: int) -> None:
            _ = turn_id

        return _run_seller_agent


def _load_trend_analyst() -> Callable[[int, list[int] | None], Awaitable[str]]:
    try:
        from trend_analyst.trend_analyst_agent import a_run_trend_analysis

        return a_run_trend_analysis
    except ImportError as exc:
        log(
            "WARN",
            "trend analyst not available",
            event="trend_analyst.unavailable",
            level="WARN",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

        async def _run_trend_analysis(turn_id: int, previous_turns: list[int] | None = None) -> str:
            _ = (turn_id, previous_turns)
            return ""

        return _run_trend_analysis


async def main() -> None:
    # Lazy imports to keep module import side effects low and preserve runtime behavior.
    from menu_pricing import compute_menu_price
    from price_analyst.bid_tracker import fetch_and_save_bid_history
    from serving.meals_tracker import (
        collect_turn_meals,
        init_firebase,
        save_to_firebase as save_meals_to_firebase,
        summarize_unserved_requests,
    )
    from serving.menu_tracker import collect_all_menus, save_to_firebase as save_menus_to_firebase

    config = load_config()
    state = GameState()
    set_log_context(
        run_id=config.run_id,
        turn_id=state.current_turn_id,
        phase=state.current_phase,
    )

    api_client = ApiClient(base_url=config.base_url, team_api_key=config.team_api_key, team_id=config.team_id)
    mcp_gateway = MCPGateway(base_url=config.base_url, team_api_key=config.team_api_key)
    phase_runner = PhaseRunner(
        config=config,
        state=state,
        api_client=api_client,
        mcp_gateway=mcp_gateway,
        run_seller_agent_fn=_load_seller_agent(),
        collect_turn_meals_fn=collect_turn_meals,
        summarize_unserved_requests_fn=summarize_unserved_requests,
        fetch_and_save_bid_history_fn=fetch_and_save_bid_history,
        compute_menu_price_fn=compute_menu_price,
        init_firebase_fn=init_firebase,
        collect_all_menus_fn=collect_all_menus,
        save_meals_to_firebase_fn=save_meals_to_firebase,
        save_menus_to_firebase_fn=save_menus_to_firebase,
        run_trend_analysis_fn=_load_trend_analyst(),
    )
    event_router = EventRouter(
        config=config,
        state=state,
        api_client=api_client,
        mcp_gateway=mcp_gateway,
        phase_runner=phase_runner,
    )

    log(
        "INIT",
        f"team={config.team_id} base_url={config.base_url}",
        event="app.started",
        team_id=config.team_id,
        base_url=config.base_url,
    )
    await listen_forever(config=config, dispatch_event_fn=event_router.dispatch_event)
