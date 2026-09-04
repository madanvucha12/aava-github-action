"""AAVA MCP tools — a curated, read-only/query surface over lib/.

Phase 1 surface (all read-only / safe; no authoring or write-path):
  aava_discover, aava_search, aava_assess, aava_runs, aava_lifecycle,
  aava_whoami, aava_ping

Each tool = an MCP spec ({name, description, inputSchema}) + a handler(args).
Tool descriptions are sourced from the shared registry (prompts/descriptions.json)
so wording matches the CLI hosts. lib imports are lazy (inside handlers), matching
the bin pattern. Stdlib only.

Tool-execution failures (e.g. AuthError when no token, a missing arg) are returned
as MCP `isError` content so the model sees them — they are NOT protocol errors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .protocol import INVALID_PARAMS, JsonRpcError

_SEARCH_TYPES = ("agents", "workflows", "tools", "kbs", "guardrails", "models")


def _core_root() -> Path:
    return Path(os.environ.get("AAVA_CORE_ROOT") or Path(__file__).resolve().parents[2])


def _registry_desc(key: str, fallback: str) -> str:
    """Pull a tool description from the shared registry; host token stripped."""
    try:
        reg = json.loads((_core_root() / "prompts" / "descriptions.json").read_text(encoding="utf-8"))
        text = (reg.get("skills") or {}).get(key) or (reg.get("agents") or {}).get(key)
        if not text:
            return fallback
        return text.replace("{{SLASH_PREFIX}}:", "")
    except Exception:
        return fallback


# ── handlers (lazy lib imports, in-process) ──────────────────────────────

def _h_discover(args: dict) -> Any:
    from lib.transport import get_transport
    from lib.cache import Cache
    t = get_transport()
    scope = t.scope_key
    snap = Cache().refresh(scope, t)
    return {"scope": scope, "counts": Cache.counts(snap),
            "errors": (snap.get("meta") or {}).get("errors", [])}


def _h_search(args: dict) -> Any:
    if not args.get("query"):
        raise KeyError("query is required")
    from lib.transport import get_transport
    from lib.cache import Cache
    from lib.search import search_snapshot
    t = get_transport()
    scope = t.scope_key
    snap = Cache().get_or_refresh(scope, t)
    res = search_snapshot(snap, args["query"], entity_type=args.get("type"),
                          limit=int(args.get("limit", 20)))
    # token economy: return id + label (drop raw bodies) — callers can lifecycle-fetch detail
    hits = {ty: [{"id": h["id"], "label": h["label"]} for h in v] for ty, v in res["hits"].items()}
    return {"query": args["query"], "scope": scope, "total": res["total"], "hits": hits}


def _h_assess(args: dict) -> Any:
    from lib.transport import get_transport
    from lib.cache import Cache
    from lib.assessor_lib import assess_snapshot
    t = get_transport()
    scope = t.scope_key
    snap = Cache().get_or_refresh(scope, t)
    return assess_snapshot(snap)


def _h_runs(args: dict) -> Any:
    from lib.runs import Runs
    return {"runs": Runs().list_runs(
        workflow_id=args.get("workflow_id"),
        status=args.get("status"),
        limit=int(args.get("limit", 20)),
    )}


def _h_lifecycle(args: dict) -> Any:
    if not args.get("kind") or not args.get("entity_id"):
        raise KeyError("kind and entity_id are required")
    from lib.transport import get_transport
    return get_transport().get_artifact(args["kind"], args["entity_id"])


def _h_whoami(args: dict) -> Any:
    from lib.transport import get_transport
    return get_transport().whoami()


def _h_ping(args: dict) -> Any:
    from lib.transport import get_transport
    return get_transport().ping()


# ── registry ─────────────────────────────────────────────────────────────

def _tool(name: str, reg_key: str, fallback: str, schema: dict, handler: Callable) -> dict:
    return {
        "name": name,
        "handler": handler,
        "spec": {
            "name": name,
            "description": _registry_desc(reg_key, fallback),
            "inputSchema": schema,
        },
    }


TOOLS = [
    _tool("aava_discover", "aava-discover",
          "Refresh the local AAVA metadata cache from the backend.",
          {"type": "object", "properties": {}},
          _h_discover),
    _tool("aava_search", "aava-search",
          "Find existing AAVA artifacts (agents, workflows, tools, KBs, guardrails) by keyword.",
          {"type": "object",
           "properties": {"query": {"type": "string", "description": "Case-insensitive substring"},
                          "type": {"type": "string", "enum": list(_SEARCH_TYPES),
                                   "description": "Restrict to one entity type"},
                          "limit": {"type": "integer", "description": "Max hits per type (default 20)"}},
           "required": ["query"]},
          _h_search),
    _tool("aava_assess", "aava-assess",
          "Score the current AAVA scope against the maturity model (read-only, advisory).",
          {"type": "object", "properties": {}},
          _h_assess),
    _tool("aava_runs", "aava-runs",
          "List past AAVA workflow run records from the local cache.",
          {"type": "object",
           "properties": {"workflow_id": {"type": "string"},
                          "status": {"type": "string"},
                          "limit": {"type": "integer", "description": "Max runs (default 20)"}}},
          _h_runs),
    _tool("aava_lifecycle", "aava-lifecycle",
          "Fetch an AAVA artifact's current state/body by kind + id.",
          {"type": "object",
           "properties": {"kind": {"type": "string",
                                   "enum": ["agent", "workflow", "tool", "kb", "guardrail"]},
                          "entity_id": {"type": "string"}},
           "required": ["kind", "entity_id"]},
          _h_lifecycle),
    _tool("aava_whoami", "",
          "Resolve the current AAVA user identity (email, roles).",
          {"type": "object", "properties": {}},
          _h_whoami),
    _tool("aava_ping", "",
          "Check AAVA backend reachability.",
          {"type": "object", "properties": {}},
          _h_ping),
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def call_tool(name: str, args: dict) -> dict:
    """Invoke a tool and wrap the result as MCP content. Execution errors → isError."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise JsonRpcError(INVALID_PARAMS, f"unknown tool: {name}")
    try:
        result = handler(args or {})
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:  # noqa: BLE001 — surface as a tool error, not a protocol crash
        return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
