"""AAVA MCP server wiring — handshake + method dispatch over the stdio transport.

Supported methods (tools-only server): initialize, notifications/initialized (no-op),
ping, tools/list, tools/call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import protocol
from .protocol import JsonRpcError, METHOD_NOT_FOUND
from .tools import TOOLS, call_tool

# Latest MCP protocol version we implement; we echo the client's requested
# version on initialize when it provides one (forward/backward tolerant).
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "aava"


def _server_version() -> str:
    root = os.environ.get("AAVA_CORE_ROOT") or str(Path(__file__).resolve().parents[2])
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


def _dispatch(method: str, params: dict) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # notification — no response
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [t["spec"] for t in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        if not name:
            raise JsonRpcError(protocol.INVALID_PARAMS, "missing tool name")
        return call_tool(name, params.get("arguments") or {})
    raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")


def serve() -> None:
    """Run the stdio server loop until the client closes stdin."""
    protocol.serve_stdio(_dispatch)
