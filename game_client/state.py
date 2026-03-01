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
    dishes_preparing: int = 0
    dishes_in_preparation: set[str] = field(default_factory=set)
    bid_min_effective: int = 0
    bid_max_effective: int = 0
    closed_bid_planned_spend: int = 0
    auction_spend_total: int = 0
    market_spend_total: int = 0
    ingredient_bids: dict[str, int] = field(default_factory=dict)
    previous_turn_demand: dict[str, dict[str, int]] = field(default_factory=dict)
    previous_turn_demand_turn_id: int = 0
    next_turn_trend_plan: "TrendPlan | None" = None
    trend_direction: "TrendDirection | None" = None

    def reset_for_game_start(self, turn_id: int) -> None:
        self.current_turn_id = int(turn_id or 0)
        self.current_phase = "speaking"
        self.target_recipes = []
        self.all_recipes = []
        self.active_menu = []
        self.dishes_ready = {}
        self.dishes_preparing = 0
        self.dishes_in_preparation = set()
        self.bid_min_effective = 0
        self.bid_max_effective = 0
        self.closed_bid_planned_spend = 0
        self.auction_spend_total = 0
        self.market_spend_total = 0
        self.ingredient_bids = {}
        self.previous_turn_demand = {}
        self.previous_turn_demand_turn_id = 0
        self.next_turn_trend_plan = None
        self.trend_direction = None
