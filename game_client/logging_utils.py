from __future__ import annotations

import contextvars
import inspect
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "log_context",
    default={},
)

_TAG_EVENT_MAP: dict[str, str] = {
    "INIT": "app.init",
    "EVENT": "event.received",
    "PHASE": "phase.changed",
    "SSE": "sse.lifecycle",
    "HTTP": "http.request",
    "MCP": "mcp.call",
    "BID": "bid.lifecycle",
    "MENU": "menu.lifecycle",
    "MENU-PRICE": "menu.price.calculated",
    "SERVE": "serve.lifecycle",
    "TRACK": "tracker.lifecycle",
    "SELLER": "seller.lifecycle",
    "TREND": "trend.lifecycle",
    "STRATEGY": "strategy.lifecycle",
    "MSG": "message.received",
    "DM": "dm.received",
    "ERROR": "error",
    "WARN": "warning",
    "PRICE": "price.lifecycle",
}

_TAG_LEVEL_MAP: dict[str, str] = {
    "ERROR": "ERROR",
    "WARN": "WARN",
}

_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "ERROR": 40,
}

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
if _LOG_LEVEL not in _LEVEL_ORDER:
    _LOG_LEVEL = "INFO"

_LOG_PROFILE = os.getenv("LOG_PROFILE", "compact").strip().lower() or "compact"
_LOG_FORMAT = os.getenv("LOG_FORMAT", "human").strip().lower() or "human"

_COMPACT_INFO_EVENTS: set[str] = {
    "app.started",
    "dashboard.started",
    "phase.started",
    "phase.changed",
    "bid.submitted",
    "bid.submit.failed",
    "trend.bid.submitted",
    "trend.bid.submit_failed",
    "trend.menu.applied",
    "trend.menu.save_failed",
    "menu.saved",
    "menu.save.failed",
    "serve.submitted",
    "serve.lifecycle",
    "serve.request.received",
    "dish.prepare.completed",
    "dish.prepare.failed",
    "bid.prestige.selected",
    "trend.direction.analyzed",
    "bid.payload.built",
    "event.game_started",
    "menu.price.calculated",
    "tracker.save.completed",
    "tracker.save.failed",
    "seller.completed",
    "seller.failed",
    "event.handler.failed",
    "sse.connection.open",
    "sse.connection.auth_error",
    "sse.connection.error",
    "sse.connection.http_error",
    "mcp.call.failed",
    "http.request.failed",
    "http.request.error",
}


def new_correlation_id() -> str:
    return uuid4().hex


def get_log_context() -> dict[str, Any]:
    return dict(_LOG_CONTEXT.get({}))


def set_log_context(**context: Any) -> None:
    current = dict(_LOG_CONTEXT.get({}))
    for key, value in context.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _LOG_CONTEXT.set(current)


def clear_log_context() -> None:
    _LOG_CONTEXT.set({})


@contextmanager
def log_context(**context: Any):
    current = dict(_LOG_CONTEXT.get({}))
    merged = dict(current)
    for key, value in context.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    token = _LOG_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _infer_component() -> str:
    frame = inspect.currentframe()
    if frame is None:
        return "unknown"

    caller = frame.f_back
    while caller is not None:
        module_name = str(caller.f_globals.get("__name__", "unknown"))
        if module_name != __name__:
            return module_name
        caller = caller.f_back
    return "unknown"


def _to_safe_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _to_safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_safe_json(v) for v in value]
    try:
        return str(value)
    except Exception:
        return "<non-serializable>"


def _format_human_line(
    *, level: str, tag: str, message: str, context: dict[str, Any]
) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    turn_id = context.get("turn_id")
    phase = context.get("phase")

    if level == "ERROR":
        indicator = "✗"
    elif level == "WARN":
        indicator = "⚠"
    else:
        indicator = " "

    ctx_parts = []
    if turn_id is not None:
        ctx_parts.append(f"T{turn_id}")
    if phase:
        ctx_parts.append(str(phase))
    ctx_col = " ".join(ctx_parts)

    tag_col = f"[{tag[:8]:<8}]"
    return f"[{now}] {ctx_col:<18} {indicator} {tag_col} {message}"


def _should_emit(*, level: str, event: str) -> bool:
    normalized_level = str(level).upper()
    level_value = _LEVEL_ORDER.get(normalized_level, _LEVEL_ORDER["INFO"])
    threshold = _LEVEL_ORDER.get(_LOG_LEVEL, _LEVEL_ORDER["INFO"])
    if level_value < threshold:
        return False

    # Human mode shows everything that passes the level filter
    if _LOG_FORMAT == "human":
        return True

    if _LOG_PROFILE == "verbose":
        return True

    if _LOG_PROFILE == "compact":
        if normalized_level in ("WARN", "ERROR"):
            return True
        return event in _COMPACT_INFO_EVENTS

    # "normal" and unknown profiles: only level filtering.
    return True


def log_event(
    *,
    event: str,
    message: str,
    level: str = "INFO",
    legacy_tag: str | None = None,
    component: str | None = None,
    **fields: Any,
) -> None:
    if not _should_emit(level=level, event=event):
        return

    context = get_log_context()

    if _LOG_FORMAT == "human":
        print(_format_human_line(level=level, tag=legacy_tag or "", message=message, context=context))
        return

    payload: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "level": str(level).upper(),
        "event": event,
        "message": message,
        "component": component or str(context.get("component") or _infer_component()),
        "run_id": context.get("run_id"),
        "turn_id": context.get("turn_id"),
        "phase": context.get("phase"),
        "correlation_id": context.get("correlation_id"),
        "legacy_tag": legacy_tag,
    }

    for key in ("event_type", "tool_name", "http_endpoint", "status_code"):
        if key in context and key not in payload:
            payload[key] = context[key]

    for key, value in context.items():
        if key not in payload:
            payload[key] = value

    for key, value in fields.items():
        payload[key] = value

    safe_payload = _to_safe_json(payload)
    print(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")))


def log(tag: str, message: str, **fields: Any) -> None:
    normalized_tag = str(tag or "INFO").upper()
    event = str(fields.pop("event", _TAG_EVENT_MAP.get(normalized_tag, "log.event")))
    level = str(fields.pop("level", _TAG_LEVEL_MAP.get(normalized_tag, "INFO")))
    component = fields.pop("component", None)
    log_event(
        event=event,
        message=message,
        level=level,
        legacy_tag=normalized_tag,
        component=component,
        **fields,
    )
