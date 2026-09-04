"""Persona router — maps the current user's AAVA roles to a plugin persona surface,
filters which skills appear in the menu accordingly. Closes gap U1
(platform designed for technical users; non-tech roles need different surfaces).

Identity resolution (called once per session, 2026-06 RBAC model):
  GET /api/auth/user/details/v2?email=<user>        → email / teamId / practices / goodAt
  GET /api/auth/v3/hierarchy/my-access              → RBAC hierarchy + role per node
    {data: {organizations[], tracks[], applications[], teams[]}}, each node carrying
    id/name/parent + roleId/roleName + fullHierarchy{organization,domain,project}.

The numeric "realm" the platform used to scope by is gone from the UX — scope is now
token-derived and the user picks a hierarchy node (org/domain/project/team) instead.
Roles come from the hierarchy nodes (roleName), not a flat realms[]/roles[] list.

Role → persona mapping (defensive: persona only controls which skills *show*; the backend
still RBAC-gates every call, so an unknown role falls back to the fullest surface rather
than hiding features). Known roleName values are mapped exactly; others use a substring
heuristic, then DEFAULT_PERSONA.

Personas as skill-list filters (locked decision: skill files are NOT duplicated
per persona; the router controls visibility):
  Product Lead     → exec / runs / approve / publish / rollback / audit / search / explain / advise / why
  Feature Owner    → author / refine / assess / lifecycle / diff / request-approval / search / explain / advise / why / test / runs
  Agents Engineer  → ALL skills (full Studio)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


PERSONAS = ("product-lead", "feature-owner", "agents-engineer")
DEFAULT_PERSONA = "agents-engineer"


def _data_root() -> Path:
    explicit = os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "aava"


def _identity_cache_path() -> Path:
    return _data_root() / "identity.json"


# ── role → persona inference ─────────────────────────────────────────

ROLE_PERSONA_MAP = {
    # Legacy Secure-MCP-Policy role names (still mapped for back-compat).
    "Platform Super Admin": "product-lead",
    "Tenant Admin": "product-lead",
    "Tenant Developer": "agents-engineer",
    "Tenant End User": "agents-engineer",
    "Security Reviewer": "feature-owner",  # read-only governance subset
    "Read-Only Auditor": "feature-owner",  # audit-only
    "Platform Developer": "agents-engineer",
    # 2026-06 hierarchy roleName values (only "Admin" and "user" observed so far — FLAG:
    # full enumeration unverified; the heuristic + default below cover the rest defensively).
    "Admin": "agents-engineer",            # full control on the node → full Studio surface
    "user": "feature-owner",               # author/request, not approve
}


def _role_name(role: Any) -> str:
    """Extract role name string from string or dict shape (AAVA returns either)."""
    if isinstance(role, str):
        return role
    if isinstance(role, dict):
        return str(role.get("name") or role.get("roleName") or role.get("role") or "")
    return str(role)


def _persona_for_role(name: str) -> Optional[str]:
    """Exact map, then a substring heuristic over the role name. None if no match."""
    if name in ROLE_PERSONA_MAP:
        return ROLE_PERSONA_MAP[name]
    low = name.lower()
    if any(t in low for t in ("admin", "developer", "engineer", "owner")):
        return "agents-engineer"
    if any(t in low for t in ("lead", "manager", "approver", "publisher")):
        return "product-lead"
    if any(t in low for t in ("review", "auditor", "read-only", "readonly", "viewer")):
        return "feature-owner"
    return None


# Privilege ranking — when a user holds several roles across hierarchy nodes, surface the
# fullest one (persona is UX-only; the backend still gates each call).
_PERSONA_RANK = {"product-lead": 0, "feature-owner": 1, "agents-engineer": 2}


def infer_persona(roles: list, override: Optional[str] = None) -> str:
    """Pick a persona surface from a list of AAVA roles (strings or dicts). When multiple
    roles map to different personas, return the fullest (highest-ranked) surface."""
    if override and override in PERSONAS:
        return override
    best = None
    for role in roles or []:
        p = _persona_for_role(_role_name(role))
        if p and (best is None or _PERSONA_RANK[p] > _PERSONA_RANK[best]):
            best = p
    return best or DEFAULT_PERSONA


# ── hierarchy (RBAC nodes) ────────────────────────────────────────────

# my-access groups nodes by bucket; each maps to a hierarchyLevel value used as a scope param.
_BUCKET_LEVEL = {
    "organizations": "ORGANIZATION",
    "tracks": "TRACK",
    "applications": "APPLICATION",
    "teams": "TEAM",
}


def hierarchy_nodes(my_access: Any) -> list[dict]:
    """Flatten /api/auth/v3/hierarchy/my-access into a list of selectable nodes.

    Each node: {level, id, name, roleId, roleName, full_hierarchy, label}.
    """
    data = (my_access.get("data") if isinstance(my_access, dict) else None) or my_access or {}
    nodes: list[dict] = []
    for bucket, level in _BUCKET_LEVEL.items():
        for n in (data.get(bucket) or []):
            if not isinstance(n, dict):
                continue
            fh = n.get("fullHierarchy") or {}
            crumbs = [fh.get(k, {}).get("name") for k in ("organization", "domain", "project")]
            label = " / ".join(c for c in crumbs if c) or n.get("name") or str(n.get("id"))
            nodes.append({
                "level": level,
                "id": n.get("id"),
                "name": n.get("name"),
                "roleId": n.get("roleId"),
                "roleName": n.get("roleName"),
                "full_hierarchy": fh,
                "label": label,
            })
    return nodes


# ── identity (whoami) ────────────────────────────────────────────────

def whoami(transport, *, force_refresh: bool = False, persona_override: Optional[str] = None) -> dict:
    """Resolve the current user's identity once per session, cache, return as dict.

    Returns {email, teamId, roles[], hierarchy[], practices, goodAt, persona, resolved_at}.
    `hierarchy` is the flattened list of RBAC nodes; `roles` are the distinct roleNames across them.
    """
    cache_path = _identity_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            # Cache for the whole session (no TTL); refresh explicitly if needed.
            # Self-heal across the 2026-06 schema bump: a cache written before the RBAC
            # migration has no "hierarchy" key — treat it as stale and re-resolve.
            if "hierarchy" in cached:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    try:
        details = transport.whoami() or {}
    except Exception as e:
        # Identity resolution failed — fall back to default persona; user can override
        details = {"error": f"{type(e).__name__}: {e}"}

    # RBAC hierarchy + roles (2026-06). Optional/best-effort: older transports / offline mode
    # may not have it — fall back gracefully to the user-details payload.
    my_access: dict = {}
    try:
        if hasattr(transport, "hierarchy_my_access"):
            my_access = transport.hierarchy_my_access() or {}
    except Exception as e:
        my_access = {"error": f"{type(e).__name__}: {e}"}

    data = (details.get("data") if isinstance(details, dict) else None) or details or {}
    email = data.get("email") or os.environ.get("AAVA_USER_EMAIL") or "(unknown)"

    nodes = hierarchy_nodes(my_access)
    # Distinct role names across nodes; fall back to any legacy roles[] on the details payload.
    roles = list(dict.fromkeys([n["roleName"] for n in nodes if n.get("roleName")]))
    if not roles:
        legacy = data.get("roles") or []
        roles = legacy if isinstance(legacy, list) else [str(legacy)]

    persona = infer_persona(roles, override=persona_override)

    identity = {
        "email": email,
        "teamId": data.get("teamId"),
        "roles": roles,
        "hierarchy": nodes,
        "active_node": None,  # set by `aava-realm select`; None ⇒ pure token scoping
        "practices": data.get("practices") or [],
        "goodAt": data.get("goodAt") or [],
        "persona": persona,
        "persona_override": persona_override,
        "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw": {"details": details, "my_access": my_access},
    }
    cache_path.write_text(json.dumps(identity, indent=2, default=str), encoding="utf-8")
    return identity


def set_active_node(node: Optional[dict]) -> dict:
    """Persist the selected hierarchy node into the identity cache and re-derive the persona
    from that node's role. Pass None to clear back to token scoping. Returns updated identity."""
    cache_path = _identity_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    identity: dict = {}
    if cache_path.exists():
        try:
            identity = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            identity = {}
    identity["active_node"] = node
    override = identity.get("persona_override")
    role_names = [node["roleName"]] if node and node.get("roleName") else (identity.get("roles") or [])
    identity["persona"] = infer_persona(role_names, override=override)
    cache_path.write_text(json.dumps(identity, indent=2, default=str), encoding="utf-8")
    return identity


def set_persona_override(persona: str) -> None:
    """Force a specific persona surface for this session (overrides inference)."""
    if persona not in PERSONAS:
        raise ValueError(f"persona must be one of {PERSONAS}; got {persona!r}")
    cache_path = _identity_cache_path()
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            data["persona"] = persona
            data["persona_override"] = persona
            cache_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return
        except (json.JSONDecodeError, OSError):
            pass
    # No prior identity — write a stub
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"persona": persona, "persona_override": persona}, indent=2),
                          encoding="utf-8")


# ── skill visibility filter ──────────────────────────────────────────

# Skills visible per persona. Skill names match SKILL.md frontmatter `name` fields.
PERSONA_SKILLS = {
    "product-lead": {
        # Run + approve. Read-mostly. Authoring is for Feature Owner / Agents Engineer.
        "explain", "advise", "why",                        # expert
        "discover", "search", "exec", "test", "runs", "replay", "diff-runs",  # execution
        "lifecycle", "audit", "approve", "publish", "rollback",  # governance read + approve
        "realm", "promote",                                # realm
    },
    "feature-owner": {
        # Design + review within tenant. Can author drafts, request approval, but not approve.
        "explain", "advise", "why",
        "discover", "search", "test", "exec", "runs", "replay", "diff-runs",
        "author agent", "author workflow", "author kb", "author guardrail",
        "refine", "optimize-prompt", "assess",
        "lifecycle", "diff", "request-approval", "audit", "gitsync",
        "realm",
    },
    "agents-engineer": {
        # Full Studio. All skills.
        "explain", "advise", "why",
        "discover", "search", "exec", "test", "runs", "replay", "diff-runs",
        "author agent", "author workflow", "author tool", "author kb", "author guardrail",
        "refine", "migrate-secrets", "optimize-prompt", "assess",
        "lifecycle", "diff", "request-approval", "approve", "publish", "rollback", "audit", "gitsync",
        "realm", "promote", "provision-squad",
        "learning show", "learning forget", "learning publish", "learning calibrate",
    },
}


def visible_skills(persona: Optional[str] = None) -> set[str]:
    """Return the set of skill names visible to the current persona surface."""
    if persona is None:
        cache_path = _identity_cache_path()
        if cache_path.exists():
            try:
                identity = json.loads(cache_path.read_text(encoding="utf-8"))
                persona = identity.get("persona") or DEFAULT_PERSONA
            except (json.JSONDecodeError, OSError):
                persona = DEFAULT_PERSONA
        else:
            persona = DEFAULT_PERSONA
    return PERSONA_SKILLS.get(persona) or PERSONA_SKILLS[DEFAULT_PERSONA]


def is_visible(skill_name: str, persona: Optional[str] = None) -> bool:
    return skill_name in visible_skills(persona)
