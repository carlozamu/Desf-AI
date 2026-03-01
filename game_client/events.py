from __future__ import annotations

from typing import Any, Awaitable, Callable

from game_client.config import ClientConfig
from game_client.logging_utils import log, set_log_context
from game_client.state import GameState
from game_client.strategy import (
    get_client_id_from_event,
    pick_dish_for_order,
    resolve_client_id_from_meals,
)


class EventRouter:
    def __init__(
        self,
        *,
        config: ClientConfig,
        state: GameState,
        api_client: Any,
        mcp_gateway: Any,
        phase_runner: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.api_client = api_client
        self.mcp_gateway = mcp_gateway
        self.phase_runner = phase_runner
        self.event_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "game_started": self.game_started,
            "game_phase_changed": self.game_phase_changed,
            "game_reset": self.game_reset,
            "client_spawned": self.client_spawned,
            "preparation_complete": self.preparation_complete,
            "message": self.message,
            "new_message": self.new_message,
        }

    async def game_started(self, data: dict[str, Any]) -> None:
        turn_id = int(data.get("turn_id", self.state.current_turn_id or 0) or 0)
        self.state.reset_for_game_start(turn_id)
        set_log_context(turn_id=self.state.current_turn_id, phase=self.state.current_phase)
        log(
            "EVENT",
            f"Partita iniziata — turno {self.state.current_turn_id}",
            event="event.game_started",
            event_data=data,
        )
        await self.mcp_gateway.call_tool("update_restaurant_is_open", {"is_open": True})

    async def game_phase_changed(self, data: dict[str, Any]) -> None:
        if data.get("turn_id") is not None:
            self.state.current_turn_id = int(data["turn_id"])

        phase = data.get("phase", "unknown")
        self.state.current_phase = str(phase)
        set_log_context(turn_id=self.state.current_turn_id, phase=self.state.current_phase)
        log(
            "PHASE",
            f"→ {phase}",
            event="phase.changed",
            event_data=data,
        )
        handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "speaking": self.phase_runner.speaking_phase_started,
            "closed_bid": lambda: self.phase_runner.closed_bid_phase_started(data),
            "waiting": self.phase_runner.waiting_phase_started,
            "serving": self.phase_runner.serving_phase_started,
            "stopped": self.phase_runner.end_turn,
        }

        handler = handlers.get(phase)
        if handler:
            await handler()
        else:
            log(
                "PHASE",
                f"fase non gestita: {phase}",
                event="phase.unhandled",
                level="WARN",
            )

    async def game_reset(self, data: dict[str, Any]) -> None:
        self.state.current_phase = "stopped"
        set_log_context(phase=self.state.current_phase)
        log("EVENT", "Partita resettata", event="event.game_reset", event_data=data)

    async def client_spawned(self, data: dict[str, Any]) -> None:
        client_name = str(data.get("clientName", data.get("name", "")))
        order_text = str(data.get("orderText", data.get("order", "")))
        log(
            "SERVE",
            f"Cliente '{client_name}' ordina: {order_text}",
            event="serve.request.received",
            client_name=client_name,
            order_text=order_text,
        )

        dish_name = pick_dish_for_order(order_text, self.state.active_menu)
        if not dish_name:
            log(
                "SERVE",
                f"Nessun piatto per '{client_name}' (cerca: {order_text})",
                event="serve.request.no_matching_dish",
                client_name=client_name,
                order_text=order_text,
            )
            return

        if self.state.dishes_ready.get(dish_name) is not True:
            log(
                "SERVE",
                f"Piatto '{dish_name}' non ancora pronto per '{client_name}'",
                event="serve.request.dish_not_ready",
                dish_name=dish_name,
                client_name=client_name,
            )
            return

        client_id = get_client_id_from_event(data)
        if not client_id:
            log(
                "SERVE",
                f"ID cliente mancante per '{client_name}', cerco nei meals...",
                event="serve.client_id.resolve_from_meals",
                client_name=client_name,
                dish_name=dish_name,
            )
            client_id = await resolve_client_id_from_meals(
                order_text,
                client_name,
                current_turn_id=self.state.current_turn_id,
                get_my_meals_fn=lambda turn_id: self.api_client.get_my_meals(
                    turn_id,
                    restaurant_id=self.config.team_id,
                ),
            )

        if not client_id:
            log(
                "SERVE",
                f"Impossibile identificare il cliente '{client_name}' (ordine: {order_text})",
                event="serve.client_id.resolution_failed",
                level="WARN",
                order_text=order_text,
                client_name=client_name,
                dish_name=dish_name,
            )
            return

        log(
            "SERVE",
            f"Cliente identificato: {client_id}",
            event="serve.client_id.resolved",
            client_id=client_id,
            dish_name=dish_name,
        )
        await self.mcp_gateway.call_tool("serve_dish", {"dish_name": dish_name, "client_id": client_id})
        self.state.dishes_ready[dish_name] = False
        log(
            "SERVE",
            f"Servito '{dish_name}' a '{client_name}'",
            event="serve.submitted",
            client_id=client_id,
            dish_name=dish_name,
            client_name=client_name,
        )

        # Rimettiamo in preparazione il piatto servito per rifornire la linea.
        await self.mcp_gateway.call_tool("prepare_dish", {"dish_name": dish_name})
        log(
            "SERVE",
            f"'{dish_name}' rimesso in preparazione",
            event="dish.prepare.submitted",
            dish_name=dish_name,
        )

    async def preparation_complete(self, data: dict[str, Any]) -> None:
        dish_name = str(data.get("dish", "")).strip()
        if not dish_name:
            return

        self.state.dishes_ready[dish_name] = True
        log("SERVE", f"Piatto '{dish_name}' pronto ✓", event="dish.prepare.completed", dish_name=dish_name)

    async def message(self, data: dict[str, Any]) -> None:
        sender = data.get("sender")
        payload = data.get("payload")
        log("MSG", f"from={sender} payload={payload}", event="message.received", sender=sender)

    async def new_message(self, data: dict[str, Any]) -> None:
        sender_name = data.get("senderName", data.get("senderId", "unknown"))
        text = data.get("text", "")
        log("DM", f"from={sender_name}: {text}", event="message.direct.received", sender=sender_name)

    async def dispatch_event(self, event_type: str, event_data: dict[str, Any]) -> None:
        handler = self.event_handlers.get(event_type)
        if not handler:
            log(
                "EVENT",
                f"no handler for event_type={event_type}",
                event="event.unhandled",
                level="WARN",
                event_type=event_type,
            )
            return

        try:
            await handler(event_data)
        except Exception as exc:
            log(
                "ERROR",
                f"handler failed for {event_type}: {exc}",
                event="event.handler.failed",
                event_type=event_type,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
