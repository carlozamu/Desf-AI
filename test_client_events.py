import unittest

from game_client.config import ClientConfig
from game_client.events import EventRouter
from game_client.state import GameState


class _FakeMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name: str, arguments: dict):
        self.calls.append((tool_name, arguments))
        return {"ok": True}


class _FakeAPI:
    async def get_my_meals(self, turn_id: int, *, restaurant_id: str):
        _ = (turn_id, restaurant_id)
        return []


class _FakePhases:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def speaking_phase_started(self) -> None:
        self.called.append("speaking")

    async def closed_bid_phase_started(self, data=None) -> None:
        _ = data
        self.called.append("closed_bid")

    async def waiting_phase_started(self) -> None:
        self.called.append("waiting")

    async def serving_phase_started(self) -> None:
        self.called.append("serving")

    async def end_turn(self) -> None:
        self.called.append("stopped")


class TestEventRouter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = ClientConfig(
            team_id="1",
            team_api_key="key",
            base_url="https://example.test",
            run_id="main",
            pricing_mode="",
            pricing_test_turn=-1,
            sse_retry_delay=3,
            sse_conflict_retry_delay=6,
        )
        self.state = GameState()
        self.mcp = _FakeMCP()
        self.router = EventRouter(
            config=self.config,
            state=self.state,
            api_client=_FakeAPI(),
            mcp_gateway=self.mcp,
            phase_runner=_FakePhases(),
        )

    async def test_game_started_resets_state_and_opens_restaurant(self) -> None:
        self.state.active_menu = [{"name": "Old"}]
        await self.router.dispatch_event("game_started", {"turn_id": 7})

        self.assertEqual(self.state.current_turn_id, 7)
        self.assertEqual(self.state.active_menu, [])
        self.assertEqual(self.mcp.calls[0], ("update_restaurant_is_open", {"is_open": True}))

    async def test_game_phase_changed_routes_to_phase_handler(self) -> None:
        phases = _FakePhases()
        router = EventRouter(
            config=self.config,
            state=self.state,
            api_client=_FakeAPI(),
            mcp_gateway=self.mcp,
            phase_runner=phases,
        )

        await router.dispatch_event("game_phase_changed", {"phase": "waiting", "turn_id": 3})
        self.assertEqual(self.state.current_turn_id, 3)
        self.assertEqual(phases.called, ["waiting"])

    async def test_client_spawned_serves_when_dish_ready(self) -> None:
        self.state.active_menu = [{"name": "Margherita", "price": 10}]
        self.state.dishes_ready["Margherita"] = True

        await self.router.dispatch_event(
            "client_spawned",
            {
                "clientName": "Ana",
                "orderText": "I want to eat Margherita",
                "clientId": "c-1",
            },
        )

        self.assertEqual(
            self.mcp.calls,
            [
                ("serve_dish", {"dish_name": "Margherita", "client_id": "c-1"}),
                ("prepare_dish", {"dish_name": "Margherita"}),
            ],
        )
        self.assertFalse(self.state.dishes_ready["Margherita"])


if __name__ == "__main__":
    unittest.main()
