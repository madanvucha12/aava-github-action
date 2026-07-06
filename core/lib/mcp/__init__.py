"""AAVA MCP server — exposes the host-agnostic AAVA core (read/query/assess)
as Model Context Protocol tools over stdio.

Stdlib only, no third-party MCP SDK: the JSON-RPC 2.0 stdio transport is small
and hand-rolled (see protocol.py) to preserve core's zero-dependency invariant
and keep the server trivially bundleable (e.g. inside a VS Code extension VSIX).

This is AAVA-as-MCP-*server* (to a client like VS Code / Copilot). It is the
opposite direction from lib/transport/factory.py's `mcp` branch, which is
AAVA-as-MCP-*client* to the future Secure Gateway. This server reaches the AAVA
backend through the existing REST transport.
"""
