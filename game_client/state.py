from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GameState:
    current_turn_id: int = 0
    target_recipes: list[dict[str, Any]] = field(default_factory=list)
    active_menu: list[dict[str, Any]] = field(default_factory=list)
    dishes_ready: dict[str, bool] = field(default_factory=dict)
    ingredient_bids: dict[str, int] = field(default_factory=dict)

    def reset_for_game_start(self, turn_id: int) -> None:
        self.current_turn_id = int(turn_id or 0)
        self.target_recipes = []
        self.active_menu = []
        self.dishes_ready = {}
        self.ingredient_bids = {}
