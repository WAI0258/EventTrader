"""MCP server adapter for the project-owned Analysis tool gateway."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from event_trader.integrations.analysis_mcp_transport import (
    analysis_tool_context_from_env,
)
from event_trader.reasoning.analysis_tools import (
    ANALYSIS_TOOL_NAMES,
    AnalysisToolGateway,
)

_gateway = AnalysisToolGateway(context=analysis_tool_context_from_env(os.environ))
mcp = FastMCP("event_trader_analysis")
for _tool_name in ANALYSIS_TOOL_NAMES:
    mcp.tool()(_gateway.bind_tool(_tool_name))


if __name__ == "__main__":
    mcp.run(transport="stdio")
