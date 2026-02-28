import unittest

from menu_pricing import compute_menu_price


class TestMenuPricing(unittest.TestCase):
    def test_default_competitive_uses_fixed_price_two(self) -> None:
        for ingredient_cost in (-10, 0, 1, 7, 42, 999):
            with self.subTest(ingredient_cost=ingredient_cost):
                price, mode = compute_menu_price(
                    ingredient_cost,
                    is_cost_1x_test=False,
                )
                self.assertEqual(price, 2)
                self.assertEqual(mode, "default_competitive_fixed_2")

    def test_cost_1x_test_is_unchanged(self) -> None:
        cases = (
            (-12, 1),
            (0, 1),
            (1, 1),
            (7, 7),
            (39, 39),
            (1000, 1000),
        )
        for ingredient_cost, expected_price in cases:
            with self.subTest(ingredient_cost=ingredient_cost):
                price, mode = compute_menu_price(
                    ingredient_cost,
                    is_cost_1x_test=True,
                )
                self.assertEqual(price, expected_price)
                self.assertEqual(mode, "cost_1x_test")


if __name__ == "__main__":
    unittest.main()
