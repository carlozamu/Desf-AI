import unittest

from game_client.trend_strategy import parse_trend_plan, plan_to_closed_bid_payload


class TestTrendStrategyParsing(unittest.TestCase):
    def test_parse_trend_plan_from_json_block(self) -> None:
        report = """
Some text.
```json
{
  "menu": [
    {"name": "Margherita", "price": 120},
    {"name": "Diavola", "price": 180}
  ],
  "bids": {
    "Tomato": {"amount": 27, "quantity": 3},
    "Cheese": {"amount": 41, "quantity": 2}
  }
}
```
More text.
"""
        plan, reason = parse_trend_plan(
            source_turn_id=4,
            target_turn_id=5,
            report=report,
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.target_turn_id, 5)
        self.assertEqual(plan.menu[0], {"name": "Margherita", "price": 120})
        self.assertEqual(plan.bids["Tomato"], {"amount": 27, "quantity": 3})

    def test_plan_to_closed_bid_payload(self) -> None:
        report = """
```json
{
  "menu": [{"name": "Margherita", "price": 120}],
  "bids": {
    "Tomato": {"amount": 27, "quantity": 3}
  }
}
```
"""
        plan, _ = parse_trend_plan(
            source_turn_id=1,
            target_turn_id=1,
            report=report,
        )
        assert plan is not None
        payload = plan_to_closed_bid_payload(plan)
        self.assertEqual(
            payload,
            [{"ingredient": "Tomato", "bid": 27, "quantity": 3}],
        )


if __name__ == "__main__":
    unittest.main()
