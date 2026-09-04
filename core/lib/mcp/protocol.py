"""MCP stdio transport — JSON-RPC 2.0 over newline-delimited stdin/stdout.

Stdlib only. One JSON object per line (the MCP stdio framing). Requests carry an
`id` and get a response; notifications (no `id`) get none. Protocol-level failures
become JSON-RPC error responses; tool-execution failures are NOT raised here —
they are returned by the handler as MCP `isError` content (see tools.py).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Optional

JSONRPC = "2.0"

# JSON-RPC standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """Raise from a dispatcher to emit a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _read_message(stream) -> Optional[dict]:
    """Read one JSON message (skip blank lines). None at EOF. Raises on bad JSON."""
    while True:
        line = stream.readline()
        if not line:
            return None  # EOF
        line = line.strip()
        if line:
            return json.loads(line)


def _write_message(stream, obj: dict) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


def _error(mid: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "id": mid, "error": err}


def serve_stdio(dispatch: Callable[[str, dict], Any],
                stdin=None, stdout=None) -> None:
    """Blocking read/dispatch/write loop until EOF.

    `dispatch(method, params)` returns the result for a request, or None for a
    notification. Raise JsonRpcError for protocol errors.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    while True:
        try:
            msg = _read_message(stdin)
        except json.JSONDecodeError as e:
            _write_message(stdout, _error(None, PARSE_ERROR, f"parse error: {e}"))
            continue
        if msg is None:
            return  # EOF — client closed

        mid = msg.get("id")
        is_notification = "id" not in msg
        method = msg.get("method")
        params = msg.get("params") or {}

        if not isinstance(method, str):
            if not is_notification:
                _write_message(stdout, _error(mid, INVALID_REQUEST, "missing method"))
            continue

        try:
            result = dispatch(method, params)
            if not is_notification:
                _write_message(stdout, {"jsonrpc": JSONRPC, "id": mid, "result": result})
        except JsonRpcError as e:
            if not is_notification:
                _write_message(stdout, _error(mid, e.code, e.message, e.data))
        except Exception as e:  # noqa: BLE001 — never let one bad call kill the server
            if not is_notification:
                _write_message(stdout, _error(mid, INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
