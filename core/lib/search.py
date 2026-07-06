"""Local search over a cached realm snapshot.

Pure, transport-free matching over the JSON snapshot produced by lib/cache.py.
Shared by bin/aava-search (CLI) and the MCP server (lib/mcp) so the two never drift.

Stdlib only.
"""

from __future__ import annotations

from typing import Optional

SEARCHABLE_TYPES = ("agents", "workflows", "tools", "kbs", "guardrails", "models")

# Per-type fields to match against. Order matters for label selection (first hit wins).
MATCH_FIELDS = {
    "agents":     ("name", "agentName", "role", "goal", "backstory", "agentDetails", "tags", "practiceArea"),
    "workflows":  ("name", "workFlowName", "description", "tags", "practiceArea"),
    "tools":      ("name", "toolName", "toolDescription", "description", "tags"),
    "kbs":        ("name", "collectionName", "description", "type"),
    "guardrails": ("name", "guardrailName", "description", "type"),
    "models":     ("name", "model", "description", "aiEngine", "type"),
}

ID_FIELDS = ("id", "agentId", "workFlowId", "workflowId", "toolId", "kbId", "collectionId", "guardrailId")


def entity_id(entity: dict) -> str:
    for f in ID_FIELDS:
        if f in entity:
            return str(entity[f])
    return "?"


def entity_label(entity: dict, entity_type: str) -> str:
    for f in MATCH_FIELDS.get(entity_type, ("name",)):
        v = entity.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(unnamed)"


def matches(entity: dict, entity_type: str, query: str) -> bool:
    q = query.lower()
    for f in MATCH_FIELDS.get(entity_type, ("name",)):
        v = entity.get(f)
        if v is None:
            continue
        if isinstance(v, str):
            if q in v.lower():
                return True
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and q in item.lower():
                    return True
                elif isinstance(item, dict):
                    for sub in item.values():
                        if isinstance(sub, str) and q in sub.lower():
                            return True
    return False


def search_snapshot(snapshot: dict, query: str, *,
                    entity_type: Optional[str] = None, limit: int = 20) -> dict:
    """Case-insensitive substring search across the snapshot.

    Returns {"total": int, "hits": {type: [{"id", "label", "raw"}, ...]}}.
    """
    types = (entity_type,) if entity_type else SEARCHABLE_TYPES
    hits: dict[str, list[dict]] = {}
    total = 0
    for t in types:
        items = snapshot.get(t) or []
        found = [e for e in items if isinstance(e, dict) and matches(e, t, query)]
        if found:
            capped = found[:limit]
            hits[t] = [{"id": entity_id(e), "label": entity_label(e, t), "raw": e} for e in capped]
            total += len(capped)
    return {"total": total, "hits": hits}
