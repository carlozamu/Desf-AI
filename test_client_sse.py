import unittest

from game_client.sse import handle_line


class TestClientSSE(unittest.IsolatedAsyncioTestCase):
    async def test_connected_line_is_ignored_for_dispatch(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_dispatch(event_type: str, event_data: dict) -> None:
            calls.append((event_type, event_data))

        await handle_line(b"data: connected\n", dispatch_event_fn=fake_dispatch)
        self.assertEqual(calls, [])

    async def test_json_event_dispatches_dict_data(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_dispatch(event_type: str, event_data: dict) -> None:
            calls.append((event_type, event_data))

        await handle_line(
            b'data: {"type": "client_spawned", "data": {"clientName": "Ana"}}\n',
            dispatch_event_fn=fake_dispatch,
        )

        self.assertEqual(calls, [("client_spawned", {"clientName": "Ana"})])

    async def test_json_event_dispatches_non_dict_as_value(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_dispatch(event_type: str, event_data: dict) -> None:
            calls.append((event_type, event_data))

        await handle_line(
            b'data: {"type": "message", "data": "hello"}\n',
            dispatch_event_fn=fake_dispatch,
        )

        self.assertEqual(calls, [("message", {"value": "hello"})])


if __name__ == "__main__":
    unittest.main()
