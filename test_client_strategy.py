import unittest

from game_client.strategy import (
    build_low_cost_bids,
    normalize_order_text,
    pick_dish_for_order,
    recipe_ingredients,
    resolve_client_id_from_meals,
)


class TestClientStrategy(unittest.IsolatedAsyncioTestCase):
    def test_normalize_order_text(self) -> None:
        text = "I want to eat Margherita. I'm intolerant to lactose"
        self.assertEqual(normalize_order_text(text), "margherita")
        self.assertEqual(normalize_order_text(""), "")

    def test_recipe_ingredients_accepts_dict_and_list(self) -> None:
        dict_recipe = {"ingredients": {"tomato": 2, "cheese": 0, "salt": "x"}}
        list_recipe = {"ingredients": ["tomato", "tomato", "basil"]}

        self.assertEqual(recipe_ingredients(dict_recipe), {"tomato": 2, "cheese": 1, "salt": 1})
        self.assertEqual(recipe_ingredients(list_recipe), {"tomato": 2, "basil": 1})

    def test_build_low_cost_bids_sets_quantities_and_prices(self) -> None:
        recipes = [
            {"name": "A", "ingredients": {"tomato": 1, "cheese": 1}},
            {"name": "B", "ingredients": {"tomato": 2}},
        ]

        bids, bid_map = build_low_cost_bids(
            recipes,
            min_ingredient_bid=20,
            max_ingredient_bid=30,
            portions_per_recipe=2,
        )

        self.assertEqual(len(bids), 2)
        tomato_bid = next(row for row in bids if row["ingredient"] == "tomato")
        cheese_bid = next(row for row in bids if row["ingredient"] == "cheese")

        self.assertEqual(tomato_bid["quantity"], 6)
        self.assertEqual(cheese_bid["quantity"], 2)
        self.assertEqual(bid_map["tomato"], 25)
        self.assertEqual(bid_map["cheese"], 20)

    def test_pick_dish_for_order(self) -> None:
        menu = [{"name": "Margherita", "price": 10}, {"name": "Diavola", "price": 12}]
        self.assertEqual(pick_dish_for_order("I want to eat Margherita", menu), "Margherita")
        self.assertIsNone(pick_dish_for_order("I want to eat Carbonara", menu))

    async def test_resolve_client_id_from_meals_matches_order(self) -> None:
        async def fake_get_my_meals(_: int):
            return [
                {
                    "request": "I want to eat margherita",
                    "executed": False,
                    "customer": {"name": "Luca"},
                    "customerId": "c-123",
                }
            ]

        client_id = await resolve_client_id_from_meals(
            "I want to eat Margherita",
            "Luca",
            current_turn_id=3,
            get_my_meals_fn=fake_get_my_meals,
        )
        self.assertEqual(client_id, "c-123")


if __name__ == "__main__":
    unittest.main()
