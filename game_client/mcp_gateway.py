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
        log(
            "MCP",
            f"calling MCP tool {tool_name}",
            event="mcp.call.started",
            tool_name=tool_name,
            payload_summary={"keys": sorted(arguments.keys()), "size": len(arguments)},
        )
        try:
            response = await self._client.call_tool(tool_name, arguments)
            log("MCP", f"{tool_name} completed", event="mcp.call.completed", tool_name=tool_name)
            return response
        except Exception as exc:
            log(
                "MCP",
                f"{tool_name} failed: {exc}",
                event="mcp.call.failed",
                tool_name=tool_name,
                level="ERROR",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return None
