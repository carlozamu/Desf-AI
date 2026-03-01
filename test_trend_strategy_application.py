import unittest
from typing import Any

from game_client.config import ClientConfig
from game_client.phases import PhaseRunner
from game_client.state import GameState


class _FakeMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        return {"ok": True}


class _FakeAPI:
    def __init__(self, recipes: list[dict[str, Any]] | None = None) -> None:
        self._recipes = recipes or []
        self.get_recipes_calls = 0

    async def get_recipes(self) -> list[dict[str, Any]]:
        self.get_recipes_calls += 1
        return self._recipes


def _build_config() -> ClientConfig:
    return ClientConfig(
        team_id="1",
        team_api_key="key",
        base_url="https://example.test",
        run_id="main",
        pricing_mode="",
        pricing_test_turn=-1,
        sse_retry_delay=3,
        sse_conflict_retry_delay=6,
    )


def _build_runner(
    *,
    state: GameState,
    api_client: _FakeAPI,
    mcp_gateway: _FakeMCP,
    run_trend_analysis_fn,
) -> PhaseRunner:
    async def _run_seller(_: int) -> None:
        return None

    async def _collect_turn_meals(_: int) -> dict[str, Any]:
        return {}

    def _summarize(_: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def _fetch_bid_history(**_: Any) -> Any:
        return None

    def _compute_menu_price(_: int, *, is_cost_1x_test: bool, dish_index: int) -> tuple[int, str]:
        _ = (is_cost_1x_test, dish_index)
        return 99, "test"

    def _init_firebase() -> Any:
        return None

    async def _collect_all_menus() -> dict[str, Any]:
        return {}

    def _save_meals(*_: Any) -> bool:
        return True

    def _save_menus(*_: Any) -> bool:
        return True

    return PhaseRunner(
        config=_build_config(),
        state=state,
        api_client=api_client,
        mcp_gateway=mcp_gateway,
        run_seller_agent_fn=_run_seller,
        collect_turn_meals_fn=_collect_turn_meals,
        summarize_unserved_requests_fn=_summarize,
        fetch_and_save_bid_history_fn=_fetch_bid_history,
        compute_menu_price_fn=_compute_menu_price,
        init_firebase_fn=_init_firebase,
        collect_all_menus_fn=_collect_all_menus,
        save_meals_to_firebase_fn=_save_meals,
        save_menus_to_firebase_fn=_save_menus,
        run_trend_analysis_fn=run_trend_analysis_fn,
    )


class TestTrendStrategyApplication(unittest.IsolatedAsyncioTestCase):
    async def test_closed_bid_uses_trend_bids_when_available(self) -> None:
        state = GameState(current_turn_id=5)
        mcp = _FakeMCP()
        api = _FakeAPI(
            recipes=[
                {"name": "FallbackRecipe", "ingredients": {"tomato": 1}},
            ]
        )

        async def _run_trend(_: int, __: list[int] | None = None) -> str:
            return """
```json
{
  "menu": [{"name": "Margherita", "price": 120}],
  "bids": {"Tomato": {"amount": 27, "quantity": 3}}
}
```
"""

        runner = _build_runner(
            state=state,
            api_client=api,
            mcp_gateway=mcp,
            run_trend_analysis_fn=_run_trend,
        )

        await runner.closed_bid_phase_started({"turn_id": 5})

        self.assertEqual(api.get_recipes_calls, 0)
        self.assertEqual(
            mcp.calls[0],
            ("closed_bid", {"bids": [{"ingredient": "Tomato", "bid": 27, "quantity": 3}]}),
        )
        self.assertEqual(state.ingredient_bids, {"Tomato": 27})

    async def test_waiting_uses_trend_menu_prices_when_available(self) -> None:
        state = GameState(current_turn_id=6)
        mcp = _FakeMCP()
        api = _FakeAPI()

        async def _run_trend(_: int, __: list[int] | None = None) -> str:
            return """
```json
{
  "menu": [
    {"name": "Margherita", "price": 120},
    {"name": "Diavola", "price": 180}
  ],
  "bids": {"Tomato": {"amount": 27, "quantity": 3}}
}
```
"""

        runner = _build_runner(
            state=state,
            api_client=api,
            mcp_gateway=mcp,
            run_trend_analysis_fn=_run_trend,
        )

        await runner.waiting_phase_started()

        self.assertEqual(
            mcp.calls[-1],
            (
                "save_menu",
                {
                    "items": [
                        {"name": "Margherita", "price": 120},
                        {"name": "Diavola", "price": 180},
                    ]
                },
            ),
        )
        self.assertEqual(
            state.active_menu,
            [
                {"name": "Margherita", "price": 120},
                {"name": "Diavola", "price": 180},
            ],
        )


if __name__ == "__main__":
    unittest.main()
