"""Transport ABC.

Every plugin skill / agent / hook calls AavaTransport — never REST or MCP directly.
P1–P10 ship the REST impl. P11 adds MCP impl behind the same interface.
The swap is a config change, not a rewrite.
"""

from __future__ import annotations  # PEP 563: Python 3.9 compat for `str | None`, `list[dict]`

from abc import ABC, abstractmethod
from typing import Any


class TransportError(Exception):
    """Base for all transport-layer errors."""


class AuthError(TransportError):
    """401 / token expired / token missing."""


class DLPMaskedError(TransportError):
    """Response contained DLP-masked content the caller cannot bypass."""


class AavaTransport(ABC):
    """Abstract transport. REST today, MCP-via-Gateway tomorrow."""

    # ── Identity / session ───────────────────────────────────────────

    @abstractmethod
    def whoami(self) -> dict:
        """GET /api/auth/user/details/v2 keyed by current user email (email/teamId/practices).
        RBAC roles + hierarchy come from GET /api/auth/v3/hierarchy/my-access (see lib/persona).
        Cached per session by the persona router.
        """

    @abstractmethod
    def ping(self) -> dict:
        """Cheap reachability check. Used by P0 smoke test."""

    # ── Discovery ────────────────────────────────────────────────────

    @abstractmethod
    def list_workflows(self) -> list[dict]: ...

    @abstractmethod
    def get_workflow(self, workflow_id: str) -> dict: ...

    @abstractmethod
    def list_agents(self) -> list[dict]: ...

    @abstractmethod
    def list_tools(self) -> list[dict]: ...

    @abstractmethod
    def list_kbs(self) -> list[dict]: ...

    @abstractmethod
    def list_guardrails(self) -> list[dict]: ...

    @abstractmethod
    def list_models(self) -> list[dict]: ...

    @abstractmethod
    def action_user_cards(self) -> dict:
        """GET /dashboard/action/user-cards.
        Returns the user's Action Zone dashboard, including metrics, review
        submissions, revisions pending, drafts, and creations.
        """

    # ── Authoring (writes) ───────────────────────────────────────────

    @abstractmethod
    def create_agent(self, body: dict) -> dict: ...

    @abstractmethod
    def update_agent(self, agent_id: str, body: dict) -> dict:
        """PUT /agents (no path id, no query param). Body MUST set both `id` and `agentId`,
        and include full refs (tools, kbIds, agentConfigs.guardrailIds) — missing any
        ref silently wipes it server-side. Only valid on DRAFT/REJECTED agents;
        APPROVED is immutable, use clone_agent() to create an editable DRAFT.
        """

    @abstractmethod
    def clone_agent(self, agent_id: str, *, new_name: str | None = None) -> dict:
        """POST /agents/clone — sanctioned path to "edit" an APPROVED agent.
        Per AAVA_PLATFORM_KNOWLEDGE_REFERENCE.md:192 (verified 2026-05-01).
        Returns a new DRAFT agent with a new id; clone is editable via update_agent.
        """

    @abstractmethod
    def create_workflow(self, body: dict) -> dict: ...

    @abstractmethod
    def create_tool(self, body: dict) -> dict: ...

    @abstractmethod
    def update_tool(self, tool_id: str, body: dict) -> dict:
        """MUST include full toolConfig (image, tool_class_def, tool_class_name).
        A partial PUT WIPES tool_class_def (silent). See tool-authoring contract Rule 8.
        """

    @abstractmethod
    def create_kb(self, body: dict) -> dict:
        """POST /embedding/knowledge/v2 — multipart/form-data, token-scoped (x-realm-id retired).
        Despite the list path being /embedding/knowledge/v2/collections, the create
        endpoint is the parent /embedding/knowledge/v2 (no /collections suffix).
        Body fields are sent as form fields (JSON-stringified for nested values).
        """

    # ── Lifecycle (P6 — exact AAVA endpoints TBD; stubs in REST impl) ──

    @abstractmethod
    def get_artifact(self, kind: str, entity_id: str) -> dict:
        """Fetch full body of any artifact (agent/workflow/tool/kb/guardrail)
        for diff/lifecycle skills. AAVA has different GET shapes per kind;
        this method abstracts the differences. Returns {} if 404 / unsupported.
        """

    @abstractmethod
    def request_approval(self, kind: str, entity_id: str, *, body: dict | None = None, rationale: str = "") -> dict:
        """Submit for review (DRAFT/REJECTED → IN_REVIEW) via the dedicated /IN_REVIEW endpoint.
        Needs only the id; `body` is optional and used only for a best-effort APPROVED guard."""

    @abstractmethod
    def approve(self, kind: str, entity_id: str, *, body: dict | None = None) -> dict:
        """Transition IN_REVIEW → APPROVED via the dedicated /approval endpoint. RBAC-gated.
        Needs only the id (+ comments); `body` is optional."""

    @abstractmethod
    def reject(self, kind: str, entity_id: str, *, body: dict | None = None, reason: str = "") -> dict:
        """Transition IN_REVIEW → REJECTED via the dedicated /approval endpoint. Needs only the id.
        REJECTED artifacts can be edited again via update_agent/update_tool."""

    @abstractmethod
    def publish(self, kind: str, entity_id: str, *, body: dict | None = None) -> dict:
        """In AAVA today, APPROVED IS the active state — there is no separate PUBLISHED.
        This is an alias for approve() preserved for governance-flow naming."""

    @abstractmethod
    def rollback(self, kind: str, entity_id: str, *, to_version: str | None = None) -> dict:
        """Roll back to a previous APPROVED version. AAVA semantics TBD."""

    # ── Execution ────────────────────────────────────────────────────

    @abstractmethod
    def execute_workflow(self, pipeline_id: str, user_inputs: dict) -> dict:
        """POST /workflows/workflow-executions — multipart/form-data.
        Field name is `pipelineId`, NOT workflowId. `userInputs` is a JSON string.
        """

    @abstractmethod
    def execute_agent(self, agent_id: str, user_inputs: dict, *,
                      user_email: str | None = None, execution_id: str | None = None) -> dict:
        """POST /agents/execute — JSON body with agentId + executionId (UUID) + user (email)
        + userInputs. Field name is `userInputs`, NOT `inputs`."""

    @abstractmethod
    def poll_run(self, run_id: str) -> dict:
        """Poll execution at GET /workflows/workflow-executions/{run_id}/result.
        Caller MUST dual-status check:
        top-level == "SUCCESS" AND data.status != "IN_PROGRESS".
        """
