"""One-way AAVA → Git sync. Closes gap S5 (no GitHub versioning).

Reads the AAVA cache + transport, writes one markdown file per artifact to a
target Git repo, with structured frontmatter and the artifact body. Runs idempotently
— safe to re-run; only modifies files that changed.

Layout in the target repo:
  <target>/scope-<key>/
    agents/<agent-id>-<slug>.md
    workflows/<wf-id>-<slug>.md
    tools/<tool-id>-<slug>.md
    kbs/<kb-id>-<slug>.md
    guardrails/<g-id>-<slug>.md

Each file has YAML frontmatter (id, name, status, scraped_at, ...) and the
canonical JSON body. The user's normal git workflow (commit, branch, PR) handles
the rest. The plugin doesn't run git commands automatically — surface 'now run
git status / commit' as the next step.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _slug(name: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", str(name or "")).strip().lower()
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len] or "unnamed"


def _frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, (list, dict)):
            v = json.dumps(v)
        else:
            v = str(v).replace("\n", " ")
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _render_artifact_md(kind: str, item: dict, scope_key: str, scraped_at: str) -> tuple[str, str]:
    """Return (filename_relative, file_content)."""
    eid = str(item.get("id") or item.get("agentId") or item.get("workFlowId")
              or item.get("toolId") or item.get("kbId") or item.get("guardrailId") or "0")
    name = (item.get("agentName") or item.get("workFlowName") or item.get("toolName")
            or item.get("collectionName") or item.get("guardrailName") or item.get("name") or "unnamed")
    status = item.get("status") or item.get("approvalStatus") or "?"
    description_preview = (
        (item.get("description") or item.get("toolDescription") or "")[:300]
    ).replace("\n", " ")

    fm = {
        "id": eid,
        "kind": kind,
        "name": name,
        "status": status,
        "scope_key": scope_key,
        "scraped_at": scraped_at,
    }
    # Optional metadata if present
    for opt in ("modelId", "tags", "practiceArea", "createdBy", "updatedAt"):
        if opt in item:
            fm[opt] = item[opt]

    body = json.dumps(item, indent=2, default=str, sort_keys=True)
    md = (
        f"{_frontmatter(fm)}\n\n"
        f"# {name}\n\n"
        f"_id: `{eid}` · status: `{status}`_\n\n"
        f"**Preview:** {description_preview}\n\n"
        "## Canonical body\n\n"
        f"```json\n{body}\n```\n"
    )
    return f"{_slug(eid)}-{_slug(name)}.md", md


def sync_to_git(target_dir: Path, snapshot: dict, *, scope_key: Optional[str] = None,
                 dry_run: bool = False) -> dict:
    """Write the AAVA snapshot as markdown files in a Git repo layout.

    Args:
      target_dir: directory under (or equal to) the user's Git repo
      snapshot: a plugin cache snapshot
      scope_key: override scope key (otherwise read from snapshot.meta.scope_key)
      dry_run: if True, return what WOULD be written without writing

    Returns:
      {"written": N, "unchanged": N, "removed": N, "files": [paths]}
    """
    scope_key = scope_key or snapshot.get("meta", {}).get("scope_key") or "?"
    scraped_at = snapshot.get("meta", {}).get("scraped_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    base = Path(target_dir) / f"scope-{_slug(scope_key)}"
    written, unchanged = 0, 0
    files = []

    for kind, plural in (("agent", "agents"), ("workflow", "workflows"),
                          ("tool", "tools"), ("kb", "kbs"), ("guardrail", "guardrails")):
        items = snapshot.get(plural) or []
        kind_dir = base / plural
        if not dry_run:
            kind_dir.mkdir(parents=True, exist_ok=True)

        for item in items:
            if not isinstance(item, dict):
                continue
            fname, content = _render_artifact_md(kind, item, str(scope_key), str(scraped_at))
            target = kind_dir / fname
            files.append(str(target))

            if dry_run:
                continue
            if target.exists() and target.read_text(encoding="utf-8") == content:
                unchanged += 1
            else:
                target.write_text(content, encoding="utf-8")
                written += 1

    return {"written": written, "unchanged": unchanged, "files": files,
             "target": str(base), "dry_run": dry_run}
