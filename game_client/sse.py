from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import aiohttp

from game_client.config import ClientConfig
from game_client.logging_utils import log


async def handle_line(
    raw_line: bytes,
    *,
    dispatch_event_fn: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    if not raw_line:
        return

    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return

    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})

    if isinstance(event_data, dict):
        await dispatch_event_fn(event_type, event_data)
    else:
        await dispatch_event_fn(event_type, {"value": event_data})


async def listen_once(
    session: aiohttp.ClientSession,
    *,
    config: ClientConfig,
    dispatch_event_fn: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    url = f"{config.base_url}/events/{config.team_id}"
    headers = {"Accept": "text/event-stream", "x-api-key": config.team_api_key}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open")

        async for line in response.content:
            await handle_line(line, dispatch_event_fn=dispatch_event_fn)


async def listen_forever(
    *,
    config: ClientConfig,
    dispatch_event_fn: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                await listen_once(session, config=config, dispatch_event_fn=dispatch_event_fn)
                log("SSE", f"connection closed, retry in {config.sse_retry_delay}s")
                await asyncio.sleep(config.sse_retry_delay)
            except aiohttp.ClientResponseError as exc:
                if exc.status == 409:
                    log(
                        "SSE",
                        (
                            "409 Conflict: esiste gia una connessione attiva "
                            f"per team {config.team_id}. "
                            f"Retry in {config.sse_conflict_retry_delay}s"
                        ),
                    )
                    await asyncio.sleep(config.sse_conflict_retry_delay)
                    continue

                if exc.status in (401, 403):
                    raise SystemExit(f"SSE auth error ({exc.status}). Verifica TEAM_ID/API key.")

                log("SSE", f"HTTP error {exc.status}, retry in {config.sse_retry_delay}s")
                await asyncio.sleep(config.sse_retry_delay)
            except aiohttp.ClientError as exc:
                log("SSE", f"connection error: {exc}; retry in {config.sse_retry_delay}s")
                await asyncio.sleep(config.sse_retry_delay)
