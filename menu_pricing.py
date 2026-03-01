from __future__ import annotations

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

    _ = dish_index
    dynamic_price = max(10, int(ingredient_cost) * 3)
    return _clamp_menu_price(dynamic_price), "fallback_cost_3x"
