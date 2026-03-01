import unittest

from menu_pricing import compute_menu_price


class TestMenuPricing(unittest.TestCase):
    def test_default_uses_tiered_fixed_prices(self) -> None:
        cases = (
            (0, 2),
            (1, 50),
            (2, 250),
            (3, 250),
            (10, 250),
        )
        for dish_index, expected_price in cases:
            with self.subTest(dish_index=dish_index):
                price, mode = compute_menu_price(
                    ingredient_cost=42,
                    is_cost_1x_test=False,
                    dish_index=dish_index,
                )
                self.assertEqual(price, expected_price)
                self.assertEqual(mode, "default_tiered_fixed_2_50_250")

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
