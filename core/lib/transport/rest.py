"""REST transport — stdlib only.

No requests, no httpx, no certifi. Lifted from scraper.py + aava_client.py patterns.
This is the only transport impl in P0–P10. MCP impl (P11) goes alongside this file.

Token resolution priority:
  1. ${user_config.aava-token}            (Claude Code plugin user config)
  2. AAVA_ACCESS_TOKEN env var            (set by Docker/K8s)
  3. macOS Keychain `aava-mcp/access_token`
  4. /tmp/aava_token.txt                  (dev fallback)
"""

from __future__ import annotations  # Python 3.9 compat

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .base import AavaTransport, AuthError, TransportError


def _resolve_token() -> str:
    # 1. user config (passed by Claude Code as env var with CLAUDE_PLUGIN_OPTION_ prefix)
    for var in ("AAVA_TOKEN", "AAVA_ACCESS_TOKEN", "CLAUDE_PLUGIN_OPTION_AAVA_TOKEN"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    # 2. macOS Keychain
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "aava-mcp", "-a", "access_token", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # 3. /tmp fallback
    try:
        with open("/tmp/aava_token.txt", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    raise AuthError(
        "No AAVA token found. Set CLAUDE_PLUGIN_OPTION_AAVA_TOKEN, AAVA_TOKEN, or store in keychain "
        "(security add-generic-password -s aava-mcp -a access_token -w '<token>' -U)."
    )


class RestTransport(AavaTransport):
    """REST/JSON over stdlib urllib. Multipart for workflow execute."""

    def __init__(self,
                 base_url: str | None = None,
                 timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("AAVA_BASE_URL")
                         or os.environ.get("CLAUDE_PLUGIN_OPTION_AAVA_BASE_URL") or "https://int-ai.aava.ai").rstrip("/")
        # Scope derived lazily from the JWT realm_id/realmId claim; used only as a local
        # cache-partition key. AAVA scopes every API call by the token's RBAC (2026-06).
        self._realm_id: str | None = None
        self._realm_id_resolved: str | None = None
        # Active hierarchy-node selection (org/domain/project/team) from /api/auth/v3/hierarchy/my-access.
        # When set, attached as query params on scoped reads — mirrors the web client's context
        # builder (roleId / hierarchyLevel / hierarchyEntityId / realmId). None ⇒ pure token scoping.
        self.role_id: str | None = None
        self.hierarchy_level: str | None = None          # ORGANIZATION | TRACK | APPLICATION | TEAM
        self.hierarchy_entity_id: str | None = None
        self.timeout = timeout
        self._token: str | None = None
        self._token_read_at: float = 0.0

    # ── scope (replaces the retired x-realm-id header) ───────────────

    @property
    def realm_id(self) -> str:
        """Token-derived scope / cache key (JWT realm_id claim, else '32')."""
        if self._realm_id is not None:
            return self._realm_id
        if self._realm_id_resolved is None:
            self._realm_id_resolved = self._token_realm_id()
        return self._realm_id_resolved

    @realm_id.setter
    def realm_id(self, value) -> None:
        self._realm_id = None if value is None else str(value)

    def _token_realm_id(self) -> str:
        """Parse the realm_id/realmId claim off the JWT payload. Default '32' — matches the
        web client's getRealmId() fallback. Never raises (best-effort scope key)."""
        try:
            import base64
            payload = self._token_value().split(".")[1]
            payload += "=" * (-len(payload) % 4)  # pad base64url
            claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
            rid = claims.get("realm_id") or claims.get("realmId")
            if rid:
                return str(rid)
        except Exception:
            pass
        return "32"

    @property
    def scope_key(self) -> str:
        """Stable string key for cache/run partitioning: the selected hierarchy node when
        one is active, else the token-derived realm id."""
        return self.hierarchy_entity_id or self.realm_id

    def set_hierarchy(self, *, level=None, entity_id=None, role_id=None) -> None:
        """Select the active hierarchy node. Subsequent scoped reads attach it as query
        params. Pass all-None to clear back to pure token scoping."""
        self.hierarchy_level = level
        self.hierarchy_entity_id = None if entity_id is None else str(entity_id)
        self.role_id = None if role_id is None else str(role_id)

    def _scope_params(self) -> dict:
        """Optional RBAC/hierarchy scope params for the selected node. Empty when no node is
        selected (⇒ backend scopes by the token alone, which is the common CLI case)."""
        if self.hierarchy_entity_id is None:
            return {}
        p = {"hierarchyEntityId": self.hierarchy_entity_id}
        if self.hierarchy_level:
            p["hierarchyLevel"] = self.hierarchy_level
        if self.role_id is not None:
            p["roleId"] = self.role_id
        if self._realm_id is not None:
            p["realmId"] = self._realm_id
        return p

    # ── auth ─────────────────────────────────────────────────────────

    def _token_value(self) -> str:
        if self._token and (time.time() - self._token_read_at) < 3300:
            return self._token
        self._token = _resolve_token()
        self._token_read_at = time.time()
        return self._token

    def _headers(self, *, content_type: str = "application/json") -> dict:
        # x-realm-id is retired (2026-06): the backend scopes by the token's RBAC claims.
        h = {"Authorization": f"Bearer {self._token_value()}", "Accept": "application/json"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    # ── HTTP primitives ──────────────────────────────────────────────

    def _request(self, method: str, path: str, *,
                 body: dict | None = None,
                 query: dict | None = None,
                 retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < retries:
                    self._token = None  # force re-read
                    continue
                if e.code == 429 and attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                body_text = ""
                try: body_text = e.read().decode("utf-8", "replace")
                except Exception: pass
                if e.code == 401:
                    raise AuthError(f"401 on {method} {path}: {body_text[:300]}") from e
                raise TransportError(f"{e.code} on {method} {path}: {body_text[:300]}") from e
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1)
                    continue
                raise TransportError(f"{type(e).__name__} on {method} {path}: {e}") from e
        raise TransportError(f"unreachable: {last_err}")

    def _post_multipart(self, path: str, *, fields: dict, files=None) -> dict:
        """Multipart POST. Hand-encoded — stdlib has no multipart helper.

        Used by workflow execution (text fields only) and KB create (text fields + a
        file part). `files` is a list of (name, filename, content_type, data) tuples;
        `data` may be str or bytes. A list/tuple field value is emitted as repeated
        parts (one per element) — KB create needs this for `goodAt`.
        """
        boundary = f"----aavactl{int(time.time()*1000)}"
        body = bytearray()

        def _w(chunk):
            body.extend(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)

        for name, value in fields.items():
            for v in (value if isinstance(value, (list, tuple)) else [value]):
                _w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{v}\r\n")
        for name, filename, content_type, data in (files or []):
            _w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n")
            _w(f"Content-Type: {content_type}\r\n\r\n")
            _w(data)
            _w("\r\n")
        _w(f"--{boundary}--\r\n")
        body = bytes(body)
        headers = self._headers(content_type=f"multipart/form-data; boundary={boundary}")
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body_text = ""
            try: body_text = e.read().decode("utf-8", "replace")
            except Exception: pass
            raise TransportError(f"{e.code} on POST {path}: {body_text[:400]}") from e

    # ── identity / session ───────────────────────────────────────────

    def whoami(self) -> dict:
        email = os.environ.get("AAVA_USER_EMAIL") or os.environ.get("USER_EMAIL") or ""
        path = "/api/auth/user/details/v2"
        return self._request("GET", path, query={"email": email} if email else None)

    def hierarchy_my_access(self) -> dict:
        """GET /api/auth/v3/hierarchy/my-access — the RBAC source (2026-06).
        Returns {data: {organizations[], tracks[], applications[], teams[]}} where each node
        carries id/name/parent + roleId/roleName + fullHierarchy{organization,domain,project}.
        """
        return self._request("GET", "/api/auth/v3/hierarchy/my-access")

    def ping(self) -> dict:
        # Cheap reachability — list_models is a simple authenticated GET with small payload.
        models = self.list_models()
        if not isinstance(models, list):
            models = []  # defensive — older AAVA shapes return dict; treat as opaque "reachable" signal
        return {"models": models[:3], "model_count": len(models),
                "base_url": self.base_url, "scope": self.scope_key}

    # ── discovery (P1) ───────────────────────────────────────────────

    @staticmethod
    def _extract_list(resp, *key_hints):
        """AAVA wraps list responses as {status: SUCCESS, data: {<resource>: [...], totalNoOfRecords}}.
        Some endpoints return list at top, some at data, some nested under data.<key>. Try them all.
        """
        if isinstance(resp, list):
            return resp
        if not isinstance(resp, dict):
            return []
        for k in key_hints:
            v = resp.get(k)
            if isinstance(v, list):
                return v
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in key_hints:
                v = data.get(k)
                if isinstance(v, list):
                    return v
        return []

    # Listing surfaces (verified live 2026-06-22, token-scoped, no realmId/x-realm-id):
    #   /agents/user        — the user's agents; `status` is OPTIONAL (omit ⇒ all statuses).
    #   /workflows/user     — the user's workflows.
    #   /tools/userTools     — ALL visible tools (catalog, thousands); /tools/userTools/user ⇒ owned-by-me.
    #   /guardrails          — ALL visible guardrails;                /guardrails/user      ⇒ owned-by-me.
    #   /embedding/knowledge/v2/collections — KBs (collections key).
    # Agents/workflows have no reliable "all visible" list, so discover uses the per-user view for
    # those and the broader catalog for tools/guardrails. Catalog lists are page-capped to bound the
    # cache; cross-scope reuse search over the full catalog is a follow-up (unified search endpoint).
    _TOOL_PAGE = 200       # single catalog page; tools number in the thousands platform-wide

    def _paginate_user(self, path, *list_keys, records=100, max_pages=None, extra=None):
        """Page a token-scoped list endpoint to completion (no status filter ⇒ all statuses),
        merging + deduping by id. Hierarchy scope params attached when a node is selected."""
        out, seen = [], set()
        page = 1
        while True:
            q = {"page": page, "records": records, "isDeleted": "false"}
            if extra:
                q.update(extra)
            q.update(self._scope_params())
            resp = self._request("GET", path, query=q)
            items = self._extract_list(resp, *list_keys)
            for it in items:
                key = (str(it.get("id") or it.get("agentId") or it.get("workFlowId")
                           or it.get("toolId") or it.get("collectionId") or it.get("guardrailId"))
                       if isinstance(it, dict) else None)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.append(it)
            if len(items) < records or (max_pages is not None and page >= max_pages):
                break
            page += 1
        return out

    def list_workflows(self):
        return self._paginate_user("/workflows/user", "workFlowDetails", "workflows", "pipelines", "data")

    def get_workflow(self, workflow_id):
        # GET /workflows?workFlowId=N — single-record fetch, token-scoped.
        return self._request("GET", "/workflows", query={"workFlowId": workflow_id})

    def list_agents(self):
        # No `status` ⇒ all statuses (verified: 18 across DRAFT/APPROVED/… vs 5 for status=approved).
        return self._paginate_user("/agents/user", "agentDetails", "agents", "data")

    def list_tools(self):
        # All-visible tool catalog. Items live under `userToolDetails` (verified). Single capped page
        # to bound the cache (thousands platform-wide). Strip the heavy base64 `image` per item.
        items = self._paginate_user("/tools/userTools", "userToolDetails", "userTools", "tools",
                                    "toolDetails", "results", "data",
                                    records=self._TOOL_PAGE, max_pages=1)
        return [{k: v for k, v in it.items() if k != "image"} if isinstance(it, dict) else it
                for it in items]

    def list_kbs(self):
        # GET /embedding/knowledge/v2/collections — token-scoped, no x-realm-id header (2026-06).
        # FLAG: returned 0 under pure token scope and under a selected node in live testing — may be
        # genuinely empty for the test scope, or require different scoping. Confirm with a scope that has KBs.
        q = {"records": 200}
        q.update(self._scope_params())
        resp = self._request("GET", "/embedding/knowledge/v2/collections", query=q)
        return self._extract_list(resp, "collections", "knowledgeBase", "kbs", "data")

    def list_guardrails(self):
        # All-visible guardrails (GET /guardrails). /guardrails/user is the owned-by-me variant.
        resp = self._request("GET", "/guardrails", query=self._scope_params() or None)
        return self._extract_list(resp, "guardrails", "guardrailDetails", "data")

    def list_models(self):
        resp = self._request("GET", "/models")
        return self._extract_list(resp, "models", "data")

    def action_user_cards(self):
        """Launchpad Action Zone for the current user.

        GET /dashboard/action/user-cards — token-scoped, no x-realm-id header (2026-06 HAR).
        """
        return self._request("GET", "/dashboard/action/user-cards")

    # ── authoring (P3+) — minimal stubs land in P3 ───────────────────

    def create_agent(self, body): return self._request("POST", "/agents", body=body)

    def update_agent(self, agent_id, body):
        # Canonical: PUT /agents (NO path id, NO query param). Body MUST contain both
        # `id` and `agentId` set to the same id, plus full refs (tools, kbIds,
        # agentConfigs.guardrailIds). Missing any ref WIPES it on the server side.
        body = dict(body or {})
        body["id"] = body.get("id", agent_id)
        body["agentId"] = body.get("agentId", agent_id)
        return self._request("PUT", "/agents", body=body)

    def clone_agent(self, agent_id, *, new_name=None):
        """Sanctioned path to 'edit' an APPROVED agent: clone to a new DRAFT, edit clone, re-approve.
        Body shape per the deployed web client: {agentId, agentName} (verified live 2026-06-22 —
        the old {sourceAgentId, newName} shape 400s ERR-5001)."""
        body = {"agentId": agent_id}
        if new_name:
            body["agentName"] = new_name
        return self._request("POST", "/agents/clone", body=body)

    def clone_tool(self, tool_id, *, new_name=None):
        """Clone a tool to a new DRAFT. Web client: POST /tools/userTools/clone {toolId, toolName}."""
        body = {"toolId": tool_id}
        if new_name:
            body["toolName"] = new_name
        return self._request("POST", "/tools/userTools/clone", body=body)

    def create_workflow(self, body): return self._request("POST", "/workflows", body=body)
    def create_tool(self, body): return self._request("POST", "/tools/userTools", body=body)

    def update_tool(self, tool_id, body):
        # PUT /tools/userTools with `id` IN THE BODY (no path id) — web client shape (2026-06).
        # MUST include full toolConfig; a partial PUT silently wipes tool_class_def.
        body = dict(body or {})
        body["id"] = body.get("id", tool_id)
        return self._request("PUT", "/tools/userTools", body=body)

    def create_kb(self, body, *, files=None):
        # Canonical KB create: POST /embedding/knowledge/v2 as multipart/form-data (token-scoped,
        # x-realm-id retired 2026-06). The list endpoint is /embedding/knowledge/v2/collections;
        # the create endpoint is the parent /embedding/knowledge/v2 (no /collections suffix).
        # /knowledgeBase returns 405 — older plugin path that never existed on the live API.
        #
        # The endpoint REQUIRES at least one file part under field name `files`, else it returns
        # 400 ERR-1002 "Please select at least one file to upload". `files` here is a list of
        # (filename, data, content_type) tuples; `data` may be str or bytes. Body keys carry the
        # platform field names (knowledgeBase, model-ref, splitSize, practiceArea, goodAt …);
        # a list value (e.g. goodAt) is emitted as repeated form parts by _post_multipart.
        fields = {}
        for k, v in (body or {}).items():
            if v is None:
                continue
            if isinstance(v, dict):
                fields[k] = json.dumps(v)
            elif isinstance(v, (list, tuple)):
                fields[k] = [str(x) for x in v]  # repeated parts, not a JSON-encoded string
            else:
                fields[k] = str(v)
        file_parts = [("files", fn, ct, data) for (fn, data, ct) in (files or [])]
        return self._post_multipart("/embedding/knowledge/v2", fields=fields,
                                    files=file_parts)

    # ── lifecycle (P6) ───────────────────────────────────────────────
    # AAVA moved ALL kinds to dedicated lifecycle endpoints (2026-06), the pattern KBs
    # already used. Confirmed for agents from a HAR and read from the deployed web client
    # for every kind:
    #   request approval (→ IN_REVIEW):
    #     agent     PUT /agents/IN_REVIEW?agent-id=<id>            (empty body)
    #     workflow  PUT /workflows/IN_REVIEW?workflow-id=<id>      (empty body)
    #     tool      PUT /tools/userTools/IN_REVIEW?<param>=<id>    (empty body; param name FLAGGED)
    #     kb        PUT /embedding/knowledge/v2/IN_REVIEW?collection_id=<id>
    #   approve / reject (→ APPROVED / REJECTED):
    #     agent/workflow/tool  PUT <base>/approval  {id, status, comments:{whatWentGood,whatWentWrong,improvements}}
    #     kb                   PUT …/v2/approval     {masterId, status, comment}
    #     guardrail            PUT /guardrails/approval {id, status, comment}
    #
    # These endpoints need only the id (+ comments) — NO full-body PUT, so `body` is now
    # optional. APPROVED is still immutable (clone_agent/clone_tool to edit); when a body is
    # passed we use it for a best-effort state guard, but we no longer require it.

    _KIND_PATHS = {
        "agent": "/agents",
        "workflow": "/workflows",
        "tool": "/tools/userTools",
        "kb": "/embedding/knowledge/v2",
        "guardrail": "/guardrails",
    }

    def _path_for(self, kind: str, entity_id=None) -> str:
        base = self._KIND_PATHS.get(kind)
        if base is None:
            raise ValueError(f"Unknown kind {kind!r}; expected one of {list(self._KIND_PATHS)}")
        return f"{base}/{entity_id}" if entity_id else base

    def get_artifact(self, kind, entity_id):
        # AAVA agent has no GET endpoint per gotcha; workflows have GET /workflows?workFlowId=N
        if kind == "agent":
            # Workaround: list filtered to single agent (still returns from list endpoint)
            # In practice, callers pass full body from local cache
            try:
                items = self.list_agents()
                for a in items:
                    if str(a.get("id") or a.get("agentId")) == str(entity_id):
                        return a
                return {}
            except Exception:
                return {}
        if kind == "workflow":
            return self._request("GET", "/workflows", query={"workFlowId": entity_id})
        try:
            return self._request("GET", self._path_for(kind, entity_id))
        except Exception:
            return {}

    # IN_REVIEW endpoint per kind: (path, query-param-name-for-id). param=None ⇒ no query.
    _IN_REVIEW = {
        "agent":     ("/agents/IN_REVIEW", "agent-id"),
        "workflow":  ("/workflows/IN_REVIEW", "workflow-id"),
        "tool":      ("/tools/userTools/IN_REVIEW", "tool-id"),     # verified live 2026-06-22 (param: tool-id)
        "kb":        ("/embedding/knowledge/v2/IN_REVIEW", "collection_id"),
        "guardrail": ("/guardrails/IN_REVIEW", "guardrail-id"),     # FLAG: unverified (no HAR/JS evidence)
    }

    @staticmethod
    def _id_for_body(entity_id):
        s = str(entity_id)
        return int(s) if s.isdigit() else entity_id

    # Backwards-compat alias (KB body uses masterId).
    _kb_master_id = _id_for_body

    def _guard_not_approved(self, kind, entity_id, body):
        """Best-effort immutability guard. Only fires when a body with a status is supplied;
        the dedicated endpoints don't need the body, so an absent body is no longer an error."""
        if isinstance(body, dict) and (body.get("status") or "").upper() == "APPROVED":
            raise RuntimeError(
                f"{kind} {entity_id} is APPROVED and immutable. Clone to an editable DRAFT "
                "(clone_agent/clone_tool) first, then transition the clone."
            )

    def request_approval(self, kind, entity_id, *, body=None, rationale=""):
        self._guard_not_approved(kind, entity_id, body)
        try:
            path, param = self._IN_REVIEW[kind]
        except KeyError:
            raise ValueError(f"Unknown kind {kind!r}; expected one of {list(self._IN_REVIEW)}")
        query = {param: entity_id} if param else None
        # agent/workflow/tool send an empty JSON object; kb sends no body.
        send_body = None if kind == "kb" else {}
        return self._request("PUT", path, query=query, body=send_body)

    def _decision(self, kind, entity_id, status, *, comment):
        """approve/reject via the dedicated /approval endpoints (body shape varies by kind)."""
        if kind in ("agent", "workflow", "tool"):
            base = {"agent": "/agents", "workflow": "/workflows", "tool": "/tools/userTools"}[kind]
            comments = {
                "whatWentGood": comment if status == "APPROVED" else "",
                "whatWentWrong": "" if status == "APPROVED" else comment,
                "improvements": "",
            }
            return self._request("PUT", f"{base}/approval",
                                 body={"id": self._id_for_body(entity_id), "status": status, "comments": comments})
        if kind == "kb":
            return self._request("PUT", "/embedding/knowledge/v2/approval",
                                 body={"masterId": self._id_for_body(entity_id), "status": status, "comment": comment})
        if kind == "guardrail":
            # Web client: PUT /guardrails/approval {id, status, comment} (singular comment).
            return self._request("PUT", "/guardrails/approval",
                                 body={"id": self._id_for_body(entity_id), "status": status, "comment": comment})
        raise ValueError(f"Unknown kind {kind!r}")

    def approve(self, kind, entity_id, *, body=None):
        self._guard_not_approved(kind, entity_id, body)
        return self._decision(kind, entity_id, "APPROVED", comment="Approved")

    def reject(self, kind, entity_id, *, body=None, reason=""):
        return self._decision(kind, entity_id, "REJECTED", comment=reason or "Rejected")

    def publish(self, kind, entity_id, *, body=None):
        # AAVA uses APPROVED as the active state; PUBLISHED is not a separate transition.
        return self.approve(kind, entity_id, body=body)

    def rollback(self, kind, entity_id, *, to_version=None):
        # AAVA rollback semantics TBD. Stub raises so callers can detect not-yet-implemented.
        raise NotImplementedError(
            f"rollback({kind!r}, {entity_id!r}) — AAVA rollback API not yet documented. "
            f"Workaround: PUT a previous version's body to create a new draft, then approve."
        )

    # ── execution (P5+) ──────────────────────────────────────────────

    def execute_workflow(self, pipeline_id, user_inputs):
        return self._post_multipart("/workflows/workflow-executions", fields={
            "pipelineId": str(pipeline_id),
            "userInputs": json.dumps(user_inputs),
        })

    def execute_agent(self, agent_id, user_inputs, *, user_email=None, execution_id=None):
        """Canonical /agents/execute body shape: agentId + executionId (UUID) + user (email) + userInputs.

        user_email resolution: explicit arg → AAVA_USER_EMAIL env → identity cache → "(unknown)".
        execution_id: caller may supply for idempotency; otherwise a fresh UUID is generated.
        """
        import uuid as _uuid
        body = {
            "agentId": agent_id,
            "executionId": execution_id or str(_uuid.uuid4()),
            "user": user_email or self._resolve_user_email(),
            "userInputs": user_inputs,
        }
        return self._request("POST", "/agents/execute", body=body)

    def _resolve_user_email(self) -> str:
        explicit = os.environ.get("AAVA_USER_EMAIL")
        if explicit:
            return explicit
        # Try the identity cache written by lib/persona.whoami()
        try:
            data_root = Path(os.environ.get("AAVA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
                             or (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
                                 / "aava"))
            cache_file = data_root / "identity.json"
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                email = cached.get("email")
                if email and email != "(unknown)":
                    return email
        except Exception:
            pass
        return "(unknown)"

    def poll_run(self, run_id):
        # Canonical poll URL has /result suffix. Caller dual-status checks:
        # top-level SUCCESS + data.status != IN_PROGRESS.
        return self._request("GET", f"/workflows/workflow-executions/{run_id}/result")
