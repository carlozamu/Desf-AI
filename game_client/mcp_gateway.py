from __future__ import annotations

from typing import Any

from game_client.logging_utils import log


class MCPGateway:
    def __init__(self, *, base_url: str, team_api_key: str) -> None:
        from datapizza.tools.mcp_client import MCPClient

        self._client = MCPClient(
            url=f"{base_url}/mcp",
            headers={"x-api-key": team_api_key},
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self._client.call_tool(tool_name, arguments)
        except Exception as exc:
            log("MCP", f"{tool_name} failed: {exc}")
            return None
