import asyncio
import logging

from mcp import ClientSession
from mcp.client.sse import sse_client

from app.core.config import settings

logger = logging.getLogger(__name__)

_MCP_GITHUB_SERVER_TOOLS = {
    "get_workflow_yaml",
    "get_run_logs",
    "search_remediations",
    "get_pull_request_diff",
    "query_graph",
}

def _server_url_for(tool_name: str) -> str:
    if tool_name in _MCP_GITHUB_SERVER_TOOLS:
        return settings.MCP_GITHUB_URL
    raise ValueError(f"Unknown MCP tool '{tool_name}'")

async def call_tool(tool_name: str, params: dict) -> str:
    url = _server_url_for(tool_name)
    try:
        async with asyncio.timeout(settings.MCP_TOOL_TIMEOUT_SECONDS):
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=params)
    except TimeoutError as exc:
        raise TimeoutError(
            f"MCP tool {tool_name!r} exceeded {settings.MCP_TOOL_TIMEOUT_SECONDS}s"
        ) from exc

    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts) if parts else str(result)
