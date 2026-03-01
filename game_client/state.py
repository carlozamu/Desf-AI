from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from game_client.prestige_strategy import TrendDirection
    from game_client.trend_strategy import TrendPlan


@dataclass
class GameState:
    current_turn_id: int = 0
    current_phase: str = "stopped"
    target_recipes: list[dict[str, Any]] = field(default_factory=list)
    all_recipes: list[dict[str, Any]] = field(default_factory=list)
    active_menu: list[dict[str, Any]] = field(default_factory=list)
    dishes_ready: dict[str, bool] = field(default_factory=dict)
    ingredient_bids: dict[str, int] = field(default_factory=dict)
    next_turn_trend_plan: "TrendPlan | None" = None
    trend_direction: "TrendDirection | None" = None

    def reset_for_game_start(self, turn_id: int) -> None:
        self.current_turn_id = int(turn_id or 0)
        self.current_phase = "speaking"
        self.target_recipes = []
        self.all_recipes = []
        self.active_menu = []
        self.dishes_ready = {}
        self.ingredient_bids = {}
        self.next_turn_trend_plan = None
        self.trend_direction = None
