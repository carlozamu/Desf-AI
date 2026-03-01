from __future__ import annotations

FIXED_MENU_PRICES = (2, 50, 250)


def _clamp_menu_price(price: int) -> int:
    return max(1, min(999, int(price)))


def compute_menu_price(
    ingredient_cost: int,
    *,
    is_cost_1x_test: bool,
    dish_index: int,
) -> tuple[int, str]:
    if is_cost_1x_test:
        return max(1, int(ingredient_cost)), "cost_1x_test"

    index = max(0, int(dish_index))
    if index < len(FIXED_MENU_PRICES):
        price = FIXED_MENU_PRICES[index]
    else:
        price = FIXED_MENU_PRICES[-1]

    return _clamp_menu_price(price), "default_tiered_fixed_2_50_250"
