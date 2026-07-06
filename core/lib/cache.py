"""Discovery cache — persisted AAVA snapshots with TTL.

Every read-side skill reads the cache first; on miss or stale, refreshes by calling
the transport. Cache survives plugin upgrades because it lives in CLAUDE_PLUGIN_DATA.

Snapshot shape:
    {
      "meta": {
        "scope_key": "32",
        "scraped_at": "2026-05-06T10:32:00Z",
        "expires_at": "2026-05-06T11:32:00Z",
        "transport": "rest",
        "version": 1
      },
      "agents":     [...],
      "workflows":  [...],
      "tools":      [...],
      "kbs":        [...],
      "guardrails": [...],
      "models":     [...]
    }
"""

from __future__ import annotations  # Python 3.9 compat

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_TTL_SECONDS = 3600  # 1 hour


def _data_root() -> Path:
    """${CLAUDE_PLUGIN_DATA} when running inside Claude Code; sane fallback otherwise."""
    explicit = os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "aava"


def _ttl() -> int:
    raw = (os.environ.get("AAVA_CACHE_TTL_SECONDS")
           or os.environ.get("CLAUDE_PLUGIN_OPTION_AAVACACHETTLSECONDS"))
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_TTL_SECONDS


class Cache:
    """Per-scope JSON snapshot cache. One file per scope key. TTL-gated."""

    def __init__(self, root: Optional[Path] = None, ttl_seconds: Optional[int] = None):
        self.root = (root or _data_root()) / "cache"
        self.ttl = ttl_seconds if ttl_seconds is not None else _ttl()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── path / state ─────────────────────────────────────────────────

    def path_for(self, scope_key: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in str(scope_key))
        return self.root / f"snapshot-{safe}.json"

    def get(self, scope_key: str) -> Optional[dict]:
        """Return snapshot if present and fresh; None otherwise."""
        p = self.path_for(scope_key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if self.is_stale(data):
            return None
        return data

    def is_stale(self, snapshot: dict) -> bool:
        meta = snapshot.get("meta") or {}
        scraped_at = meta.get("scraped_at")
        if not scraped_at:
            return True
        try:
            ts = datetime.fromisoformat(scraped_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age >= self.ttl

    def put(self, scope_key: str, sections: dict, *, transport_name: str = "rest") -> dict:
        """Write a snapshot with fresh meta. `sections` has entity-type keys."""
        now = datetime.now(timezone.utc)
        snapshot = {
            "meta": {
                "scope_key": str(scope_key),
                "scraped_at": now.isoformat(timespec="seconds"),
                "expires_at": (now.timestamp() + self.ttl),
                "transport": transport_name,
                "version": 1,
            },
            **sections,
        }
        p = self.path_for(scope_key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp.replace(p)  # atomic on POSIX
        return snapshot

    def invalidate(self, scope_key: Optional[str] = None) -> int:
        """Drop a single scope's cache, or all if scope_key is None. Returns files removed."""
        removed = 0
        if scope_key is not None:
            p = self.path_for(scope_key)
            if p.exists():
                p.unlink()
                removed = 1
            return removed
        for p in self.root.glob("snapshot-*.json"):
            p.unlink()
            removed += 1
        return removed

    # ── refresh (calls transport) ────────────────────────────────────

    def refresh(self, scope_key: str, transport) -> dict:
        """Pull all entity types via transport, write snapshot, return it.

        Failures on individual entity types degrade gracefully — the snapshot stores
        what it could fetch and tags errors in meta.errors[].
        """
        sections: dict[str, Any] = {}
        errors: list[dict] = []

        calls = [
            ("agents",     transport.list_agents),
            ("workflows",  transport.list_workflows),
            ("tools",      transport.list_tools),
            ("kbs",        transport.list_kbs),
            ("guardrails", transport.list_guardrails),
            ("models",     transport.list_models),
        ]
        for key, fn in calls:
            try:
                sections[key] = fn() or []
            except Exception as e:
                sections[key] = []
                errors.append({"section": key, "error": f"{type(e).__name__}: {e}"})

        snapshot = self.put(scope_key, sections, transport_name=getattr(transport, "name", "rest"))
        if errors:
            snapshot["meta"]["errors"] = errors
            # Re-write so the errors land on disk too.
            self.path_for(scope_key).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return snapshot

    def get_or_refresh(self, scope_key: str, transport, *, force: bool = False) -> dict:
        """Read cache; refresh on miss / stale / force."""
        if not force:
            cached = self.get(scope_key)
            if cached is not None:
                return cached
        return self.refresh(scope_key, transport)

    # ── derived views ────────────────────────────────────────────────

    @staticmethod
    def counts(snapshot: dict) -> dict:
        """Lightweight summary for status display."""
        return {
            section: len(snapshot.get(section, []) or [])
            for section in ("agents", "workflows", "tools", "kbs", "guardrails", "models")
        }
