"""Typed dependencies, results, and execution limits for the Axis Phase 1
root agent.

Deliberately small: this is not a workflow/state module. `AxisRunDeps` is
the trusted, app-supplied binding between one agent run and one already
Agent-managed browser tab; `AxisTaskResult` is the closed set of outcomes
the model can produce; `AxisRunLimits` is the non-model-controllable budget
for one run. Nothing here is generated or mutated by the model itself.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Union

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import UsageLimits  # noqa: E402

from browser_agent_tools import BrowserAgentTools  # noqa: E402

# =====================================================================
# Typed run dependencies
# =====================================================================


@dataclass
class AxisRunDeps:
    """Trusted values one agent run is given, never generated or modified
    by the model. `browser_session_id` is the ONLY session identifier a
    tool call may ever act on for this run — see
    ``axis_browser_capability.AxisBrowserCapability`` for the enforcement.

    `browser_tools` must be a `BrowserAgentTools` instance dedicated to
    this run (its own `self.observations`/`self.browser_sessions` dicts),
    never shared with a concurrent run — that is what keeps observations
    from leaking between unrelated tasks.
    """

    run_id: str
    browser_session_id: str
    browser_tools: BrowserAgentTools
    job_id: Optional[str] = None
    conversation_id: Optional[str] = None


# =====================================================================
# Typed task results
# =====================================================================


class TaskCompleted(BaseModel):
    """The requested outcome was verified — never returned just because an
    action or wait succeeded; only after a final observation or a passing
    browser_assert confirms it."""

    status: Literal["completed"] = "completed"
    summary: str
    verification_summary: str
    warnings: List[str] = Field(default_factory=list)


class NeedsUserInput(BaseModel):
    status: Literal["needs_user_input"] = "needs_user_input"
    question: str
    reason: str
    choices: List[str] = Field(default_factory=list)


class TaskFailed(BaseModel):
    status: Literal["failed"] = "failed"
    summary: str
    error_code: str
    retryable: bool = False


class TaskCancelled(BaseModel):
    status: Literal["cancelled"] = "cancelled"
    summary: str


AxisTaskResult = Union[TaskCompleted, NeedsUserInput, TaskFailed, TaskCancelled]

# Stable, run-level error codes. Browser-tool errors already have their own
# codes (browser_agent_tools.py's INVALID_ARGUMENT/SCOPE_DENIED/etc.) —
# these are only for failures at the agent-run level, above the tools.
RUN_LIMIT_EXCEEDED = "RUN_LIMIT_EXCEEDED"
RUN_TIMEOUT = "RUN_TIMEOUT"
PROVIDER_ERROR = "PROVIDER_ERROR"
MODEL_ERROR = "MODEL_ERROR"
NO_MANAGED_TAB = "NO_MANAGED_TAB"
UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


# =====================================================================
# Execution limits (non-model-controllable)
# =====================================================================


@dataclass(frozen=True)
class AxisRunLimits:
    """One run's execution budget. Supplied by application code only — the
    model never sees this object and has no field or tool that can change
    it. Defaults are sized for a small browser task (a handful of
    observe/act/assert steps), not a long-running job."""

    max_requests: int = 20
    max_tool_calls: int = 30
    max_total_tokens: Optional[int] = 100_000
    max_wall_clock_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "AxisRunLimits":
        return cls(
            max_requests=_int_env("AXIS_MAX_REQUESTS", cls.max_requests),
            max_tool_calls=_int_env("AXIS_MAX_TOOL_CALLS", cls.max_tool_calls),
            max_total_tokens=_optional_int_env("AXIS_MAX_TOTAL_TOKENS", cls.max_total_tokens),
            max_wall_clock_seconds=_float_env("AXIS_MAX_WALL_CLOCK_SECONDS", cls.max_wall_clock_seconds),
        )

    def to_usage_limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.max_requests,
            tool_calls_limit=self.max_tool_calls,
            total_tokens_limit=self.max_total_tokens,
        )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _optional_int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip() == "" or raw.strip().lower() == "none":
        return None
    return int(raw)
