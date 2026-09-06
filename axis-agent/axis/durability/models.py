"""Serializable Temporal-only inputs, signals, projections, and lease state."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from axis.approvals import ApprovalRequest
from axis.effects import EffectRecord, TaskIntent
from axis.models import AxisControlState, AxisTaskResult
from axis.planning import AxisPlanItem
from browser_agent_tools import FirewallConfig

AxisJobStatus = Literal[
    "created", "running", "waiting_for_approval", "waiting_for_user", "paused",
    "reconciling", "waiting_for_browser", "completed", "failed", "cancelled",
]


def derive_effect_id(job_id: str, effect_key: str, plan_task_id: str, mutation_ordinal: int) -> str:
    raw = f"{job_id}:{effect_key}:{plan_task_id}:{mutation_ordinal}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


DurableEffectRecord = EffectRecord
DurableApproval = ApprovalRequest


class DurableQuestion(BaseModel):
    request_id: str = Field(max_length=64)
    question: str = Field(min_length=1, max_length=500)
    answer_nonce: Optional[str] = Field(default=None, max_length=64)
    answered: bool = False


class BrowserRecoveryProjection(BaseModel):
    durable_tab_key: Optional[str] = Field(default=None, max_length=80)
    last_known_sanitized_url: Optional[str] = Field(default=None, max_length=2000)
    last_known_title: Optional[str] = Field(default=None, max_length=500)
    active_tab_intent: bool = False
    binding_generation: int = Field(default=0, ge=0)
    observation_generation: int = Field(default=0, ge=0)
    rebind_required: bool = False
    fresh_observation: bool = False

    def invalidated(self) -> "BrowserRecoveryProjection":
        return self.model_copy(update={
            "durable_tab_key": None, "fresh_observation": False, "rebind_required": True,
            "binding_generation": self.binding_generation + 1,
            "observation_generation": self.observation_generation + 1,
        })


class RebindCandidate(BaseModel):
    """One bounded, opaque candidate tab offered during ambiguous rebind.

    ``candidate_id`` is derived (never the raw Chrome/bridge tab id) and is
    only meaningful against the browser-tools cache entry produced by the
    refresh that offered it — a candidate from an earlier refresh cannot be
    replayed against a newer one (see
    ``axis.durability.activities.select_browser_rebind_candidate``)."""

    candidate_id: str = Field(min_length=1, max_length=32)
    sanitized_url: Optional[str] = Field(default=None, max_length=2000)
    title: Optional[str] = Field(default=None, max_length=500)
    active: bool = False


class BrowserRebindResult(BaseModel):
    """Result of a rebind refresh or candidate selection attempt: the
    updated projection plus, only when still ambiguous, the bounded set of
    opaque candidates a human can choose from."""

    projection: "BrowserRecoveryProjection"
    candidates: List[RebindCandidate] = Field(default_factory=list, max_length=10)


class AxisVersionManifest(BaseModel):
    agent_version: str
    pydantic_ai_version: str
    harness_version: str
    temporal_sdk_version: str
    browser_contract_version: str
    browser_contract_hash: str
    prompt_version: str
    policy_version: str
    config_hash: str
    result_schema_version: str
    workflow_schema_version: str

    def is_compatible_with(self, other: "AxisVersionManifest") -> bool:
        """Two manifests are compatible when every pinned dependency and
        contract surface that could change wire format, tool schemas, or
        control-flow semantics matches exactly. `agent_version` alone (a
        build/release label, not a pinned dependency or contract) is
        deliberately excluded: a same-behavior rebuild must not be treated
        as incompatible with itself."""
        return all(getattr(self, field) == getattr(other, field) for field in (
            "pydantic_ai_version", "harness_version", "temporal_sdk_version",
            "browser_contract_version", "browser_contract_hash", "policy_version",
            "prompt_version", "config_hash", "result_schema_version", "workflow_schema_version",
        ))


class DurableRuntimeSettings(BaseModel):
    leases_enabled: bool = True
    lease_conflict_policy: Literal["wait", "fail"] = "wait"
    lease_acquire_timeout_seconds: float = Field(default=30, gt=0)
    lease_duration_seconds: float = Field(default=30, gt=0)
    max_run_segments: int = Field(default=100, gt=0)
    reconcile_unknown_effects: bool = True


class WorkerRuntimeInfo(BaseModel):
    manifest: AxisVersionManifest
    settings: DurableRuntimeSettings


class LeaseProof(BaseModel):
    token: str
    generation: int = Field(ge=1)
    expires_at: float


class AxisDurableDeps(BaseModel):
    run_id: str
    job_id: str
    browser_scope_key: str
    allow_response_body: bool = False
    max_open_tabs: int = 30
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    initial_tab_handle: Optional[str] = None
    control_state: AxisControlState = Field(default_factory=AxisControlState)
    browser_projection: BrowserRecoveryProjection = Field(default_factory=BrowserRecoveryProjection)
    lease_proof: Optional[LeaseProof] = None
    leases_enabled: bool = True
    lease_duration_seconds: float = Field(default=30, gt=0)
    reconciling_effect_id: Optional[str] = None
    # Stamped by the workflow from the manifest it already validated at the
    # start of the current run segment. Any activity that actually executes
    # a browser handler re-checks this against ITS OWN worker's current
    # manifest (same-process, no extra round trip) before touching the
    # bridge — see axis.durability.runtime.durable_browser_handler — so an
    # activity a differently-versioned worker happens to pick up refuses
    # rather than silently running under mismatched assumptions.
    expected_manifest: Optional["AxisVersionManifest"] = None

    @property
    def task_intent(self) -> Optional[TaskIntent]:
        return self.control_state.task_intent

    @property
    def effects(self) -> List[EffectRecord]:
        return self.control_state.effects

    @property
    def plan(self) -> List[AxisPlanItem]:
        return self.control_state.plan

    @property
    def pending_approval(self) -> Optional[ApprovalRequest]:
        return next((r for r in self.control_state.approval_state.requests_by_tool_call_id.values() if not r.resolved), None)


class AxisJobState(BaseModel):
    job_id: str
    status: AxisJobStatus
    task_summary: str = Field(max_length=500)
    browser_scope_key: str
    plan: List[AxisPlanItem] = Field(default_factory=list)
    plan_revision: int = 0
    task_intent: Optional[TaskIntent] = None
    effects: List[EffectRecord] = Field(default_factory=list)
    acceptance: Dict[str, bool] = Field(default_factory=dict)
    pending_approval: Optional[ApprovalRequest] = None
    pending_question: Optional[DurableQuestion] = None
    paused: bool = False
    cancel_requested: bool = False
    rebind_required: bool = False
    rebind_candidates: List[RebindCandidate] = Field(default_factory=list)
    binding_generation: int = 0
    observation_generation: int = 0
    run_segments: int = 0
    version_manifest: Optional[AxisVersionManifest] = None
    result: Optional[AxisTaskResult] = None
    last_error_code: Optional[str] = None


class AxisContinuationState(BaseModel):
    control_state: AxisControlState
    browser_projection: BrowserRecoveryProjection
    total_run_segments: int = 0
    version_manifest: Optional[AxisVersionManifest] = None
    # Bounded continuation context restored in the first model request after
    # continue_as_new (see AxisJobWorkflow._build_resumed_prompt) — never
    # relied on message history, which continue_as_new intentionally drops.
    original_task_summary: str = Field(default="", max_length=500)
    user_answers: List[str] = Field(default_factory=list, max_length=5)


class AxisJobInput(BaseModel):
    job_id: str
    task_summary: str = Field(min_length=1, max_length=500)
    browser_scope_key: str
    allow_response_body: bool = False
    max_open_tabs: int = 30
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    initial_tab_handle: Optional[str] = None
    continuation: Optional[AxisContinuationState] = None


class SubmitApprovalSignal(BaseModel):
    request_id: str = Field(max_length=64)
    approved: bool
    decision_nonce: str = Field(min_length=1, max_length=64)


class PauseSignal(BaseModel):
    pass


class ResumeSignal(BaseModel):
    pass


class CancelSignal(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


class SubmitUserInputSignal(BaseModel):
    request_id: Optional[str] = Field(default=None, max_length=64)
    answer_nonce: Optional[str] = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=2000)


class RebindBrowserSignal(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


class SelectRebindCandidateSignal(BaseModel):
    """A validated human choice among the opaque candidates most recently
    queryable via ``AxisJobWorkflow.get_rebind_candidates`` — never a raw
    Chrome/bridge identifier."""

    candidate_id: str = Field(min_length=1, max_length=32)


class LeaseOwner(BaseModel):
    job_id: str
    token: str
    generation: int = Field(default=1, ge=1)
    expires_at: float = 32503680000.0


class LeaseAcquireResult(BaseModel):
    acquired: bool
    owner: Optional[LeaseOwner] = None
    reason: Optional[Literal["busy", "stale", "expired"]] = None


class LeaseOperationResult(BaseModel):
    accepted: bool
    owner: Optional[LeaseOwner] = None


class BrowserActivityEnvelope(BaseModel):
    result: Dict[str, Any]
    projection: BrowserRecoveryProjection
    execution_stage: Literal["validated", "authorized", "dispatch_started", "response_received"]
