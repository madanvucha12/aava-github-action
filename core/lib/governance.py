"""Governance helpers — write paths, audit log, lifecycle pre-flight assessment.

Architectural decisions (read this once):

1. **Authoring is Claude-guided, not assessor-gated.** The formal assessor
   (vendor/assessor) runs only at two well-defined moments:
     a. Pre-flight before raising IN_REVIEW (always advisory; never blocks).
     b. Explicit user request (`/aava:assess` or "assess my workflow design").
   During authoring, Claude consults the framework, guidelines, the realm
   cache, and the learning loop to guide drafts naturally — no scorecard
   interrupts the flow.

2. **Lifecycle gates require explicit user confirmation.** DRAFT → IN_REVIEW
   and IN_REVIEW → APPROVED are deliberate two-step flows enforced at the
   bin layer (dry-run unless --yes) and reinforced by skill discipline
   and Claude Code's native Bash permission prompt.

3. **Skills route writes through `gated_create` / `gated_update`.** Those
   helpers don't run the assessor anymore; their job is: write to AAVA
   via transport, append an audit entry, raise on transport error.
   Skills never call `transport.create_<kind>` directly — that bypasses
   the audit trail.
"""

from __future__ import annotations  # Python 3.9 compat

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _audit_path() -> Path:
    """Audit log lives in plugin data dir, separate from cache."""
    root = Path(os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
                or Path.home() / ".local" / "share" / "aava")
    p = root / "audit"
    p.mkdir(parents=True, exist_ok=True)
    return p / "actions.jsonl"


def _record(action: str, kind: str, *, decision: str = "allowed",
            extra: Optional[dict] = None) -> None:
    """Append-only audit entry. JSONL so it streams cleanly."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,            # "create" | "update" | "request_approval" | "approve" | "reject" | "publish" | "rollback"
        "kind": kind,                # "agent" | "workflow" | "tool" | "kb" | "guardrail"
        "decision": decision,        # "allowed" | "blocked" | "rejected_by_user"
    }
    if extra:
        entry.update(extra)
    try:
        with _audit_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # never let audit failure block the action


def _extract_response_id(response: Any, kind: str) -> Optional[str]:
    """AAVA create responses nest the new id differently per kind.

    Observed shapes:
      agent     → {data: {agentId: N, agentDetails: {...}}, status: "SUCCESS"}
      workflow  → {data: {workFlowId: N, ...}, status}
      tool      → {data: {toolId: N, ...}, status}
      kb        → {data: {collectionId: "...", ...}, status}
      guardrail → {data: {guardrailId: N, ...}, status}

    Top-level `id` rarely exists. Walk the common nested paths and return the
    first match — fixes the prior bug where audit log response_id was always null.
    """
    if not isinstance(response, dict):
        return None
    if response.get("id") is not None:
        return str(response["id"])
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    kind_keys = {
        "agent":     ("agentId", "id"),
        "workflow":  ("workFlowId", "workflowId", "id"),
        "tool":      ("toolId", "id"),
        "kb":        ("collectionId", "kbId", "id"),
        "guardrail": ("guardrailId", "id"),
    }
    for key in kind_keys.get(kind, ("id",)):
        v = data.get(key)
        if v is not None:
            return str(v)
    return None


# ── public API ───────────────────────────────────────────────────────

def gated_create(transport, body: dict, kind: str, *,
                 files=None, context: Optional[dict] = None) -> dict:
    """Write a new artifact to AAVA via transport, then append an audit entry.

    No assessor call. Authoring skills are responsible for guiding the user
    using the framework / guidelines / cache. The formal assessor runs only
    at lifecycle pre-flight (`pre_review_assessment`) or on explicit demand.

    `files` is forwarded to the transport create method as a keyword arg when
    provided — KB create requires a file part (list of (filename, data,
    content_type) tuples); other kinds ignore it.

    Returns the transport's response on success. Raises whatever the
    transport raises on failure.
    """
    method_name = f"create_{kind}"
    create_method = getattr(transport, method_name, None)
    if create_method is None:
        raise AttributeError(f"Transport has no method {method_name!r}")

    response = create_method(body, files=files) if files is not None else create_method(body)
    _record("create", kind,
            extra={"response_id": _extract_response_id(response, kind)})
    return response


def gated_update(transport, entity_id: str, body: dict, kind: str, *,
                 context: Optional[dict] = None) -> dict:
    """Update an existing artifact via transport, then append an audit entry.

    Same philosophy as gated_create — no assessor here. Skills handle the
    "what to change" decision with framework/guidelines/cache as guide.
    """
    update_method = getattr(transport, f"update_{kind}", None)
    if update_method is None:
        raise AttributeError(f"Transport has no method update_{kind!r}")

    response = update_method(entity_id, body)
    _record("update", kind,
            extra={"entity_id": entity_id,
                   "response_id": _extract_response_id(response, kind)})
    return response


def pre_review_assessment(body: dict, kind: str, *,
                          context: Optional[dict] = None) -> dict:
    """Run the formal assessor before a DRAFT → IN_REVIEW transition.

    This is **always advisory** — never blocks. The skill flow shows the
    report to the user and asks whether to proceed. Use this from
    `/aava:request-approval` and from explicit `/aava:assess`.

    Returns the scorecard {summary, dimensions, ...}.
    """
    from .assessor_lib import assess_draft
    return assess_draft(body, kind=kind, context=context)


# ── lifecycle helpers (called from bins) ─────────────────────────────

def record_lifecycle_action(action: str, kind: str, entity_id: str, *,
                            decision: str = "allowed",
                            extra: Optional[dict] = None) -> None:
    """Audit-log helper for lifecycle bins. The bin handles the dry-run /
    --yes gate; this just records the result.

    `action`: 'request_approval' | 'approve' | 'reject' | 'publish' | 'rollback'
    `decision`: 'allowed' (transitioned) | 'dry_run' (printed-only) |
                'rejected_by_user' (user said no) | 'blocked' (RBAC etc.)
    """
    payload = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    _record(action, kind, decision=decision, extra=payload)


def read_audit_log(*, limit: int = 50, since: Optional[str] = None) -> list:
    """Read recent audit entries. `since` is an ISO timestamp string."""
    p = _audit_path()
    if not p.exists():
        return []
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and e.get("ts", "") < since:
            continue
        entries.append(e)
    return entries[-limit:]
