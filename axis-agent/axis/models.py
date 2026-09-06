"""Typed dependencies, run-local verification state, the one typed result
model, and execution limits for the Axis root agent.

Deliberately small: this is not a workflow/state module. `AxisRunDeps` is
the trusted, app-supplied binding between one agent run and its
whole-browser `BrowserAgentTools` instance (see that module's own
`tab_registry` for the actual multi-tab trust boundary); `AxisRunState` is
run-local, in-memory verification bookkeeping (never persisted, never
shared across tasks); `AxisTaskResult` is the single structured output
schema the model can produce; `AxisRunLimits` is the non-model-controllable
budget for one run, normally sourced from `axis.yaml` via `axis.config`.
Nothing here is generated or mutated by the model itself.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic import BaseModel, Field, model_validator  # noqa: E402
from pydantic_ai import UsageLimits  # noqa: E402
from pydantic_ai_harness.planning import InMemoryPlanStore  # noqa: E402

from axis.approvals import ApprovalState  # noqa: E402
from axis.effects import EffectLedger, EffectRecord, TaskIntent  # noqa: E402
from axis.planning import AxisPlanItem  # noqa: E402
from browser_agent_tools import BrowserAgentTools  # noqa: E402

# =====================================================================
# Run-local verification state
# =====================================================================


class AxisControlState(BaseModel):
    """Serializable Phase 1/2 truth shared by normal and durable runs."""

    observed_current_run: bool = False
    verification_required: bool = False
    assertion_passed_after_effect: bool = False
    task_intent: Optional[TaskIntent] = None
    plan: List[AxisPlanItem] = Field(default_factory=list)
    plan_revision: int = Field(default=0, ge=0)
    effects: List[EffectRecord] = Field(default_factory=list)
    approval_state: ApprovalState = Field(default_factory=ApprovalState)


# Compatibility name retained for accepted Phase 1/2 callers. The object is
# now the shared serializable control state, not a parallel state container.
AxisRunState = AxisControlState


# =====================================================================
# Typed run dependencies
# =====================================================================


@dataclass(init=False)
class AxisRunDeps:
    """Trusted values one agent run is given, never generated or modified
    by the model.

    There is no single fixed "the" tab for a run anymore: AXIS has
    whole-browser, multi-tab access (see `browser_agent_tools.py`'s
    `browser_tabs` tool and its own trusted tab registry,
    `BrowserAgentTools.tab_registry`). The model discovers, creates,
    activates, and closes logical tab handles itself via `browser_tabs`;
    every other tool call is validated against that same registry
    (`BrowserAgentTools._require_tab`) — there is nothing left for
    `AxisRunDeps` to force-override. `initial_tab_handle` is optional,
    informational context (e.g. "you started on this tab") the root
    agent's instructions may mention, not an enforcement boundary.

    `browser_tools` lifecycle: every task gets a fresh `run_state` (see
    `axis.agent.next_run`), but `browser_tools` itself is bound once per
    interactive session/binding and MAY be reused across that session's
    sequential tasks (this is what `axis.cli` does — one bound session
    keeps the same `BrowserAgentTools` instance, with its own tab
    registry and observations, across every task typed into it). What
    must never happen is two *concurrent* runs sharing the same instance,
    or two separate session/application constructions sharing one — each
    independent binding gets its own.
    """

    run_id: str
    browser_tools: BrowserAgentTools
    plan_store: InMemoryPlanStore
    initial_tab_handle: Optional[str] = None
    job_id: Optional[str] = None
    conversation_id: Optional[str] = None

    # -- Phase 2: task-scoped control state ------------------------------
    # Fresh for every AXIS *task* (see axis.agent.next_run) — never
    # inherited across tasks, and never shared/replaced-by-reference.
    control_state: AxisControlState = field(default_factory=AxisControlState)
    last_plan_snapshot: List["AxisPlanItem"] = field(default_factory=list)

    def __init__(
        self, run_id: str, browser_tools: BrowserAgentTools, plan_store: InMemoryPlanStore,
        run_state: Optional[AxisRunState] = None, initial_tab_handle: Optional[str] = None,
        job_id: Optional[str] = None, conversation_id: Optional[str] = None,
        control_state: Optional[AxisControlState] = None,
    ) -> None:
        self.run_id = run_id
        self.browser_tools = browser_tools
        self.plan_store = plan_store
        self.initial_tab_handle = initial_tab_handle
        self.job_id = job_id
        self.conversation_id = conversation_id
        self.control_state = control_state or run_state or AxisControlState()
        self.last_plan_snapshot = list(self.control_state.plan)

    @property
    def run_state(self) -> AxisControlState:
        return self.control_state

    @run_state.setter
    def run_state(self, value: AxisRunState) -> None:
        self.control_state.observed_current_run = value.observed_current_run
        self.control_state.verification_required = value.verification_required
        self.control_state.assertion_passed_after_effect = value.assertion_passed_after_effect

    @property
    def task_intent(self) -> Optional[TaskIntent]:
        return self.control_state.task_intent

    @task_intent.setter
    def task_intent(self, value: Optional[TaskIntent]) -> None:
        self.control_state.task_intent = value

    @property
    def effect_ledger(self) -> EffectLedger:
        return EffectLedger(self.control_state.effects)

    @effect_ledger.setter
    def effect_ledger(self, value: EffectLedger) -> None:
        self.control_state.effects = value.list_effects()

    @property
    def approval_state(self) -> ApprovalState:
        return self.control_state.approval_state

    @approval_state.setter
    def approval_state(self, value: ApprovalState) -> None:
        self.control_state.approval_state = value


# =====================================================================
# One typed task result
# =====================================================================


class AxisTaskResult(BaseModel):
    """The single structured-output schema the model can produce — one
    provider-facing output tool, not four. `status` selects which of the
    optional fields are required; see the validator below."""

    status: Literal["completed", "needs_user_input", "failed", "cancelled"]
    summary: str
    verification_summary: Optional[str] = None
    question: Optional[str] = None
    choices: List[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    retryable: bool = False
    warnings: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_fields_for_status(self) -> "AxisTaskResult":
        if self.status == "completed" and not self.verification_summary:
            raise ValueError("status='completed' requires 'verification_summary'.")
        if self.status == "needs_user_input" and not self.question:
            raise ValueError("status='needs_user_input' requires 'question'.")
        if self.status == "failed" and not self.error_code:
            raise ValueError("status='failed' requires 'error_code'.")
        if self.status == "cancelled" and (
            self.verification_summary or self.question or self.choices or self.error_code
        ):
            raise ValueError(
                "status='cancelled' must not include verification_summary/question/choices/error_code."
            )
        return self


# Stable, run-level error codes. Browser-tool errors already have their own
# codes (browser_agent_tools.py's INVALID_ARGUMENT/SCOPE_DENIED/TAB_NOT_FOUND/
# etc.) — these are only for failures at the agent-run level, above the tools.
#
# Four distinct, precise limit codes replace one undifferentiated
# "RUN_LIMIT_EXCEEDED" — see axis.agent._classify_usage_limit_error, which
# maps a caught UsageLimitExceeded to exactly one of the first three based on
# its message (the pinned Pydantic AI version's exception carries no
# structured field for this — see that function's docstring).
REQUEST_LIMIT_EXCEEDED = "REQUEST_LIMIT_EXCEEDED"
TOOL_CALL_LIMIT_EXCEEDED = "TOOL_CALL_LIMIT_EXCEEDED"
TOKEN_LIMIT_EXCEEDED = "TOKEN_LIMIT_EXCEEDED"
WALL_CLOCK_LIMIT_EXCEEDED = "WALL_CLOCK_LIMIT_EXCEEDED"
RUN_TIMEOUT = WALL_CLOCK_LIMIT_EXCEEDED  # back-compat alias
PROVIDER_ERROR = "PROVIDER_ERROR"
MODEL_ERROR = "MODEL_ERROR"
NO_MANAGED_TAB = "NO_MANAGED_TAB"
UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"

# Phase 2: Task Intent / Planning / Effect / Approval / Acceptance error
# codes. Returned in AxisTaskResult.error_code or embedded (never as a raw
# exception) in a ModelRetry/tool-block message.
TASK_INTENT_REQUIRED = "TASK_INTENT_REQUIRED"
TASK_INTENT_INVALID = "TASK_INTENT_INVALID"
BLOCKING_AMBIGUITY = "BLOCKING_AMBIGUITY"
PLAN_REQUIRED = "PLAN_REQUIRED"
PLAN_STEP_REQUIRED = "PLAN_STEP_REQUIRED"
PLAN_STEP_NOT_ACTIVE = "PLAN_STEP_NOT_ACTIVE"
EFFECT_REQUIRED = "EFFECT_REQUIRED"
EFFECT_NOT_FOUND = "EFFECT_NOT_FOUND"
EFFECT_MISMATCH = "EFFECT_MISMATCH"
EFFECT_LIMIT_EXCEEDED = "EFFECT_LIMIT_EXCEEDED"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
APPROVAL_DENIED = "APPROVAL_DENIED"
ACCEPTANCE_UNRESOLVED = "ACCEPTANCE_UNRESOLVED"
PLAN_INCOMPLETE = "PLAN_INCOMPLETE"
CONTROL_STATE_ERROR = "CONTROL_STATE_ERROR"


# =====================================================================
# Execution limits (non-model-controllable)
# =====================================================================


class AxisRunLimitsError(RuntimeError):
    """Raised when execution limits are missing or misconfigured (invalid
    values, or an unparsable environment variable) — a controlled
    configuration failure, never an unhandled traceback."""


@dataclass(frozen=True)
class AxisRunLimits:
    """One run's execution budget. Supplied by application code only — the
    model never sees this object and has no field or tool that can change
    it. Normally sourced from ``axis.yaml`` via ``axis.config`` — there is
    no hidden hard-coded fallback that silently overrides configured
    values; the defaults below only apply to a field a caller genuinely
    omits. Every field accepts ``None`` to disable that individual limit.
    Construct via `AxisRunLimits(...)` directly (validates eagerly) or
    `AxisRunLimits.from_env()` (legacy env-var source; prefer YAML)."""

    max_requests: Optional[int] = 20
    max_tool_calls: Optional[int] = 30
    max_total_tokens: Optional[int] = 100_000
    max_wall_clock_seconds: Optional[float] = 180.0

    def __post_init__(self) -> None:
        if self.max_requests is not None and self.max_requests <= 0:
            raise AxisRunLimitsError("max_requests must be None or > 0.")
        if self.max_tool_calls is not None and self.max_tool_calls <= 0:
            raise AxisRunLimitsError("max_tool_calls must be None or > 0.")
        if self.max_total_tokens is not None and self.max_total_tokens <= 0:
            raise AxisRunLimitsError("max_total_tokens must be None or > 0.")
        if self.max_wall_clock_seconds is not None and self.max_wall_clock_seconds <= 0:
            raise AxisRunLimitsError("max_wall_clock_seconds must be None or > 0.")

    @classmethod
    def from_env(cls) -> "AxisRunLimits":
        # Each helper raises AxisRunLimitsError naming only the variable,
        # never the value that failed to parse — an operator's malformed
        # (or accidentally secret-shaped) env value must never end up in an
        # exception message that later gets returned to a caller or logged.
        return cls(
            max_requests=_optional_int_env("AXIS_MAX_REQUESTS", cls.max_requests),
            max_tool_calls=_optional_int_env("AXIS_MAX_TOOL_CALLS", cls.max_tool_calls),
            max_total_tokens=_optional_int_env("AXIS_MAX_TOTAL_TOKENS", cls.max_total_tokens),
            max_wall_clock_seconds=_optional_float_env("AXIS_MAX_WALL_CLOCK_SECONDS", cls.max_wall_clock_seconds),
        )

    def to_usage_limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.max_requests,
            tool_calls_limit=self.max_tool_calls,
            total_tokens_limit=self.max_total_tokens,
        )


def _optional_float_env(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip() == "" or raw.strip().lower() == "none":
        return None
    try:
        return float(raw)
    except ValueError:
        raise AxisRunLimitsError(f"Invalid value for {name}: must be a number or 'none'.") from None


def _optional_int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip() == "" or raw.strip().lower() == "none":
        return None
    try:
        return int(raw)
    except ValueError:
        raise AxisRunLimitsError(f"Invalid value for {name}: must be an integer or 'none'.") from None
