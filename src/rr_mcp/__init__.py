"""rr-mcp: MCP server for orchestrating multi-process rr debugging sessions."""

from rr_mcp.errors import RrMcpError, SessionNotFoundError, TraceNotFoundError
from rr_mcp.models import ProcessInfo, TraceInfo, TraceSummary

__all__ = [
    "RrMcpError",
    "SessionNotFoundError",
    "TraceNotFoundError",
    "ProcessInfo",
    "TraceInfo",
    "TraceSummary",
]

from importlib.metadata import version

__version__ = version("rr-mcp")
