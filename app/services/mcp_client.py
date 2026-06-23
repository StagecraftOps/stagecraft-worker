import asyncio
import logging

from mcp import ClientSession
from mcp.client.sse import sse_client

from app.core.config import settings

logger = logging.getLogger(__name__)

# Maps an MCP tool name to the server that implements it. All tools here are
# read-only — see nodes.py for why write tools (branch/commit/PR) are
# deliberately absent from every tool-calling loop in this codebase.
_MCP_GITHUB_SERVER_TOOLS = {
    "get_workflow_yaml",
    "get_run_logs",
    "search_remediations",
}


def _server_url_for(tool_name: str) -> str:
    if tool_name in _MCP_GITHUB_SERVER_TOOLS:
        return settings.MCP_GITHUB_URL
    raise ValueError(f"Unknown MCP tool '{tool_name}'")


async def call_tool(tool_name: str, params: dict) -> str:
    """Call an MCP tool by name over in-cluster SSE and return its result as text.

    Uses the official MCP Python SDK (mcp.client.sse). The model decides to call
    a tool (Converse tool-use); the worker bridges that call to the in-cluster
    MCP server here and feeds the result back.
    """
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
