import unittest

from menu_pricing import compute_menu_price


class TestMenuPricing(unittest.TestCase):
    def test_default_uses_dynamic_cost_multiplier(self) -> None:
        cases = (
            (1, 10),
            (3, 10),
            (7, 21),
            (42, 126),
        )
        for ingredient_cost, expected_price in cases:
            for dish_index in (0, 1, 2, 3):
                with self.subTest(ingredient_cost=ingredient_cost, dish_index=dish_index):
                    price, mode = compute_menu_price(
                        ingredient_cost=ingredient_cost,
                        is_cost_1x_test=False,
                        dish_index=dish_index,
                    )
                    self.assertEqual(price, expected_price)
                    self.assertEqual(mode, "fallback_cost_3x")

    def test_cost_1x_test_is_unchanged(self) -> None:
        cases = (
            (-12, 1),
            (0, 1),
            (1, 1),
            (7, 7),
            (39, 39),
            (1000, 1000),
        )
        for dish_index in (0, 1, 2, 3):
            for ingredient_cost, expected_price in cases:
                with self.subTest(dish_index=dish_index, ingredient_cost=ingredient_cost):
                    price, mode = compute_menu_price(
                        ingredient_cost,
                        is_cost_1x_test=True,
                        dish_index=dish_index,
                    )
                    self.assertEqual(price, expected_price)
                    self.assertEqual(mode, "cost_1x_test")


if __name__ == "__main__":
    unittest.main()
