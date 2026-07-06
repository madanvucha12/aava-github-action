"""Transport factory — REST today, MCP via Secure Gateway later (P11).

Selection order:
  1. ${user_config.aava-transport} (set by Claude Code as CLAUDE_PLUGIN_OPTION_AAVA_TRANSPORT)
  2. AAVA_TRANSPORT env var
  3. Default: rest

Singleton policy: one transport instance per process for connection pooling.
The scope key is mutable — get_transport(scope_key=X) updates the singleton's
internal scope when X is provided. Since AAVA scopes by token-derived RBAC (2026-06),
this key is a cache-partition hint only; hierarchy-node selection uses set_hierarchy().
"""

from __future__ import annotations  # Python 3.9 compat

import os
from typing import Optional

from .base import AavaTransport


_singleton: Optional[AavaTransport] = None


def get_transport(scope_key: Optional[str] = None) -> AavaTransport:
    """Return the cached transport (creating it on first call).

    Pass `scope_key` to override the cache-partition key for this call; the singleton's
    internal key is updated so callers dispatching across scopes in the same process
    don't inherit the first-call value.
    """
    global _singleton

    if _singleton is None:
        name = (os.environ.get("AAVA_TRANSPORT")
                or os.environ.get("CLAUDE_PLUGIN_OPTION_AAVA_TRANSPORT")
                or "rest").lower()

        if name == "rest":
            from .rest import RestTransport
            _singleton = RestTransport()
        elif name == "mcp":
            # P11 — lights up when Secure Gateway is GA for external clients.
            raise NotImplementedError(
                "MCP transport is P11. Use rest for now (default). "
                "MCP requires the Secure MCP Gateway to be production-ready."
            )
        else:
            raise ValueError(f"Unknown transport: {name!r}. Expected 'rest' or 'mcp'.")

    if scope_key is not None and str(scope_key) != str(_singleton.realm_id):
        _singleton.realm_id = str(scope_key)

    return _singleton


def reset_transport() -> None:
    """Test / debug helper: drop the singleton so the next get_transport() builds fresh."""
    global _singleton
    _singleton = None
