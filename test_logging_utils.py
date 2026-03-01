import io
import json
import unittest
from contextlib import redirect_stdout

from game_client.logging_utils import clear_log_context, log, log_context, set_log_context


class TestLoggingUtils(unittest.TestCase):
    def tearDown(self) -> None:
        clear_log_context()

    def _capture_one(self, fn) -> dict:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            fn()
        line = buffer.getvalue().strip()
        self.assertTrue(line)
        return json.loads(line)

    def test_log_emits_required_fields(self) -> None:
        set_log_context(run_id="main", turn_id=4, phase="closed_bid", correlation_id="corr-1")

        payload = self._capture_one(
            lambda: log(
                "BID",
                "closed_bid submitted",
                event="bid.submitted",
                bids_count=12,
            )
        )

        self.assertEqual(payload["event"], "bid.submitted")
        self.assertEqual(payload["legacy_tag"], "BID")
        self.assertEqual(payload["run_id"], "main")
        self.assertEqual(payload["turn_id"], 4)
        self.assertEqual(payload["phase"], "closed_bid")
        self.assertEqual(payload["correlation_id"], "corr-1")
        self.assertEqual(payload["bids_count"], 12)
        self.assertIn("ts", payload)
        self.assertIn("component", payload)
        self.assertIn("level", payload)

    def test_log_context_is_scoped(self) -> None:
        clear_log_context()

        with io.StringIO() as buffer:
            with redirect_stdout(buffer):
                with log_context(correlation_id="scope-123"):
                    log("SERVE", "serve attempt", event="serve.submitted")
                log("SERVE", "after scope", event="serve.lifecycle")
            lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

        self.assertEqual(lines[0]["correlation_id"], "scope-123")
        self.assertIsNone(lines[1]["correlation_id"])


if __name__ == "__main__":
    unittest.main()
