"""Typed models, bounded task memory, and the YAML configuration for AXIS.

Everything the two agents produce or consume is defined here, plus the one
configuration model that ``axis.yaml`` maps onto. Nothing in this module talks
to a browser, a provider, or Pydantic AI's runtime.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from pydantic_ai import CancellationToken, RunUsage

# Bounds. Model output is never rejected for exceeding these — validators clip
# instead, so a chatty model costs a truncation rather than a retry.
MAX_STRING = 600
MAX_ANSWER = 4_000
MAX_SNAPSHOT = 12_000
MAX_EVIDENCE = 6
MAX_EXTRACT = 4_000

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "axis.yaml"


def clip(text: str | None, limit: int = MAX_STRING) -> str | None:
    """Bound one string for storage or for a prompt."""
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


def clip_all(items: list[str] | None, count: int = MAX_EVIDENCE, limit: int = MAX_STRING) -> list[str]:
    return [clip(item, limit) or "" for item in (items or [])[:count]]


# ---------------------------------------------------------------------------
# Agent output types
# ---------------------------------------------------------------------------

class PlanDecision(BaseModel):
    """The planner's typed decision. The planner has no tools; this is its
    only channel, and it is the only place overall completion is decided."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["browse", "complete", "ask_user", "fail"]
    plan_summary: str | None = None
    next_goal: str | None = None
    success_condition: str | None = None
    final_answer: str | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("plan_summary", "next_goal", "success_condition", "reason")
    @classmethod
    def _short(cls, value: str | None) -> str | None:
        return clip(value)

    @field_validator("final_answer")
    @classmethod
    def _answer(cls, value: str | None) -> str | None:
        return clip(value, MAX_ANSWER)

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        return clip_all(value)


class NavigatorOutcome(BaseModel):
    """The navigator's typed step result. ``goal_reached`` is a claim about the
    planner's *immediate* goal only, never about the whole task."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["continue", "goal_reached", "blocked", "ask_user", "failed"]
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("summary", "reason")
    @classmethod
    def _short(cls, value: str) -> str:
        return clip(value) or ""

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        return clip_all(value)


# ---------------------------------------------------------------------------
# Browser-facing state handed to the navigator
# ---------------------------------------------------------------------------

class ActionRecord(BaseModel):
    """One executed (or refused) browser tool call, already sanitized by
    ``browser_tools`` and bounded here."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    tab: str | None = None
    operation: str | None = None
    args: str | None = None
    executed: bool = True
    execution_success: bool = False
    semantic_success: bool | None = None
    code: str | None = None
    message: str | None = None
    extracted_data: str | None = None
    refs_invalidated: bool = False
    retain_in_memory: bool = False
    meaningful_change: bool | None = None
    result_data: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @computed_field
    @property
    def success(self) -> bool:
        """Compatibility view used by events and callers.

        Transport/execution success and semantic success intentionally remain
        separate in storage: an assertion can execute correctly and still fail.
        """
        return self.executed and self.execution_success and self.semantic_success is not False

    @computed_field
    @property
    def error(self) -> str | None:
        if self.success:
            return None
        detail = ": ".join(part for part in (self.code, self.message) if part)
        return clip(detail) if detail else "The browser action did not succeed."

    @field_validator("args")
    @classmethod
    def _args(cls, value: str | None) -> str | None:
        return clip(value)

    @field_validator("extracted_data")
    @classmethod
    def _extract(cls, value: str | None) -> str | None:
        return clip(value, MAX_EXTRACT)

    @field_validator("message")
    @classmethod
    def _error(cls, value: str | None) -> str | None:
        return clip(value)


class AssertionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion: str
    passed: bool
    expected: Any = None
    tab: str | None = None
    message: str | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["observation", "navigator", "assertion", "screenshot", "download", "console", "network"]
    detail: str
    tab: str | None = None
    verified: bool = True

    @field_validator("detail")
    @classmethod
    def _detail(cls, value: str) -> str:
        return clip(value, MAX_EXTRACT) or ""


class TaskRequirements(BaseModel):
    """Only the literal facts needed for deterministic completion checks."""

    model_config = ConfigDict(extra="forbid")

    literal_urls: list[str] = Field(default_factory=list)
    required_text: list[str] = Field(default_factory=list)
    final_url_pattern: str | None = None
    require_screenshot: bool = False
    require_download: bool = False
    require_console_check: bool = False
    require_network_check: bool = False
    mutation_allowed: bool = True
    forbidden_actions: set[str] = Field(default_factory=set)


class RunCounters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    navigator_steps_since_plan: int = 0
    total_steps: int = 0
    planner_passes: int = 0
    browser_actions: int = 0
    consecutive_failures: int = 0
    mutation_count: int = 0
    verified_mutation_count: int = 0
    repeated_observations: int = 0
    repeated_actions: int = 0
    no_progress_steps: int = 0


class CompletionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    reasons: list[str] = Field(default_factory=list)


class TabSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: str
    title: str | None = None
    url: str | None = None
    active: bool = False


class BrowserState(BaseModel):
    """The fresh, bounded page view assembled before every navigator step.

    Everything in ``snapshot``, ``title``, ``url``, and ``tabs`` is untrusted
    web content; the navigator's instructions say so explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    tab: str | None = None
    url: str | None = None
    title: str | None = None
    tabs: list[TabSummary] = Field(default_factory=list)
    scroll: dict[str, float] | None = None
    snapshot: str = ""
    truncated: bool = False
    screenshot: str | None = None
    refs_fresh: bool = False
    unverified_page_changes: int = 0
    site_pattern: dict[str, Any] | None = None
    runtime_warning: str | None = None
    recent_actions: list[ActionRecord] = Field(default_factory=list)
    goal: str | None = None
    success_condition: str | None = None
    remaining_steps: int = 0
    remaining_actions: int = 0
    note: str | None = None
    requirements: TaskRequirements | None = None

    @field_validator("snapshot")
    @classmethod
    def _snapshot(cls, value: str) -> str:
        return clip(value, MAX_SNAPSHOT) or ""


# ---------------------------------------------------------------------------
# Bounded task memory
# ---------------------------------------------------------------------------

class TaskRunState(BaseModel):
    """The single, bounded source of truth for one task and its follow-ups."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    original_request: str = ""
    follow_ups: list[str] = Field(default_factory=list)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    bound_tab: str | None = None
    task_tabs: list[str] = Field(default_factory=list)
    diagnostics_armed_tabs: set[str] = Field(default_factory=set)
    evidence: list[Evidence] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    assertions: list[AssertionRecord] = Field(default_factory=list)
    downloads: list[Evidence] = Field(default_factory=list)
    diagnostic_evidence: list[Evidence] = Field(default_factory=list)
    last_browser_state: BrowserState | None = Field(default=None, exclude=True)
    usage: RunUsage = Field(default_factory=RunUsage, exclude=True)
    cancellation_token: CancellationToken = Field(default_factory=CancellationToken, exclude=True)
    counters: RunCounters = Field(default_factory=RunCounters)
    status: Literal[
        "active", "completed", "failed", "cancelled", "needs_user", "paused", "limit_reached"
    ] = "active"

    # Current planning state is part of the same task object, not a second
    # memory subsystem.
    current_plan_summary: str | None = None
    current_goal: str | None = None
    current_success_condition: str | None = None
    completed_goal_summaries: list[str] = Field(default_factory=list)
    latest_navigator_outcome: NavigatorOutcome | None = None
    relevant_extracted_data: list[str] = Field(default_factory=list)
    last_error: str | None = None
    last_page_fingerprint: str | None = None
    last_action_fingerprint: str | None = None
    direct_navigation_done: bool = False
    completion_rejected: bool = False
    repair_attempted: bool = False
    debug_telemetry_started: bool = False
    started_monotonic: float = Field(default_factory=time.monotonic, exclude=True)
    model_ms: int = 0
    browser_ms: int = 0
    plan_ms: int = 0
    step_ms: int = 0

    # bounds, injected from MemoryConfig
    max_recent_actions: int = 8
    max_completed_goals: int = 8
    max_followups: int = 5
    max_extracted_data: int = 8

    def add_followup(self, follow_up: str) -> None:
        self.follow_ups = (self.follow_ups + [clip(follow_up, MAX_ANSWER) or ""])[-self.max_followups:]

    def complete_goal(self, summary: str) -> None:
        self.completed_goal_summaries = (
            self.completed_goal_summaries + [clip(summary) or ""]
        )[-self.max_completed_goals:]

    def record_actions(self, records: list[ActionRecord]) -> None:
        self.actions = (self.actions + records)[-self.max_recent_actions:]
        extracted = [
            f"{record.tool}: {record.extracted_data}"
            for record in records
            if record.retain_in_memory and record.extracted_data
        ]
        if extracted:
            self.relevant_extracted_data = (
                self.relevant_extracted_data + extracted
            )[-self.max_extracted_data:]

    def evidence_by_kind(self, kind: str) -> list[Evidence]:
        return [item for item in self.evidence if item.kind == kind]

    @property
    def unverified_mutations(self) -> int:
        return max(0, self.counters.mutation_count - self.counters.verified_mutation_count)

    @property
    def original_task(self) -> str:
        return self.original_request

    @property
    def user_followups(self) -> list[str]:
        return self.follow_ups

    @property
    def recent_action_results(self) -> list[ActionRecord]:
        return self.actions

    @property
    def navigator_steps_since_plan(self) -> int:
        return self.counters.navigator_steps_since_plan

    @property
    def total_steps(self) -> int:
        return self.counters.total_steps

    @property
    def consecutive_failures(self) -> int:
        return self.counters.consecutive_failures

    @property
    def mutation_count(self) -> int:
        return self.counters.mutation_count

    @property
    def verified_mutation_count(self) -> int:
        return self.counters.verified_mutation_count


# Backwards-compatible import name; there is only one state implementation.
TaskMemory = TaskRunState


# ---------------------------------------------------------------------------
# Run result and events
# ---------------------------------------------------------------------------

class AxisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed", "needs_user", "paused", "cancelled", "limit_reached"]
    answer: str | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    total_steps: int = 0
    planner_passes: int = 0
    browser_actions: int = 0
    model_requests: int = 0
    # Wall-clock breakdown, so a slow run can be attributed rather than guessed
    # at: model time is provider latency, browser time is bridge/page latency,
    # and whatever is left is orchestration.
    duration_ms: int = 0
    model_ms: int = 0
    browser_ms: int = 0


class AxisEvent(BaseModel):
    """One bounded event for an optional caller-supplied callback. Not a bus,
    not persisted, not a UI contract."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["planner_decision", "navigator_step", "browser_action", "status", "final"]
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Configuration (axis.yaml)
# ---------------------------------------------------------------------------

class FirewallConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: Literal["allow", "deny"] = "allow"
    allow_urls: list[str] = Field(default_factory=lambda: ["*"])
    deny_urls: list[str] = Field(default_factory=list)
    deny_schemes: list[str] = Field(
        default_factory=lambda: ["chrome", "chrome-extension", "devtools", "javascript"]
    )
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    deny_methods: list[str] = Field(default_factory=list)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whole_browser_access: bool = True
    initial_tab: Literal["focused", "new"] = "focused"
    max_open_tabs: int = 30
    allow_response_body: bool = False
    allow_coordinate_fallback: bool = False
    artifact_directory: str | None = None
    upload_roots: list[str] = Field(default_factory=list)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)


class ProviderOverrides(BaseModel):
    """Optional overrides on top of the ``AXIS_OCI_*`` environment variables
    that ``provider_config.py`` already owns."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    base_url: str | None = None
    api: Literal["responses", "chat"] = "responses"
    settings: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_interval_steps: int = Field(default=3, ge=1)
    max_actions_per_step: int = Field(default=3, ge=1)
    max_total_steps: int = Field(default=30, ge=1)
    max_model_requests: int = Field(default=60, ge=1)
    max_browser_actions: int = Field(default=90, ge=1)
    max_consecutive_failures: int = Field(default=3, ge=1)
    observation_mode: Literal["compact", "aria", "text"] = "compact"
    observation_max_nodes: int = Field(default=1_000, ge=1, le=2_000)
    include_screenshot: bool = False
    tool_timeout_seconds: float | None = 120.0


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_evidence: bool = False
    diagnose: bool = False
    downloads: bool = False
    debug_recording: bool = False
    tool_search: bool = False
    approval_required_actions: list[str] = Field(default_factory=lambda: ["upload", "drag"])


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recent_actions: int = Field(default=8, ge=1)
    max_completed_goals: int = Field(default=8, ge=1)
    max_followups: int = Field(default=5, ge=1)
    max_extracted_data: int = Field(default=8, ge=1)


class AxisConfig(BaseModel):
    """Runtime configuration only. Each agent's Pydantic AI ``AgentSpec``
    lives in its own file (``planner_spec_path`` / ``navigator_spec_path``)
    and is loaded directly with ``Agent.from_file()`` — this class never
    duplicates the ``AgentSpec`` schema pydantic_ai already validates."""

    model_config = ConfigDict(extra="forbid")

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    provider: ProviderOverrides = Field(default_factory=ProviderOverrides)
    run: RunConfig = Field(default_factory=RunConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    planner_spec_path: str = "axis/planner_agent.yaml"
    navigator_spec_path: str = "axis/navigator_agent.yaml"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AxisConfig":
        """Read ``axis.yaml`` (or an explicit path). This is the only place
        runtime configuration is read; there is no configuration framework."""
        source = Path(path) if path else DEFAULT_CONFIG_PATH
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source} must contain a YAML mapping.")
        config = cls.model_validate(raw)
        for field in ("planner_spec_path", "navigator_spec_path"):
            resolved = Path(getattr(config, field))
            if not resolved.is_absolute():
                resolved = (source.parent / resolved).resolve()
            setattr(config, field, str(resolved))
        return config

    def new_memory(self, task: str, requirements: TaskRequirements | None = None) -> TaskRunState:
        return TaskRunState(
            original_request=clip(task, MAX_ANSWER) or "",
            requirements=requirements or TaskRequirements(),
            **self.memory.model_dump(),
        )
