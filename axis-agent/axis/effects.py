"""Phase 2 risk classification, typed Task Intent / effect models, the
run-local effect ledger, and acceptance matching.

Deliberately framework-agnostic: nothing here imports Pydantic AI. This
module is the single source of truth for what counts as a state-changing
browser call (``requires_effect``), what its non-model-lowerable risk floor
is (``deterministic_risk_floor``), and whether a ``browser_assert`` call
actually satisfies a declared acceptance criterion
(``criterion_matches_assertion``). ``axis.agent``'s ``ToolGuardrail`` and
output validator are the only callers that turn these into control flow;
this file never raises ``ModelRetry``/``ApprovalRequired`` itself.

Two effect-status "views" matter and must not be conflated:

- *Active* (``awaiting_approval``, ``executing``, ``executed_unverified``):
  an effect that has started or is waiting to. Blocks a second concurrent
  prepare, blocks Task Intent revision, blocks silently dropping the plan
  task through ``write_plan``/``remove_task``.
- *Resolved* (``succeeded``, ``cancelled``, ``superseded``): an effect the
  completion gate is satisfied with. ``failed``/``denied`` are neither —
  they are inert (they don't block a retry-by-re-preparing the same key) but
  they are NOT resolved until either a later attempt succeeds or the
  branch is explicitly cancelled.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional
from uuid import uuid4

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic import BaseModel, Field, model_validator  # noqa: E402

from browser_agent_tools import get_tool_definitions  # noqa: E402

# =====================================================================
# Frozen-contract-derived vocabulary (never hand-duplicated)
# =====================================================================

_DEFINITIONS_BY_NAME = {entry["function"]["name"]: entry["function"] for entry in get_tool_definitions()}


def _enum_of(tool_name: str, prop: str) -> List[str]:
    return list(_DEFINITIONS_BY_NAME[tool_name]["parameters"]["properties"][prop]["enum"])


_ACT_ACTIONS = frozenset(_enum_of("browser_act", "action"))
_TABS_OPERATIONS = frozenset(_enum_of("browser_tabs", "operation"))
_ASSERT_TYPES = frozenset(_enum_of("browser_assert", "assertion"))
_ASSERT_PROPERTIES = frozenset(_DEFINITIONS_BY_NAME["browser_assert"]["parameters"]["properties"].keys())

KNOWN_TOOL_NAMES = frozenset(_DEFINITIONS_BY_NAME.keys())

# Raw identifiers a criterion's assertion spec (or a proposed effect) must
# never carry — mirrors tests/test_phase0_baseline.py's/test_phase1_agent.py's
# own FORBIDDEN_INPUT_PROPERTY_NAMES; declared once here so tests reference
# this copy instead of the other way around.
FORBIDDEN_INPUT_PROPERTY_NAMES = frozenset({
    "tabId", "windowId", "groupId", "frameId", "bridgeSessionId",
    "bridgeMethod", "method", "rpcMethod", "snapshotId",
})

# =====================================================================
# Effect-gating vs. deterministic risk floor (kept as two functions —
# "requires a prepared effect" and "how risky is it" are different axes)
# =====================================================================

_EFFECT_GATED_ACT_ACTIONS = frozenset({"click", "fill", "press", "select", "check", "uncheck", "upload", "drag"})
_CONTROL_ACT_ACTIONS = frozenset({"hover", "scroll"})
assert _EFFECT_GATED_ACT_ACTIONS | _CONTROL_ACT_ACTIONS == _ACT_ACTIONS, "browser_act action enum drifted"

_READ_TOOLS = frozenset({"browser_observe", "browser_wait", "browser_assert", "browser_diagnose", "browser_capture_evidence"})
_CONTROL_TOOLS = frozenset({"browser_navigate"})
assert {"list", "create", "activate", "close"} == _TABS_OPERATIONS, "browser_tabs operation enum drifted"

RiskClass = Literal["read", "reversible_local", "external_effect", "destructive_high_impact"]
_RISK_ORDER: Dict[str, int] = {"read": 0, "reversible_local": 1, "external_effect": 2, "destructive_high_impact": 3}


def max_risk(*risks: RiskClass) -> RiskClass:
    """The highest of the given risks. Model-controlled data may only ever
    be one input among several here — this function has no notion of which
    input is "the model's", so a lower model-proposed risk can never win."""
    if not risks:
        raise ValueError("max_risk() requires at least one risk.")
    return max(risks, key=lambda r: _RISK_ORDER[r])


def requires_effect(tool: str, action: Optional[str] = None, operation: Optional[str] = None) -> bool:
    """Whether a call to this tool (with this action/operation) is
    effect-gated: it may only execute with a prepared, matching Effect
    behind it. Everything else (read + control) is unconditionally allowed
    and never consults Task Intent/plan/effect/approval state."""
    if tool == "browser_act":
        return action in _EFFECT_GATED_ACT_ACTIONS
    if tool == "browser_tabs":
        return operation == "close"
    return False


def deterministic_risk_floor(tool: str, action: Optional[str] = None, operation: Optional[str] = None) -> RiskClass:
    """The non-model-lowerable minimum risk for a tool/action/operation
    combination. Descriptive for control/read calls (never gates them);
    load-bearing for effect-gated calls (folded into ``effective_risk``)."""
    if tool in _READ_TOOLS:
        return "read"
    if tool == "browser_navigate":
        return "reversible_local"
    if tool == "browser_tabs":
        if operation in ("list", "create", "activate"):
            return "reversible_local"
        if operation == "close":
            return "external_effect"
        raise ValueError(f"Unknown browser_tabs operation: {operation!r}")
    if tool == "browser_act":
        if action in _CONTROL_ACT_ACTIONS:
            return "read"
        if action in ("fill", "select", "check", "uncheck"):
            return "reversible_local"
        if action in ("click", "press", "drag"):
            return "reversible_local"  # elevated to the effect's own risk by effective_risk
        if action == "upload":
            return "external_effect"
        raise ValueError(f"Unknown browser_act action: {action!r}")
    raise ValueError(f"Unknown browser tool: {tool!r}")


def effective_risk(tool: str, effect_risk: RiskClass, action: Optional[str] = None, operation: Optional[str] = None) -> RiskClass:
    """The risk actually applied to an effect-gated call: the deterministic
    floor for this specific call, raised (never lowered) by the prepared
    effect's own stored risk (itself already ``max(proposed, policy)`` —
    see ``EffectLedger.prepare``)."""
    return max_risk(deterministic_risk_floor(tool, action=action, operation=operation), effect_risk)


# =====================================================================
# Non-idempotent classification (Phase 3 durability: which exact
# tool/action/operation combinations must never be safely repeated once
# dispatch may have started).
# =====================================================================

# Every browser_capture_evidence 'capture' kind is either a trace
# start/stop/export or writes an artifact (screenshot/pdf/snapshot) — none of
# these are safe to blindly repeat, so the whole tool is non-idempotent
# regardless of which 'capture' value is used.
_NON_IDEMPOTENT_WHOLE_TOOLS = frozenset({"browser_navigate", "browser_capture_evidence"})


def is_non_idempotent_operation(tool: str, action: Optional[str] = None, operation: Optional[str] = None) -> bool:
    """Whether this *exact* tool call (tool + action/operation, not merely
    the tool name) must never be automatically repeated once dispatch may
    have started.

    This is the finer-grained classification Temporal's own per-tool
    activity retry policy cannot express by itself (a `Tool`'s Temporal
    `ActivityConfig` is resolved once per tool name, before the model's
    argument values for a specific call are known — see
    ``axis.durability.runtime._activity_metadata`` for the tool-name-level
    fallback this refines, and ``durable_browser_handler`` for where this
    function is consulted with the actual call's action/operation once they
    ARE known, as a same-attempt guard against ever dispatching a second
    attempt of a non-idempotent operation)."""
    if tool == "browser_act":
        return action not in _CONTROL_ACT_ACTIONS
    if tool == "browser_tabs":
        return operation in ("create", "close")
    return tool in _NON_IDEMPOTENT_WHOLE_TOOLS


# =====================================================================
# Typed Task Intent models
# =====================================================================


class AcceptanceCriterion(BaseModel):
    criterion_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=300)
    required: bool = True
    assertion: Dict[str, Any]

    @model_validator(mode="after")
    def _validate_assertion_shape(self) -> "AcceptanceCriterion":
        validate_assertion_spec(self.assertion)
        return self


def validate_assertion_spec(assertion: Dict[str, Any]) -> None:
    """Validate an acceptance criterion's assertion spec against the real
    frozen ``browser_assert`` schema (never a hand-duplicated key list), the
    per-assertion-type conditional requirements that schema documents, and
    the raw-identifier denylist. Raises ``ValueError`` with a precise reason."""
    if "browserSessionId" in assertion:
        raise ValueError("assertion must not include browserSessionId; the runtime supplies the trusted session.")
    forbidden = FORBIDDEN_INPUT_PROPERTY_NAMES & set(assertion)
    if forbidden:
        raise ValueError(f"assertion contains forbidden raw identifier field(s): {sorted(forbidden)}.")
    unknown = set(assertion) - _ASSERT_PROPERTIES
    if unknown:
        raise ValueError(f"assertion has unknown field(s): {sorted(unknown)}.")
    kind = assertion.get("assertion")
    if kind not in _ASSERT_TYPES:
        raise ValueError(f"assertion.assertion must be one of {sorted(_ASSERT_TYPES)}, got {kind!r}.")

    needs_locator = kind not in ("title", "aria_snapshot")
    if needs_locator and "locator" not in assertion:
        raise ValueError(f"assertion type {kind!r} requires 'locator'.")
    if not needs_locator and "locator" in assertion:
        raise ValueError(f"assertion type {kind!r} must not include 'locator'.")

    needs_expected = kind in ("value", "text", "attribute", "title", "aria_snapshot")
    if needs_expected and "expected" not in assertion:
        raise ValueError(f"assertion type {kind!r} requires 'expected'.")
    if not needs_expected and "expected" in assertion:
        raise ValueError(f"assertion type {kind!r} must not include 'expected'.")

    if (kind == "attribute") != ("attribute" in assertion):
        raise ValueError("'attribute' is required for and only valid with assertion type 'attribute'.")
    if (kind == "count") != ("count" in assertion):
        raise ValueError("'count' is required for and only valid with assertion type 'count'.")
    if "contains" in assertion and kind not in ("text", "title"):
        raise ValueError("'contains' is only valid with assertion types 'text'/'title'.")


class ProposedEffect(BaseModel):
    effect_key: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=300)
    risk: RiskClass
    allowed_tools: List[str] = Field(min_length=1, max_length=8)
    allowed_actions: List[str] = Field(default_factory=list, max_length=10)
    max_browser_mutations: int = Field(ge=1, le=20)
    acceptance_criterion_ids: List[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _validate_tools_and_actions(self) -> "ProposedEffect":
        unknown_tools = set(self.allowed_tools) - KNOWN_TOOL_NAMES
        if unknown_tools:
            raise ValueError(f"Unsupported browser tool(s): {sorted(unknown_tools)}.")
        unknown_actions = set(self.allowed_actions) - (_ACT_ACTIONS | _TABS_OPERATIONS)
        if unknown_actions:
            raise ValueError(f"Unsupported browser action(s): {sorted(unknown_actions)}.")
        return self


class TaskAmbiguity(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    blocking: bool = True


class TaskIntent(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    constraints: List[str] = Field(default_factory=list, max_length=10)
    ambiguities: List[TaskAmbiguity] = Field(default_factory=list, max_length=10)
    proposed_effects: List[ProposedEffect] = Field(default_factory=list, max_length=10)
    acceptance_criteria: List[AcceptanceCriterion] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_cross_references(self) -> "TaskIntent":
        for constraint in self.constraints:
            if not (1 <= len(constraint) <= 300):
                raise ValueError("Each constraint must be 1-300 characters.")
        criterion_ids = [c.criterion_id for c in self.acceptance_criteria]
        duplicate_criteria = sorted({c for c in criterion_ids if criterion_ids.count(c) > 1})
        if duplicate_criteria:
            raise ValueError(f"Duplicate acceptance_criterion_id(s): {duplicate_criteria}.")
        effect_keys = [e.effect_key for e in self.proposed_effects]
        duplicate_effects = sorted({k for k in effect_keys if effect_keys.count(k) > 1})
        if duplicate_effects:
            raise ValueError(f"Duplicate effect_key(s): {duplicate_effects}.")
        known_criteria = set(criterion_ids)
        for effect in self.proposed_effects:
            unknown_refs = set(effect.acceptance_criterion_ids) - known_criteria
            if unknown_refs:
                raise ValueError(f"Effect {effect.effect_key!r} references unknown criterion id(s): {sorted(unknown_refs)}.")
            required_refs = [
                cid for cid in effect.acceptance_criterion_ids
                if next(c for c in self.acceptance_criteria if c.criterion_id == cid).required
            ]
            if not required_refs:
                raise ValueError(f"Effect {effect.effect_key!r} must reference at least one required acceptance criterion.")
        return self


# =====================================================================
# Runtime-owned effect record + status
# =====================================================================

ExecutionStage = Literal["validated", "authorized", "dispatch_started", "response_received"]
MutationOutcome = Literal["known_not_applied", "applied_unverified", "unknown"]

EffectStatus = Literal[
    "prepared", "awaiting_approval", "approved", "starting", "executing", "executed_unverified",
    "succeeded", "failed", "failed_known_not_applied", "denied", "cancelled", "superseded",
    "unknown_after_crash", "reconciling", "reconciled_succeeded", "reconciled_not_applied",
    "manual_intervention_required", "cancelled_before_execution",
]

# "In flight": occupies the ledger's single-active-effect slot (blocks a
# new prepare()/get_active() anywhere). Includes "prepared" — a prepared-
# but-not-yet-started effect is still the one effect currently in play.
_IN_FLIGHT_STATUSES = frozenset({
    "prepared", "awaiting_approval", "approved", "starting", "executing",
    "executed_unverified", "unknown_after_crash", "reconciling",
})
# "Started or awaiting": the narrower set that blocks a Task Intent
# revision ("revisions are allowed only before an effect has started or is
# awaiting approval" — merely "prepared" does not block a revision).
_STARTED_OR_AWAITING_STATUSES = frozenset({
    "awaiting_approval", "approved", "starting", "executing", "executed_unverified",
    "unknown_after_crash", "reconciling", "manual_intervention_required",
})
_RESOLVED_STATUSES = frozenset({
    "succeeded", "cancelled", "superseded", "reconciled_succeeded", "cancelled_before_execution",
})

# Audited against the pinned BrowserAgentTools implementation. Every code in
# this allowlist is produced before BrowserAgentTools._call_bridge reaches
# bridge_client.rpc for a mutating browser call. Anything else is conservative.
PRE_DISPATCH_BROWSER_ERROR_CODES = frozenset({
    "INVALID_ARGUMENT", "TAB_NOT_FOUND", "TAB_CLOSED", "NO_MANAGED_TAB",
    "SCOPE_DENIED", "FIREWALL_DENIED", "STALE_OBSERVATION", "REF_NOT_FOUND",
    "SENSITIVE_OPERATION_BLOCKED", "EFFECT_LIMIT_EXCEEDED", "EFFECT_MISMATCH",
    "TASK_INTENT_REQUIRED", "PLAN_REQUIRED", "PLAN_STEP_REQUIRED", "EFFECT_REQUIRED",
    "BROWSER_REBIND_REQUIRED", "LEASE_NOT_HELD",
})


def classify_mutation_outcome(
    *, execution_stage: ExecutionStage, result: Optional[Dict[str, Any]] = None,
    failure_kind: Optional[str] = None,
) -> MutationOutcome:
    """Classify a mutation attempt without guessing from ``ok: false``.

    Only audited pre-dispatch rejections are known not to have applied. Once
    dispatch may have begun, every failure defaults to ``unknown``.
    """
    if result is not None and result.get("ok") is True and execution_stage == "response_received":
        return "applied_unverified"
    code = ((result or {}).get("error") or {}).get("code")
    if execution_stage in ("validated", "authorized") and code in PRE_DISPATCH_BROWSER_ERROR_CODES:
        return "known_not_applied"
    if failure_kind == "pre_dispatch" and execution_stage in ("validated", "authorized"):
        return "known_not_applied"
    return "unknown"


class EffectRecord(BaseModel):
    effect_id: str
    effect_key: str
    plan_task_id: str
    risk: RiskClass
    summary: str
    status: EffectStatus
    allowed_tools: List[str]
    allowed_actions: List[str]
    max_browser_mutations: int
    browser_mutation_count: int = 0
    acceptance_criterion_ids: List[str]
    approval_id: Optional[str] = None
    target_browser_session_id: Optional[str] = None
    passed_criterion_ids: List[str] = Field(default_factory=list)
    failed_criterion_ids: List[str] = Field(default_factory=list)
    mutation_ordinal: int = 1
    execution_stage: ExecutionStage = "validated"
    outcome: Optional[MutationOutcome] = None


class EffectError(RuntimeError):
    """Controlled effect-ledger failure; ``code`` is one of axis.models's
    stable Phase 2 error codes — never a raw traceback-worthy message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EffectLedger:
    """Pure effect transitions over the shared serializable record list."""

    def __init__(
        self, records: Optional[List[EffectRecord]] = None,
        *, id_factory: Optional[Callable[[str, str, int], str]] = None,
    ) -> None:
        self._records_list = records if records is not None else []
        self._id_factory = id_factory

    # -- queries -----------------------------------------------------

    def list_effects(self) -> List[EffectRecord]:
        return list(self._records_list)

    def list_for_task(self, plan_task_id: str) -> List[EffectRecord]:
        return [r for r in self.list_effects() if r.plan_task_id == plan_task_id]

    def get_for_task(self, plan_task_id: str) -> Optional[EffectRecord]:
        history = self.list_for_task(plan_task_id)
        return history[-1] if history else None

    def get_by_effect_key(self, effect_key: str) -> Optional[EffectRecord]:
        for record in reversed(self.list_effects()):
            if record.effect_key == effect_key:
                return record
        return None

    def get_active(self) -> Optional[EffectRecord]:
        """The one in-flight effect anywhere (``prepared`` through
        ``executed_unverified``), or ``None``. At most one such record can
        exist at a time — enforced by ``prepare()``."""
        for record in self.list_effects():
            if record.status in _IN_FLIGHT_STATUSES:
                return record
        return None

    def has_started_or_awaiting(self) -> bool:
        """Whether an effect has started executing or is awaiting approval
        (narrower than ``get_active()`` — a merely ``prepared`` effect does
        not count). Used to decide whether a Task Intent revision is still
        allowed."""
        active = self.get_active()
        return active is not None and active.status in _STARTED_OR_AWAITING_STATUSES

    def get(self, effect_id: str) -> EffectRecord:
        record = next((item for item in self._records_list if item.effect_id == effect_id), None)
        if record is None:
            raise EffectError("EFFECT_NOT_FOUND", f"No effect with id {effect_id!r}.")
        return record

    # -- mutation ------------------------------------------------------

    def prepare(
        self, *, effect_key: str, plan_task_id: str, risk: RiskClass, summary: str,
        allowed_tools: List[str], allowed_actions: List[str], max_browser_mutations: int,
        acceptance_criterion_ids: List[str],
    ) -> EffectRecord:
        if self.get_active() is not None:
            raise EffectError("EFFECT_LIMIT_EXCEEDED", "Another effect is already active; only one effect may be in flight at a time.")
        history = self.list_for_task(plan_task_id)
        for prior in history:
            if prior.effect_key != effect_key:
                raise EffectError("EFFECT_MISMATCH", f"Plan task {plan_task_id!r} is already bound to effect_key {prior.effect_key!r}.")
        latest = history[-1] if history else None
        if latest is not None and latest.status not in ("failed", "failed_known_not_applied", "reconciled_not_applied"):
            raise EffectError("EFFECT_LIMIT_EXCEEDED", f"Effect {effect_key!r} for task {plan_task_id!r} is already {latest.status!r}; it cannot be prepared again.")
        ordinal = 1 + sum(1 for item in self.list_effects() if item.effect_key == effect_key)
        effect_id = self._id_factory(effect_key, plan_task_id, ordinal) if self._id_factory else uuid4().hex[:12]
        record = EffectRecord(
            effect_id=effect_id, effect_key=effect_key, plan_task_id=plan_task_id, risk=risk,
            summary=summary, status="prepared", allowed_tools=list(allowed_tools),
            allowed_actions=list(allowed_actions), max_browser_mutations=max_browser_mutations,
            acceptance_criterion_ids=list(acceptance_criterion_ids), mutation_ordinal=ordinal,
        )
        self._records_list.append(record)
        return record

    def _set(self, effect_id: str, **fields: Any) -> EffectRecord:
        current = self.get(effect_id)
        updated = current.model_copy(update=fields)
        index = next(i for i, item in enumerate(self._records_list) if item.effect_id == effect_id)
        self._records_list[index] = updated
        return updated

    def mark_awaiting_approval(self, effect_id: str, approval_id: str) -> EffectRecord:
        return self._set(effect_id, status="awaiting_approval", approval_id=approval_id)

    def mark_approved(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, status="prepared")  # approved, not yet started

    def mark_authorized(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, execution_stage="authorized")

    def mark_dispatch_scheduled(self, effect_id: str) -> EffectRecord:
        current = self.get(effect_id)
        return self._set(
            effect_id, status="starting", execution_stage="dispatch_started",
            browser_mutation_count=current.browser_mutation_count + 1,
        )

    def mark_started(self, effect_id: str) -> EffectRecord:
        current = self.get(effect_id)
        if current.status == "starting":
            return self._set(effect_id, status="executing")
        return self._set(
            effect_id, status="executing", browser_mutation_count=current.browser_mutation_count + 1,
            execution_stage="dispatch_started",
        )

    def mark_executed_unverified(self, effect_id: str, browser_session_id: str) -> EffectRecord:
        current = self.get(effect_id)
        if current.target_browser_session_id is None:
            return self._set(
                effect_id, status="executed_unverified", target_browser_session_id=browser_session_id,
                execution_stage="response_received", outcome="applied_unverified",
            )
        if current.target_browser_session_id != browser_session_id:
            raise EffectError("EFFECT_MISMATCH", "Effect executed against a different tab than it was bound to.")
        return self._set(
            effect_id, status="executed_unverified", execution_stage="response_received",
            outcome="applied_unverified",
        )

    def mark_outcome(self, effect_id: str, outcome: MutationOutcome, stage: ExecutionStage) -> EffectRecord:
        if outcome == "applied_unverified":
            status = "executed_unverified"
        elif outcome == "known_not_applied":
            current = self.get(effect_id)
            status = "failed" if current.browser_mutation_count >= current.max_browser_mutations else "prepared"
        else:
            status = "unknown_after_crash"
        return self._set(effect_id, status=status, outcome=outcome, execution_stage=stage)

    def mark_failed(self, effect_id: str, *, known_not_applied: bool = False) -> EffectRecord:
        """A single call attempt failed.

        ``known_not_applied=True`` means the bridge proved *this specific
        attempt* never touched the page — its error code is one of
        ``PRE_DISPATCH_BROWSER_ERROR_CODES`` (e.g. a stale ref rejected
        before the fill/click ever reached the element). ``mark_started``
        optimistically counts every attempt against the mutation budget the
        moment the handler is entered, before the outcome is known; a
        pre-dispatch rejection refunds that count and resets execution_stage
        so the same prepared effect is fully reusable — no re-preparation
        required — since nothing was actually attempted against the page.
        This does not weaken the crash/unknown-outcome safety story: a
        result whose code is *not* in that audited allowlist still can't
        prove the page was untouched, so it falls through to the original,
        budget-consuming path below.

        Otherwise, if the mutation budget still has room, the effect
        returns to ``prepared`` so a corrective retry (e.g. re-observe after
        an ambiguous failure, then retry the same logical action) is still
        permitted against this same effect — only once
        ``max_browser_mutations`` is exhausted does the effect become
        terminally ``failed``. Either way, a retry never happens
        automatically: the model must issue a new tool call itself."""
        current = self.get(effect_id)
        if known_not_applied:
            return self._set(
                effect_id, status="prepared",
                browser_mutation_count=max(0, current.browser_mutation_count - 1),
                execution_stage="authorized" if current.approval_id else "validated",
            )
        if current.browser_mutation_count >= current.max_browser_mutations:
            return self._set(effect_id, status="failed")
        return self._set(effect_id, status="prepared")

    def mark_denied(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, status="denied")

    def mark_cancelled(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, status="cancelled")

    def mark_superseded(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, status="superseded")

    def mark_acceptance_passed(self, effect_id: str, criterion_id: str) -> EffectRecord:
        current = self.get(effect_id)
        if criterion_id in current.passed_criterion_ids:
            return current
        return self._set(effect_id, passed_criterion_ids=[*current.passed_criterion_ids, criterion_id])

    def mark_succeeded(self, effect_id: str) -> EffectRecord:
        return self._set(effect_id, status="succeeded")


# =====================================================================
# Acceptance matching
# =====================================================================

_ASSERT_DEFAULTS: Dict[str, Any] = {"contains": False, "timeoutMs": 30000}


def _normalize_assert_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the runtime-supplied session id and fill in documented defaults
    so an omitted-vs-explicit default never breaks a match."""
    normalized = {k: v for k, v in args.items() if k != "browserSessionId"}
    for key, default in _ASSERT_DEFAULTS.items():
        normalized.setdefault(key, default)
    return normalized


def criterion_matches_assertion(
    criterion: AcceptanceCriterion, assert_kwargs: Dict[str, Any], *,
    effect: EffectRecord, trusted_session_id: str,
) -> bool:
    """Whether a just-executed, successful ``browser_assert`` call (its
    validated arguments plus the *trusted, runtime-resolved* session id it
    actually ran against) satisfies ``criterion`` for ``effect``.

    ``trusted_session_id`` must come from the tool result's own echoed
    ``browserSessionId`` (what ``axis.capabilities.browser`` already
    resolves via ``_tab_handle_of``) — never from the model-supplied
    argument alone, and never from anything the ToolGuardrail merely
    observed pre-execution."""
    if effect.target_browser_session_id is None or trusted_session_id != effect.target_browser_session_id:
        return False
    expected = _normalize_assert_args(criterion.assertion)
    actual = _normalize_assert_args(assert_kwargs)
    for key, value in expected.items():
        if actual.get(key) != value:
            return False
    return True


def unresolved_proposed_effects(intent: TaskIntent, ledger: EffectLedger) -> List[str]:
    """``effect_key``s from the *current* Task Intent that are not yet
    represented by a resolved (succeeded/cancelled/superseded) ledger
    record. A missing record (never prepared) counts as unresolved — this
    is what prevents declaring effects, never preparing them, and claiming
    completion anyway."""
    unresolved: List[str] = []
    for proposed in intent.proposed_effects:
        record = ledger.get_by_effect_key(proposed.effect_key)
        if record is None or record.status not in _RESOLVED_STATUSES:
            unresolved.append(proposed.effect_key)
    return unresolved
