"""Run cache — per-run artifacts in ${CLAUDE_PLUGIN_DATA}/runs/<run-id>/.

Each run directory contains:
  inputs.json        — the inputs that triggered the run (for replay)
  metadata.json      — workflow_id, scope_key, started_at, completed_at, status, score
  path.mmd           — Mermaid path diagram of the workflow
  report.html        — human-readable run report
  output.json        — final workflow output

Used by:
  /aava:exec       — saves inputs+metadata on dispatch
  /aava:test       — writes path.mmd + report.html as the run progresses
  /aava:runs       — lists past runs from the run dir
  /aava:replay     — reads inputs.json to re-execute
  /aava:diff-runs  — compares output.json from two runs
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _data_root() -> Path:
    explicit = os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "aava"


class Runs:
    """Read/write per-run artifacts. One directory per run, atomic writes."""

    def __init__(self, root: Optional[Path] = None):
        self.root = (root or _data_root()) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def dir_for(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(run_id))
        d = self.root / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_metadata(self, run_id: str, *,
                      workflow_id: str,
                      scope_key: str,
                      inputs: dict,
                      status: str = "STARTED",
                      score: Optional[float] = None,
                      extra: Optional[dict] = None) -> dict:
        d = self.dir_for(run_id)
        meta = {
            "run_id": str(run_id),
            "workflow_id": str(workflow_id),
            "scope_key": str(scope_key),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "score": score,
        }
        if extra:
            meta.update(extra)
        (d / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        (d / "inputs.json").write_text(json.dumps(inputs, indent=2), encoding="utf-8")
        return meta

    def update_metadata(self, run_id: str, **fields) -> dict:
        d = self.dir_for(run_id)
        path = d / "metadata.json"
        meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        meta.update(fields)
        if fields.get("status") in ("COMPLETED", "SUCCESS", "FAILED", "FAILURE"):
            meta.setdefault("completed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def save_output(self, run_id: str, output: Any) -> None:
        d = self.dir_for(run_id)
        (d / "output.json").write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    def save_path_mmd(self, run_id: str, mermaid: str) -> Path:
        d = self.dir_for(run_id)
        p = d / "path.mmd"
        p.write_text(mermaid, encoding="utf-8")
        return p

    def save_report_html(self, run_id: str, html: str) -> Path:
        d = self.dir_for(run_id)
        p = d / "report.html"
        p.write_text(html, encoding="utf-8")
        return p

    def get_metadata(self, run_id: str) -> Optional[dict]:
        d = self.dir_for(run_id)
        path = d / "metadata.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def get_inputs(self, run_id: str) -> Optional[dict]:
        d = self.dir_for(run_id)
        path = d / "inputs.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def get_output(self, run_id: str) -> Optional[Any]:
        d = self.dir_for(run_id)
        path = d / "output.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list_runs(self, *,
                  workflow_id: Optional[str] = None,
                  scope_key: Optional[str] = None,
                  status: Optional[str] = None,
                  limit: int = 50) -> list[dict]:
        """List runs, newest first. Filter by workflow/scope_key/status if provided."""
        entries = []
        for run_dir in self.root.iterdir():
            if not run_dir.is_dir():
                continue
            meta_path = run_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if workflow_id and str(meta.get("workflow_id")) != str(workflow_id):
                continue
            if scope_key and str(meta.get("scope_key")) != str(scope_key):
                continue
            if status and meta.get("status") != status:
                continue
            entries.append(meta)
        entries.sort(key=lambda e: e.get("started_at", ""), reverse=True)
        return entries[:limit]


def render_mermaid_path(workflow_def: dict, run_state: Optional[dict] = None) -> str:
    """Render the workflow as a Mermaid graph; mark step status if run_state provided.

    workflow_def: AAVA workflow definition (with agentDetails array)
    run_state: optional poll_run response — to color steps by completion state
    """
    agents = workflow_def.get("agentDetails") or workflow_def.get("agents") or []
    if isinstance(agents, dict):
        agents = list(agents.values())
    agents = sorted(agents, key=lambda a: a.get("serialOrder", 0))

    # State map: agent_id → status from run_state
    state = {}
    if run_state:
        steps = (run_state.get("data") or {}).get("steps") or run_state.get("steps") or []
        for s in steps:
            aid = str(s.get("agentId") or s.get("agent_id") or "")
            state[aid] = s.get("status") or "PENDING"

    lines = ["graph TD"]
    prev = None
    for i, a in enumerate(agents):
        aid = str(a.get("agentId") or a.get("id") or i)
        name = (a.get("agentName") or a.get("name") or f"Agent {aid}").replace('"', "'")
        status = state.get(aid, "PENDING")
        style = {
            "COMPLETED": ":::done", "SUCCESS": ":::done",
            "IN_PROGRESS": ":::running", "RUNNING": ":::running",
            "FAILED": ":::failed", "FAILURE": ":::failed",
        }.get(status.upper() if isinstance(status, str) else "", "")
        node = f'  A{i}["{name}<br/>({status})"]{style}'
        lines.append(node)
        if prev is not None:
            lines.append(f"  A{prev} --> A{i}")
        prev = i

    lines.append("  classDef done fill:#3fb950,stroke:#0d6e2a,color:#fff")
    lines.append("  classDef running fill:#d29922,stroke:#7a5800,color:#fff")
    lines.append("  classDef failed fill:#f85149,stroke:#9c0707,color:#fff")
    return "\n".join(lines)
