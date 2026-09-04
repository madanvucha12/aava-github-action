"""Thin wrapper over vendor/assessor — plugin-facing API.

Exposes two capabilities the rest of the plugin needs:

1. `assess_snapshot(snapshot)` — runs the existing assessor's analyzer on a cache
   snapshot. Use for post-hoc realm/workflow assessment.

2. `assess_draft(draft, kind)` — NEW capability. Runs the relevant subset of
   dimensions on a single artifact draft (agent or workflow) BEFORE submission.
   This is the capability that shifts assessment from post-hoc to design-time.

Also re-exports formatters (`format_terminal`, `format_markdown`) so callers
don't need to know the vendor module layout.
"""

from __future__ import annotations  # Python 3.9 compat

import os
import sys
from pathlib import Path
from typing import Any, Optional


# ── vendor path injection ────────────────────────────────────────────

_PLUGIN_ROOT = Path(os.environ.get("AAVA_CORE_ROOT")
                    or Path(__file__).resolve().parent.parent)
_VENDOR_ASSESSOR = _PLUGIN_ROOT / "vendor" / "assessor"

if str(_VENDOR_ASSESSOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ASSESSOR))

# Imports happen lazily inside functions so import errors surface with context.


# ── shape adapters ───────────────────────────────────────────────────

def _agents_to_dict(agents):
    """Plugin cache stores agents as a list; analyzer expects {id: agent}."""
    if isinstance(agents, dict):
        return agents
    if not isinstance(agents, list):
        return {}
    out = {}
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            continue
        key = str(a.get("id") or a.get("agentId") or i)
        out[key] = a
    return out


def _tools_to_dict(tools):
    if isinstance(tools, dict):
        return tools
    if not isinstance(tools, list):
        return {}
    out = {}
    for i, t in enumerate(tools):
        if not isinstance(t, dict):
            continue
        key = str(t.get("id") or t.get("toolId") or i)
        out[key] = t
    return out


def _cache_to_analyzer(snapshot: dict) -> dict:
    """Convert a plugin cache snapshot into the shape analyzer.normalize_snapshot expects."""
    return {
        "meta": snapshot.get("meta", {}),
        "workflows": snapshot.get("workflows", []) or [],
        "agents": _agents_to_dict(snapshot.get("agents", [])),
        "tools": _tools_to_dict(snapshot.get("tools", [])),
        "knowledge_bases": snapshot.get("kbs", []) or [],
        "guardrails": snapshot.get("guardrails", []) or [],
    }


# ── public API ───────────────────────────────────────────────────────

def assess_snapshot(snapshot: dict, *, dimensions: Optional[list] = None) -> dict:
    """Run the analyzer over a cache snapshot.

    Args:
      snapshot: a plugin cache snapshot (lib/cache.py shape)
      dimensions: optional list of dimension keys to compute. If None, all run.
                  Useful for fast partial re-checks (e.g., only KB wiring).

    Returns:
      The analyzer's result dict (summary + dimensions[] + recommendations).
    """
    from assessor.analyzer import analyze
    adapted = _cache_to_analyzer(snapshot)
    result = analyze(adapted)
    if dimensions:
        result["dimensions"] = {k: v for k, v in result["dimensions"].items() if k in dimensions}
    return result


# Dimension subsets meaningful at draft time (i.e., before submission).
# Cross-realm dimensions like reusability, executive_summary, config_hygiene need
# more context than a draft has — exclude them by default.
_AGENT_DRAFT_DIMENSIONS = (
    "prompt_hygiene", "kb_wiring", "guardrail_coverage",
    "tool_usage", "agent_design",
)
_WORKFLOW_DRAFT_DIMENSIONS = (
    "decomposition", "orchestration", "model_selection",
    "aqg_readiness", "hitl_design",
)


def assess_draft(draft: dict, kind: str, *, context: Optional[dict] = None) -> dict:
    """Run a focused subset of dimensions on a single not-yet-deployed artifact.

    Design-time scoring. Used by:
      - PreToolUse hook (advisory in P2, blocking in P9)
      - /aava:author-agent stage 8 (pre-publish assessment)
      - /aava:assess --draft <path>

    Args:
      draft: the artifact body about to be created/updated (dict)
      kind:  "agent" or "workflow"
      context: optional cache snapshot; lets the analyzer resolve KB/tool/agent
               refs when scoring the draft. If None, scoring is purely structural.

    Returns:
      Same shape as assess_snapshot but with only the dimensions relevant to
      drafts. Includes a `draft` block summarizing the artifact under review.
    """
    if kind not in ("agent", "workflow"):
        raise ValueError(f"kind must be 'agent' or 'workflow', got {kind!r}")

    # Build a synthetic snapshot. Start from context (so refs resolve), then
    # overlay the draft as the entity under review.
    base = _cache_to_analyzer(context) if context else {
        "meta": {}, "workflows": [], "agents": {}, "tools": {},
        "knowledge_bases": [], "guardrails": [],
    }

    if kind == "agent":
        # Slot the draft in as a single agent. Use a placeholder id to avoid
        # colliding with existing realm agents.
        draft_id = str(draft.get("id") or draft.get("agentId") or "_draft_agent_")
        base["agents"] = {draft_id: draft}
        dims = _AGENT_DRAFT_DIMENSIONS
    else:  # workflow
        # Workflow draft references existing agents — keep context.agents.
        # Replace context.workflows with just this draft.
        base["workflows"] = [draft]
        dims = _WORKFLOW_DRAFT_DIMENSIONS

    from assessor.analyzer import analyze
    result = analyze(base)

    # Filter to draft-relevant dimensions
    result["dimensions"] = {k: v for k, v in result["dimensions"].items() if k in dims}

    # Add a `draft` summary block so the caller can see what was scored
    result["draft"] = {
        "kind": kind,
        "id": str(draft.get("id") or draft.get("agentId") or draft.get("workFlowId") or "(unsaved)"),
        "name": (draft.get("agentName") or draft.get("workFlowName")
                 or draft.get("name") or "(unnamed)"),
        "scored_dimensions": list(dims),
    }

    # Recompute summary score over draft-relevant dimensions only
    scored = [d for d in result["dimensions"].values() if isinstance(d.get("score"), (int, float))]
    if scored:
        avg = sum(d["score"] for d in scored) / len(scored)
        result["summary"]["overall_score"] = round(avg, 2)
        result["summary"]["dimensions_scored"] = len(scored)
        result["summary"]["mode"] = f"draft ({kind})"

    return result


# ── formatters (re-exports) ──────────────────────────────────────────

def format_terminal(result: dict) -> None:
    """Print scorecard to stdout. Passes through to vendor reporter."""
    from assessor.reporter import print_terminal
    print_terminal(result)


def format_markdown(result: dict, *, meta: Optional[dict] = None) -> str:
    from assessor.reporter import generate_markdown
    return generate_markdown(result, snapshot_meta=meta)


def critic_prompt(snapshot: dict, analysis: dict) -> str:
    """Build the LLM expert critic prompt (for /aava:assess --critic)."""
    from assessor.critic import build_critic_prompt
    adapted = _cache_to_analyzer(snapshot)
    return build_critic_prompt(adapted, analysis)
