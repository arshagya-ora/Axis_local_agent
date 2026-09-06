"""The Axis Phase 1 root agent.

One typed Pydantic AI `Agent[AxisRunDeps, AxisTaskResult]` wired to the
seven frozen browser tools via `AxisBrowserCapability`, running small
natural-language browser tasks under simple, non-model-controllable limits
(`AxisRunLimits`). See `axis_cli.py` for the terminal harness that drives
this, and `tests/test_axis_agent.py` for deterministic scripted-model tests.

Per the Phase 0 compatibility findings (`PHASE_0_BASELINE.md` — nothing was
confirmed `supported` against the live OCI/Grok endpoint), this module
avoids depending on any provider-specific capability: structured output
uses Pydantic AI's own model-agnostic tool-call-based output extraction
(plain `output_type=` with a Union of models — verified against the pinned
`pydantic-ai-slim==2.40.0` to build one ordinary function-callable "output
tool" per variant, not any provider-native structured-output feature),
conversation state is plain in-process `message_history` (no
`previous_response_id`), limits use Pydantic AI's own `UsageLimits`
accounting plus a plain `asyncio.wait_for` wall-clock timeout (no
provider-native background/cancel), and there is no reliance on parallel
tool calls, streaming, or provider-native compaction.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai import Agent, ModelRetry, RunContext, capture_run_messages  # noqa: E402
from pydantic_ai.exceptions import (  # noqa: E402
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolReturnPart  # noqa: E402
from pydantic_ai.models.openai import OpenAIResponsesModel  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402

from axis_browser_capability import AxisBrowserCapability  # noqa: E402
from axis_events import RunEventLogger  # noqa: E402
from axis_models import (  # noqa: E402
    MODEL_ERROR,
    NO_MANAGED_TAB,
    PROVIDER_ERROR,
    RUN_LIMIT_EXCEEDED,
    RUN_TIMEOUT,
    UNEXPECTED_ERROR,
    AxisRunDeps,
    AxisRunLimits,
    AxisTaskResult,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)
from browser_agent_tools import BrowserAgentTools  # noqa: E402
from provider_config import ProviderConfig, build_async_openai_client  # noqa: E402

DEFAULT_RETRIES = 2

# Permanent AXIS safety instructions. Deliberately does not restate what the
# seven tool schemas/descriptions and browser_agent_tools.BROWSER_AGENT_INSTRUCTIONS
# already say — only the cross-cutting rules that must hold regardless of
# which tool is being used.
AXIS_SAFETY_INSTRUCTIONS = """You are the Axis browser agent, operating one already-bound, human-approved Chrome tab.

Non-negotiable rules:
- Operate only within the bound managed browser session; you cannot select or discover another tab.
- Observe before any element-targeted action that needs a ref.
- Use refs only from the latest compact observation; a stale or missing ref means observe again — never retry the same ref.
- Never invent refs, browserSessionId values, URLs, selectors, or file paths — use only values already observed or given to you.
- Perform exactly one semantic interaction per browser_act call.
- Re-observe after navigation or any major page change before interacting further.
- Use browser_wait only for a real asynchronous condition, never as an arbitrary delay.
- Do not report a task as completed until the requested outcome is confirmed by a fresh observation or a passing browser_assert — a successful action or wait is not proof of completion.
- Use browser_diagnose only after an unexpected failure or an explicit diagnostic request.
- You have no way to run raw RPC methods, arbitrary JavaScript, read cookies, change policy, modify headers, bypass CSP, control the extension, or send coordinate/mouse input — never ask for or claim any of these.

When you have a final result, produce exactly one of: TaskCompleted (only once the outcome is verified), NeedsUserInput (a specific question blocking further progress), TaskFailed (a clear reason), or TaskCancelled.
"""


def build_model(config: ProviderConfig) -> Tuple[OpenAIResponsesModel, Any]:
    """Build the Phase-0-approved OCI/Grok model. Returns (model, client) —
    the caller owns closing `client` (see axis_cli.py's finally block)."""
    client = build_async_openai_client(config)
    provider = OpenAIProvider(openai_client=client)
    return OpenAIResponsesModel(config.model, provider=provider), client


def _last_tool_return(messages: List[ModelMessage]) -> Optional[ToolReturnPart]:
    last: Optional[ToolReturnPart] = None
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                last = part
    return last


def _completion_is_verified(messages: List[ModelMessage]) -> bool:
    """Completion requires a final observation or a passing browser_assert
    as the most recent tool result — not merely a successful action or
    wait. Checked here as a real output validator (not just prompt text)
    so an eager model cannot report TaskCompleted on its own say-so."""
    last = _last_tool_return(messages)
    if last is None or not isinstance(last.content, dict) or not last.content.get("ok"):
        return False
    if last.tool_name == "browser_assert":
        return bool((last.content.get("data") or {}).get("passed"))
    return last.tool_name == "browser_observe"


def build_axis_agent(
    model: Any,
    browser_capability: Optional[AxisBrowserCapability] = None,
    event_logger: Optional[RunEventLogger] = None,
) -> Agent[AxisRunDeps, AxisTaskResult]:
    """Build the one authoritative Axis root agent. `model` may be a real
    provider model or a Pydantic AI test model (TestModel/FunctionModel).

    Tools are built once here and reused across every run this agent
    serves (see AxisBrowserCapability's own docstring for why that's safe),
    so `event_logger` must be the same instance passed to every
    `run_axis_task()` call for this agent — otherwise tool-level events and
    run-level events land in different loggers/sinks."""
    capability = browser_capability or AxisBrowserCapability(event_logger=event_logger)
    instructions = f"{AXIS_SAFETY_INSTRUCTIONS}\n\n{capability.instructions}"
    agent: Agent[AxisRunDeps, AxisTaskResult] = Agent(
        model=model,
        deps_type=AxisRunDeps,
        output_type=AxisTaskResult,
        tools=capability.build_tools(),
        instructions=instructions,
        retries=DEFAULT_RETRIES,
    )

    @agent.output_validator
    def _verify_completion_is_earned(ctx: RunContext[AxisRunDeps], output: AxisTaskResult) -> AxisTaskResult:
        if isinstance(output, TaskCompleted) and not _completion_is_verified(ctx.messages):
            raise ModelRetry(
                "TaskCompleted requires a passing browser_assert or a fresh browser_observe as your "
                "most recent tool call — a successful browser_act or browser_wait alone does not prove "
                "the outcome. Verify with browser_assert (or re-observe) before reporting completion."
            )
        return output

    @agent.instructions
    def _tell_bound_session(ctx: RunContext[AxisRunDeps]) -> str:
        # The browserSessionId schema field is part of the frozen contract
        # and can't be removed, so the model still has to write something
        # there — telling it the real value (read from trusted deps, never
        # from the model) is what makes that a formality rather than a
        # guess. axis_browser_capability.py still force-overrides whatever
        # is actually supplied, as defense in depth against a model that
        # ignores this or hallucinates a different value.
        return (
            f"Your bound browserSessionId for every browser_* tool call is exactly "
            f"{ctx.deps.browser_session_id!r}. Never use any other value and never invent one."
        )

    return agent


def bind_axis_run_deps(
    browser_tools: BrowserAgentTools, *, job_id: Optional[str] = None, conversation_id: Optional[str] = None,
) -> Tuple[Optional[AxisRunDeps], Optional[Dict[str, Any]]]:
    """Bind Chrome's focused Agent-managed tab and produce trusted
    `AxisRunDeps` for it. Returns `(deps, None)` on success or
    `(None, error_dict)` — the same error envelope shape
    `bind_active_managed_tab()` already returns (e.g. `NO_MANAGED_TAB`)."""
    bound = browser_tools.bind_active_managed_tab()
    if not bound["ok"]:
        return None, bound["error"]
    deps = AxisRunDeps(
        run_id=_new_run_id(),
        browser_session_id=bound["data"]["browserSessionId"],
        browser_tools=browser_tools,
        job_id=job_id,
        conversation_id=conversation_id,
    )
    return deps, None


def no_managed_tab_result(error: Dict[str, Any]) -> TaskFailed:
    """The typed failure to report when no Agent-managed tab is bound at
    all — i.e. before an `AxisRunDeps` (which requires a real
    browser_session_id) can even be constructed."""
    return TaskFailed(
        summary=error.get("message", "No Agent-managed Chrome tab is bound."),
        error_code=NO_MANAGED_TAB,
        retryable=True,
    )


def next_run(deps: AxisRunDeps) -> AxisRunDeps:
    """A fresh `AxisRunDeps` for a new task against the same bound session
    and `BrowserAgentTools` instance — only `run_id` changes, so each task
    gets its own run-correlation id without re-binding the tab."""
    return replace(deps, run_id=_new_run_id())


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _emit_turn_events(logger: RunEventLogger, run_id: str, messages: List[ModelMessage]) -> None:
    """Best-effort per-turn events, reconstructed after the fact from the
    captured message history rather than a live streaming hook — simple
    and reliable, at the cost of not knowing individual turn durations."""
    for message in messages:
        if isinstance(message, ModelResponse):
            logger.turn_started(run_id)
            logger.turn_completed(run_id)


async def run_axis_task(
    agent: Agent[AxisRunDeps, AxisTaskResult],
    deps: AxisRunDeps,
    user_prompt: str,
    *,
    message_history: Optional[List[ModelMessage]] = None,
    limits: Optional[AxisRunLimits] = None,
    event_logger: Optional[RunEventLogger] = None,
) -> Tuple[AxisTaskResult, List[ModelMessage]]:
    """Run one task to a typed `AxisTaskResult`. Never raises: every
    provider, model, tool-budget, timeout, and cancellation failure is
    caught here and converted into `TaskFailed`/`TaskCancelled` so callers
    (the CLI, tests) never need their own top-level exception handling for
    this call. No browser effects are attempted after a limit/timeout stops
    the run — the failure paths below return immediately without touching
    the agent or the bridge again."""
    limits = limits or AxisRunLimits.from_env()
    logger = event_logger or RunEventLogger()
    start = time.perf_counter()
    logger.run_started(deps.run_id)

    captured: List[ModelMessage] = []
    try:
        with capture_run_messages() as messages:
            captured = messages
            result = await asyncio.wait_for(
                agent.run(
                    user_prompt,
                    deps=deps,
                    message_history=message_history,
                    usage_limits=limits.to_usage_limits(),
                ),
                timeout=limits.max_wall_clock_seconds,
            )
    except asyncio.TimeoutError:
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return TaskFailed(
            summary=f"The task did not complete within {limits.max_wall_clock_seconds:.0f}s.",
            error_code=RUN_TIMEOUT, retryable=True,
        ), captured
    except UsageLimitExceeded as exc:
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return TaskFailed(summary=str(exc), error_code=RUN_LIMIT_EXCEEDED, retryable=True), captured
    except (ModelHTTPError, ModelAPIError) as exc:
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return TaskFailed(summary=f"Provider error: {exc}", error_code=PROVIDER_ERROR, retryable=True), captured
    except UnexpectedModelBehavior as exc:
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return TaskFailed(summary=f"Model behaved unexpectedly: {exc}", error_code=MODEL_ERROR, retryable=False), captured
    except asyncio.CancelledError:
        logger.run_cancelled(deps.run_id, _elapsed_ms(start))
        return TaskCancelled(summary="The task was cancelled."), captured
    except Exception as exc:  # noqa: BLE001 - this boundary must never raise
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return TaskFailed(
            summary=f"Unexpected error: {type(exc).__name__}: {exc}", error_code=UNEXPECTED_ERROR, retryable=False,
        ), captured

    _emit_turn_events(logger, deps.run_id, captured)
    logger.run_completed(deps.run_id, _elapsed_ms(start))
    return result.output, result.all_messages()
