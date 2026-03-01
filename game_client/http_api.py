from __future__ import annotations

from typing import Any

import aiohttp

from game_client.logging_utils import log


class ApiClient:
    def __init__(self, *, base_url: str, team_api_key: str, team_id: str = "") -> None:
        self.base_url = base_url
        self.team_api_key = team_api_key
        self.team_id = team_id

    async def api_get(self, endpoint: str, *, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        headers = {"x-api-key": self.team_api_key}
        timeout = aiohttp.ClientTimeout(total=20)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        log(
                            "HTTP",
                            f"GET {endpoint} ok",
                            event="http.request.completed",
                            http_endpoint=endpoint,
                            status_code=200,
                        )
                        return data

                    text = await response.text()
                    log(
                        "HTTP",
                        f"GET {endpoint} -> {response.status} {text[:180]}",
                        event="http.request.failed",
                        level="WARN",
                        http_endpoint=endpoint,
                        status_code=response.status,
                    )
                    return None
        except aiohttp.ClientError as exc:
            log(
                "HTTP",
                f"GET {endpoint} failed: {exc}",
                event="http.request.error",
                level="ERROR",
                http_endpoint=endpoint,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return None

    async def get_recipes(self) -> list[dict[str, Any]]:
        data = await self.api_get("/recipes")
        return data if isinstance(data, list) else []

    async def get_my_meals(self, turn_id: int, *, restaurant_id: str) -> list[dict[str, Any]]:
        data = await self.api_get(
            "/meals",
            params={"turn_id": turn_id, "restaurant_id": restaurant_id},
        )
        return data if isinstance(data, list) else []

    async def get_restaurant_info(self) -> dict[str, Any] | None:
        if not self.team_id:
            return None
        data = await self.api_get(f"/restaurant/{self.team_id}")
        return data if isinstance(data, dict) else None

    async def get_market_entries(self) -> list[dict[str, Any]]:
        data = await self.api_get("/market/entries")
        return data if isinstance(data, list) else []
