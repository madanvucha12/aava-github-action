"""Learning loop — accumulates the user's usage patterns, conditions future advice.

Three places memory lands:
  1. Per-project (Claude Code memory) — `~/.claude/projects/<proj>/memory/aava_*.md`
  2. Plugin-global — `${CLAUDE_PLUGIN_DATA}/learning/*.json` (this file's primary surface)
  3. AAVA-side mirror — opt-in via `/aava:learning-publish`

Sources of learning:
  - Audit log (gated_create / gated_update outcomes — score, decision)
  - Run cache (test/exec outcomes — pass/fail, duration)
  - Refinement decisions (what user accepted vs rejected)
  - Q&A history (which framework topics user asks repeatedly)
  - Realm/persona switches (where the user works, in what role)

Output: a compact conditioning string that can be injected into agent
system prompts at session start (UserPromptSubmit hook).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional


def _data_root() -> Path:
    explicit = os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "aava"


def _learning_dir() -> Path:
    p = _data_root() / "learning"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── primitive: append a learning entry ───────────────────────────────

def record(kind: str, payload: dict) -> None:
    """Append a learning entry. Each `kind` lands in its own JSONL file.

    Kinds:
      decision      — gated_create/update outcome (from audit log)
      preference    — model/KB-tier/practiceArea picks the user makes repeatedly
      qa_topic      — what the user asks /aava:explain about
      run           — workflow run outcome
      calibration   — manual user adjustment ("stop warning me about X")
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        **payload,
    }
    f = _learning_dir() / f"{kind}.jsonl"
    with f.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, default=str) + "\n")


# ── primitive: read recent entries of a kind ─────────────────────────

def read(kind: str, *, limit: int = 50, since_days: Optional[int] = None) -> list[dict]:
    f = _learning_dir() / f"{kind}.jsonl"
    if not f.exists():
        return []
    entries = []
    cutoff = None
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff and e.get("ts", "") < cutoff:
            continue
        entries.append(e)
    return entries[-limit:]


# ── ingestion: pull from audit log + run cache ───────────────────────

def ingest_from_audit_log(*, since_days: int = 30) -> int:
    """Pull recent audit entries into the decision learning stream.
    Returns count of new entries recorded.
    """
    try:
        from .governance import read_audit_log
    except Exception:
        return 0

    audit = read_audit_log(limit=500)
    decisions_file = _learning_dir() / "decision.jsonl"
    seen_ts = set()
    if decisions_file.exists():
        for line in decisions_file.read_text(encoding="utf-8").splitlines():
            try:
                seen_ts.add(json.loads(line).get("ts"))
            except json.JSONDecodeError:
                continue

    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")
    new = 0
    for e in audit:
        if e.get("ts", "") < cutoff:
            continue
        if e.get("ts") in seen_ts:
            continue
        record("decision", {
            "action": e.get("action"),
            "kind": e.get("kind"),
            "decision": e.get("decision"),
            "score": e.get("score"),
            "name": (e.get("draft") or {}).get("name"),
        })
        new += 1
    return new


# ── analysis: derive preferences from history ────────────────────────

def derived_preferences() -> dict:
    """Crunch decision/preference history into a preferences dict.

    Looks at recent gated_create outcomes to infer:
      - typical model picks per agent role
      - typical practiceArea per artifact kind
      - calibration: how often the user accepts vs requires fixes at a given score
    """
    decisions = read("decision", limit=200, since_days=60)
    if not decisions:
        return {}

    # Score acceptance — when did user proceed despite a warned gate?
    accepted_below = [e for e in decisions if e.get("decision") == "warned"]
    blocked = [e for e in decisions if e.get("decision") == "blocked"]
    allowed_above = [e for e in decisions if e.get("decision") == "allowed"]

    avg_warned_score = (
        sum(e.get("score") or 0 for e in accepted_below) / len(accepted_below)
        if accepted_below else None
    )

    # Most common kinds authored
    kind_counter = Counter(e.get("kind") for e in decisions if e.get("kind"))
    top_kinds = kind_counter.most_common(3)

    return {
        "decisions_observed": len(decisions),
        "warned_acceptance_count": len(accepted_below),
        "blocked_count": len(blocked),
        "allowed_count": len(allowed_above),
        "avg_warned_score": round(avg_warned_score, 2) if avg_warned_score else None,
        "top_authored_kinds": top_kinds,
        "calibration_signal": (
            f"User accepts warnings at avg score {round(avg_warned_score, 2)} — threshold may be too strict"
            if avg_warned_score and avg_warned_score >= 2.5 else None
        ),
    }


# ── conditioning context for injection ───────────────────────────────

def build_conditioning_context(*, max_chars: int = 800) -> str:
    """Return a compact text block that summarizes what the plugin knows about
    the user's preferences. Injected into agent system prompts at session start.

    Format (markdown):
      ## What the plugin has learned about your usage
      - You typically ...
      - Recent decisions show ...
    """
    prefs = derived_preferences()
    if not prefs.get("decisions_observed"):
        return ""

    lines = ["## Your AAVA plugin usage history"]

    if prefs.get("top_authored_kinds"):
        kinds_str = ", ".join(f"{k} ({c})" for k, c in prefs["top_authored_kinds"] if k)
        lines.append(f"- Most authored: {kinds_str}")

    if prefs.get("warned_acceptance_count"):
        lines.append(
            f"- Accepted {prefs['warned_acceptance_count']} draft(s) below assessor threshold "
            f"(avg score {prefs.get('avg_warned_score')}); {prefs['allowed_count']} above."
        )

    if prefs.get("blocked_count"):
        lines.append(f"- {prefs['blocked_count']} submission(s) blocked by assessor.")

    if prefs.get("calibration_signal"):
        lines.append(f"- Calibration: {prefs['calibration_signal']}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"
    return text


# ── forget / publish / calibrate ─────────────────────────────────────

def forget(topic: str) -> int:
    """Remove learning entries matching a topic substring. Returns count removed.
    Topics are matched against names, kinds, and any string field.
    """
    removed = 0
    for f in _learning_dir().glob("*.jsonl"):
        original = f.read_text(encoding="utf-8")
        kept = []
        for line in original.splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            blob = json.dumps(e).lower()
            if topic.lower() in blob:
                removed += 1
                continue
            kept.append(line)
        if removed > 0:
            f.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


def publish_to_aava(transport, *, scope: str = "user", entry_filter: Optional[dict] = None) -> dict:
    """Promote selected learning entries to an AAVA-side memory KB.

    AAVA's memory-KB endpoint and shape are TBD per the AI-Native framework
    documentation (CrewAI Memory Management). Until that's wired, this stub
    saves the would-publish payload locally for review and surfaces what would
    be sent. Caller can then manually push via AAVA UI / admin API.
    """
    snapshot = {}
    for kind in ("decision", "preference", "calibration"):
        snapshot[kind] = read(kind, limit=200)

    out_path = _learning_dir() / f"publish-stage-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps({"scope": scope, "entries": snapshot}, indent=2, default=str),
                        encoding="utf-8")

    return {
        "staged": str(out_path),
        "entry_count": sum(len(v) for v in snapshot.values()),
        "note": "AAVA memory KB schema TBD — would-publish payload staged for review only",
    }


def calibrate(*, set_threshold: Optional[float] = None) -> dict:
    """Manually adjust calibration. If set_threshold given, records the value used
    by /aava:assess and /aava:request-approval pre-flight to highlight scores
    below the bar. Does NOT gate anything — purely a visual signal.
    """
    if set_threshold is not None:
        record("calibration", {
            "type": "threshold_override",
            "value": set_threshold,
            "rationale": "manual user adjustment via /aava:learning-calibrate",
        })
    return derived_preferences()
