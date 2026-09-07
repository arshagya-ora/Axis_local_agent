"""The Axis root agent: Agent construction and the run boundary.

Per the Phase 0 compatibility findings (`PHASE_0_BASELINE.md` — nothing was
confirmed `supported` against the live OCI/Grok endpoint), this module
avoids depending on any provider-specific capability: structured output
uses Pydantic AI's own model-agnostic tool-call-based output extraction
(`output_type=[AxisTaskResult, DeferredToolRequests]` — one AXIS-authored
result model plus the framework's own deferred-call control type, so
exactly one *business* output tool is built), conversation state is plain
in-process `message_history` (no `previous_response_id`), limits use
Pydantic AI's own `UsageLimits` accounting plus a plain `asyncio.wait_for`
wall-clock timeout (no provider-native background/cancel), and there is no
reliance on parallel tool calls or provider-native compaction. The one
exception is `event_stream_handler` (a genuine, pinned Pydantic AI hook —
see `_stream_handler_for` below) used only for observability.

Phase 2 adds: a real Harness `Planning` capability (bound to an explicit,
task-scoped `InMemoryPlanStore` and passed per run-segment via
`agent.run(capabilities=[...])` — see `_planning_capabilities_for` below for
why), the `axis.task_control` capability (`axis_set_task_intent`,
`axis_prepare_effect`), a real Harness `ToolGuardrail` enforcing effect
gating/approval policy, and a real Harness `SystemReminders` capability.

**Planning-store lifetime.** `Planning(store=None)` (the harness default)
resolves a *fresh* `InMemoryPlanStore` on every `for_run()` — i.e. on every
run *segment*. An approval-required call ends its run segment early with a
`DeferredToolRequests` output; resuming is another `agent.run()` call,
another run segment. If `Planning` were built once with `store=None` and
reused, the plan would silently vanish across that resume. The fix used
here: `axis.planning.create_task_plan_store()` builds one explicit
`InMemoryPlanStore` per AXIS *task* (stored on `AxisRunDeps.plan_store`,
reset only by `next_run()`), and every `run_axis_task` call for that task —
initial or resumed — builds a *fresh* `Planning(store=deps.plan_store, ...)`
capability instance and passes it via `agent.run(..., capabilities=[...])`,
confirmed to accept per-run capabilities in the installed
`pydantic-ai-slim==2.40.0` source (`AbstractAgent.run`'s `capabilities`
parameter). Because the store itself is explicit and persists independently
of the `Planning` wrapper object, rebuilding the wrapper every call is safe
and simple — there is no need to keep one `Planning` instance alive across
calls, only one store.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterable, Dict, List, Optional, Tuple, Union

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ModelRetry, RunContext, RunUsage, capture_run_messages  # noqa: E402
from pydantic_ai.exceptions import (  # noqa: E402
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import AgentStreamEvent, ModelMessage, PartStartEvent, TextPart, ThinkingPart  # noqa: E402
from pydantic_ai.models.openai import OpenAIResponsesModel  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402
from pydantic_ai_harness.guardrails import GuardrailResult, ToolCallInfo, ToolGuardrail  # noqa: E402
from pydantic_ai_harness.system_reminders import Reminder, SystemReminders  # noqa: E402

from axis.capabilities.browser import build_browser_capability  # noqa: E402
from axis.capabilities.task_control import build_task_control_capability  # noqa: E402
from axis.config import AxisConfigError, Phase2ApprovalsConfig, load_axis_config  # noqa: E402
from axis.approvals import approval_required, derive_durable_approval_id, record_approval_request  # noqa: E402
from axis.effects import EffectLedger, effective_risk, requires_effect, unresolved_proposed_effects  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402
from axis.models import (  # noqa: E402
    ACCEPTANCE_UNRESOLVED,
    BLOCKING_AMBIGUITY,
    CONFIGURATION_ERROR,
    EFFECT_LIMIT_EXCEEDED,
    EFFECT_MISMATCH,
    EFFECT_REQUIRED,
    MODEL_ERROR,
    NO_MANAGED_TAB,
    PLAN_INCOMPLETE,
    PLAN_REQUIRED,
    PLAN_STEP_REQUIRED,
    PROVIDER_ERROR,
    REQUEST_LIMIT_EXCEEDED,
    TASK_INTENT_REQUIRED,
    TOKEN_LIMIT_EXCEEDED,
    TOOL_CALL_LIMIT_EXCEEDED,
    UNEXPECTED_ERROR,
    WALL_CLOCK_LIMIT_EXCEEDED,
    AxisRunDeps,
    AxisRunLimits,
    AxisRunState,
    AxisTaskResult,
)
from axis.planning import (  # noqa: E402
    build_planning_capability,
    create_task_plan_store,
    diff_and_emit_plan_events,
    read_plan_snapshot,
    required_items_incomplete,
    single_in_progress_task,
)
from browser_agent_tools import BrowserAgentTools  # noqa: E402
from provider_config import ProviderConfig, build_async_openai_client  # noqa: E402

DEFAULT_RETRIES = 3  # overridden by build_axis_agent(max_retries=...) — see axis.config's limits.max_retries
_error_logger = logging.getLogger("axis.errors")

AxisRunOutput = Union[AxisTaskResult, DeferredToolRequests]

# Permanent, cross-capability AXIS-level instructions ONLY. Everything about
# how to use a specific capability's tools — for the browser capability:
# discovering/switching tabs, observing before acting, using only refs from
# the latest observation, one interaction per browser_act, re-observing
# after navigation, when to wait/assert/diagnose — is that capability's own
# responsibility (BROWSER_AGENT_INSTRUCTIONS, contributed via
# axis.capabilities.browser) and must not be restated here.
AXIS_SAFETY_INSTRUCTIONS = """You are the Axis agent, with whole-browser access to every ordinary tab available.

Non-negotiable rules:
- Never invent a logical tab handle, identifier, URL, selector, or file path — use only values already given to you or produced by a tool you called.
- Do not report a task as completed until the requested outcome is actually verified — a successful action or wait is never proof by itself.
- You have no way to run raw RPC methods, arbitrary JavaScript, read cookies, change policy, modify headers, bypass CSP, control the extension, or send coordinate/mouse input — never ask for or claim any of these.
- Before any state-changing browser action (click/fill/press/select/check/uncheck/upload/drag, or closing a tab), call axis_set_task_intent, create a plan, and call axis_prepare_effect. Read-only tasks may skip this.
- Follow each available capability's own operating instructions for how to use its tools; this instruction does not repeat them.

Produce exactly one AxisTaskResult with status "completed", "needs_user_input", "failed", or "cancelled".
"""

_STATIC_REMINDER_TEXTS = (
    "Use the current plan and keep exactly one step in progress.",
    "Observe before element-targeted browser actions.",
    "Use only current refs for the correct tab.",
    "Navigation or tab changes can invalidate observations.",
    "A browser action or wait does not prove completion.",
    "Prepare an effect before a state-changing browser call.",
    "Approval does not replace scope authorization.",
    "Never blindly reissue a denied or outcome-unknown mutation. A diagnosed, "
    "retryable failure (e.g. a stale ref) is different: re-observe, prepare a "
    "fresh effect for the same plan step, and retry — do not abandon the step.",
    "Use browser_assert to satisfy acceptance criteria.",
    "Do not claim completion while required criteria or effects are unresolved.",
)


def build_model(config: ProviderConfig) -> Tuple[OpenAIResponsesModel, Any]:
    """Build the Phase-0-approved OCI/Grok model. Returns (model, client) —
    the caller owns closing `client` (see axis/cli.py's finally block)."""
    client = build_async_openai_client(config)
    provider = OpenAIProvider(openai_client=client)
    return OpenAIResponsesModel(config.model, provider=provider), client


def _completion_is_verified(state: AxisRunState) -> bool:
    """Phase 1 completion check: a passing browser_assert after any
    state-changing action; for a task that never changed state, a fresh
    observation is enough."""
    if state.verification_required:
        return state.assertion_passed_after_effect
    return state.observed_current_run or state.assertion_passed_after_effect


async def _phase2_completion_gate(deps: AxisRunDeps) -> Optional[str]:
    """Additional Phase 2 gates for `status="completed"`. Returns `None`
    when satisfied, else a `ModelRetry`-ready corrective message. A task
    that never called `axis_set_task_intent` (purely read-only) is exempt
    from every check here — Phase 1's `_completion_is_verified` already
    covers it."""
    control = deps.control_state
    intent = control.task_intent
    if intent is None:
        return None
    if any(a.blocking for a in intent.ambiguities):
        return f"{BLOCKING_AMBIGUITY}: a blocking ambiguity remains unresolved in the current Task Intent."

    ledger = EffectLedger(control.effects)
    unresolved = unresolved_proposed_effects(intent, ledger)
    if unresolved:
        return (
            f"{PLAN_INCOMPLETE}: proposed effect(s) {unresolved} are not yet resolved (succeeded/cancelled/"
            "superseded). Prepare and execute them, cancel their plan step, or revise the Task Intent to drop them."
        )

    if intent.proposed_effects:
        plan_items = await read_plan_snapshot(deps.plan_store) if hasattr(deps, "plan_store") else control.plan
        if not plan_items:
            return f"{PLAN_REQUIRED}: no plan exists."
        incomplete = (await required_items_incomplete(deps.plan_store) if hasattr(deps, "plan_store") else
                      [item for item in plan_items if item.status in ("pending", "in_progress")])
        if incomplete:
            task_ids = [item.task_id for item in incomplete]
            return f"{PLAN_INCOMPLETE}: plan step(s) not complete: {task_ids}."
        for effect in ledger.list_effects():
            if effect.status in (
                "prepared", "awaiting_approval", "approved", "starting", "executing",
                "executed_unverified", "unknown_after_crash", "reconciling",
                "manual_intervention_required", "failed_known_not_applied", "denied",
            ):
                task = next((p for p in plan_items if p.task_id == effect.plan_task_id), None)
                if task is None or task.status != "cancelled":
                    return f"{ACCEPTANCE_UNRESOLVED}: effect {effect.effect_key!r} is unresolved ({effect.status})."
    return None


def _criterion_required(intent, criterion_id: str) -> bool:
    criterion = next((c for c in intent.acceptance_criteria if c.criterion_id == criterion_id), None)
    return criterion is not None and criterion.required


def _field(item: Any, name: str) -> Any:
    """`item` is normally a validated object (e.g. `PlanStatusUpdate`) by
    the time a guard sees it, but must never crash this guard if it ever
    arrives as a plain dict instead (a tool's own argument type hint
    controls whether the framework actually constructs that object — see
    `axis.durability.runtime._DurablePlanningToolset.update_task_statuses`'s
    docstring for a concrete case where this used to not hold)."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _guard_task_status_update(deps: AxisRunDeps, name: str, args: Dict[str, Any], logger: RunEventLogger) -> GuardrailResult:
    ledger = EffectLedger(deps.control_state.effects)
    if name == "update_task_status":
        updates = [(args.get("task_id"), args.get("status"))]
    else:
        updates = [(_field(u, "task_id"), _field(u, "status")) for u in args.get("updates", [])]
    for task_id, status in updates:
        status_str = status.value if hasattr(status, "value") else str(status)
        record = ledger.get_for_task(task_id) if task_id else None
        if status_str == "completed":
            if record is not None and record.status not in (
                "succeeded", "reconciled_succeeded", "cancelled", "cancelled_before_execution", "superseded",
            ):
                return GuardrailResult.retry(
                    f"Plan step {task_id!r} cannot be completed yet: its effect is {record.status!r}, not resolved."
                )
        elif status_str == "cancelled" and record is not None:
            if record.status == "executing":
                return GuardrailResult.retry(f"Plan step {task_id!r} cannot be cancelled while its effect is executing.")
            if record.status in ("prepared", "awaiting_approval", "denied", "failed"):
                ledger.mark_cancelled(record.effect_id)
                logger.effect_cancelled(deps.run_id)
    return GuardrailResult.allow()


def _guard_plan_mutation(deps: AxisRunDeps, name: str, args: Dict[str, Any]) -> GuardrailResult:
    ledger = EffectLedger(deps.control_state.effects)
    if name == "remove_task":
        task_id = args.get("task_id")
        record = ledger.get_for_task(task_id) if task_id else None
        if record is not None and record.status in ("awaiting_approval", "executing", "executed_unverified"):
            return GuardrailResult.retry(f"Cannot remove plan step {task_id!r}: its effect is {record.status!r}.")
        return GuardrailResult.allow()
    # write_plan (bulk replace)
    items = args.get("items", [])
    surviving_in_progress = {
        item.id for item in items
        if (item.status.value if hasattr(item.status, "value") else str(item.status)) == "in_progress"
    }
    all_ids = {item.id for item in items}
    for effect in ledger.list_effects():
        if effect.status in ("awaiting_approval", "executing", "executed_unverified"):
            if effect.plan_task_id not in all_ids or effect.plan_task_id not in surviving_in_progress:
                return GuardrailResult.retry(
                    f"write_plan would drop or deactivate plan step {effect.plan_task_id!r}, which has an active effect."
                )
    return GuardrailResult.allow()


async def _guard_effectful_browser_call(
    ctx: RunContext[AxisRunDeps], tool: str, action: Optional[str], operation: Optional[str],
    args: Dict[str, Any], tool_call_id: str, approvals: Phase2ApprovalsConfig, logger: RunEventLogger,
) -> GuardrailResult:
    deps = ctx.deps
    control = deps.control_state
    ledger = EffectLedger(control.effects)
    intent = control.task_intent
    if intent is None:
        return GuardrailResult.block(f"{TASK_INTENT_REQUIRED}: call axis_set_task_intent before a state-changing browser action.")
    if any(a.blocking for a in intent.ambiguities):
        return GuardrailResult.block(f"{BLOCKING_AMBIGUITY}: resolve the blocking ambiguity before mutating.")

    if hasattr(deps, "plan_store"):
        plan_items = await read_plan_snapshot(deps.plan_store)
        control.plan = plan_items
    else:
        plan_items = control.plan
    active = [item for item in plan_items if item.status == "in_progress"]
    active_task = active[0] if len(active) == 1 else None
    if active_task is None:
        if not plan_items:
            return GuardrailResult.block(f"{PLAN_REQUIRED}: create a plan with write_plan before mutating.")
        return GuardrailResult.block(f"{PLAN_STEP_REQUIRED}: exactly one plan step must be in_progress before mutating.")

    effect = ledger.get_for_task(active_task.task_id)
    if effect is None or effect.status not in ("prepared", "awaiting_approval", "approved", "starting", "executing", "executed_unverified"):
        unresolved = set(unresolved_proposed_effects(intent, ledger))
        candidate = next(
            (p for p in intent.proposed_effects
             if p.effect_key in unresolved and tool in p.allowed_tools
             and (action is None or not p.allowed_actions or action in p.allowed_actions)),
            None,
        )
        # `retry` (ModelRetry) rather than `block` (SkipToolExecution): a plain
        # block's refusal text is just another tool result the model is free to
        # ignore and re-attempt identically (observed live: the same blocked
        # browser_act retried 3 times verbatim against this exact message).
        # ModelRetry carries the framework's own retry semantics and counts
        # against the agent's bounded retry budget, so persistent
        # non-compliance now fails the run cleanly instead of wandering for
        # several more turns before giving up on its own.
        if candidate is not None:
            return GuardrailResult.retry(
                f"{EFFECT_REQUIRED}: call axis_prepare_effect(effectKey={candidate.effect_key!r}, "
                f"planTaskId={active_task.task_id!r}) before retrying this {tool} call."
            )
        return GuardrailResult.retry(
            f"{EFFECT_REQUIRED}: no proposed effect in the current Task Intent covers {tool!r} for plan step "
            f"{active_task.task_id!r}. Call axis_set_task_intent with a proposed_effects entry whose "
            f"allowed_tools includes {tool!r}, then axis_prepare_effect(effectKey=<that effect_key>, "
            f"planTaskId={active_task.task_id!r})."
        )

    def reject_before_dispatch(message: str) -> GuardrailResult:
        """Persist the conservative outcome for a guarded mutation attempt."""
        current = ledger.get(effect.effect_id)
        ledger.mark_outcome(effect.effect_id, "known_not_applied", current.execution_stage)
        return GuardrailResult.block(message)

    pending = control.approval_state.requests_by_tool_call_id
    if effect.status == "awaiting_approval" and tool_call_id not in pending and not ctx.tool_call_approved:
        # This is an unexpected second mutation while the original deferred
        # call remains authoritative.  Do not rewrite the original effect's
        # state or lose its pending request.
        return GuardrailResult.block("Only one effectful deferred call may be active at a time.")

    if tool not in effect.allowed_tools:
        return reject_before_dispatch(f"{EFFECT_MISMATCH}: {tool!r} is not permitted by the prepared effect.")
    call_action = action or operation
    if call_action is not None and effect.allowed_actions and call_action not in effect.allowed_actions:
        return reject_before_dispatch(f"{EFFECT_MISMATCH}: {call_action!r} is not permitted by the prepared effect.")

    if effect.browser_mutation_count >= effect.max_browser_mutations:
        return reject_before_dispatch(f"{EFFECT_LIMIT_EXCEEDED}: the prepared effect's mutation budget is exhausted.")

    model_supplied_session = args.get("browserSessionId")
    if effect.target_browser_session_id is not None and model_supplied_session != effect.target_browser_session_id:
        return reject_before_dispatch(f"{EFFECT_MISMATCH}: this effect is bound to a different tab.")
    if effect.target_browser_session_id is None and isinstance(model_supplied_session, str):
        # The durable opaque handle is part of the scheduled execution
        # record. The activity still validates it against its trusted tab
        # registry before dispatching, but reconciliation needs the intended
        # target even when the response is lost after dispatch.
        ledger._set(effect.effect_id, target_browser_session_id=model_supplied_session)

    risk = effective_risk(tool, effect.risk, action=action, operation=operation)

    if risk == "destructive_high_impact":
        if not control.observed_current_run:
            return reject_before_dispatch(f"{ACCEPTANCE_UNRESOLVED}: observe the current page before a destructive action.")
        required_ids = [cid for cid in effect.acceptance_criterion_ids if _criterion_required(intent, cid)]
        if not required_ids:
            return reject_before_dispatch(f"{ACCEPTANCE_UNRESOLVED}: this effect has no required postcondition.")

    ledger.mark_authorized(effect.effect_id)
    if not approval_required(approvals, risk):
        ledger.mark_dispatch_scheduled(effect.effect_id)
        return GuardrailResult.allow()

    # policy == "require"
    if ctx.tool_call_approved:
        ledger.mark_dispatch_scheduled(effect.effect_id)
        return GuardrailResult.allow()
    if any(not request.resolved for call_id, request in pending.items() if call_id != tool_call_id):
        return GuardrailResult.block("Only one effectful deferred call may be active at a time.")
    request = record_approval_request(
        control.approval_state, tool_call_id=tool_call_id, effect_id=effect.effect_id,
        risk=risk, summary=effect.summary,
        approval_id=(derive_durable_approval_id(deps.job_id, tool_call_id, effect.effect_id)
                     if getattr(deps, "job_id", None) else None),
    )
    ledger.mark_awaiting_approval(effect.effect_id, request.approval_id)
    logger.effect_approval_requested(deps.run_id, risk)
    return GuardrailResult.approve()


_guard_debug_logger = logging.getLogger("axis.debug.guard")


def build_axis_guard(approvals: Phase2ApprovalsConfig, logger: RunEventLogger):
    async def guard(ctx: RunContext[AxisRunDeps], call: ToolCallInfo) -> GuardrailResult:
        name = call.name
        args = dict(call.args)

        if name in ("axis_set_task_intent", "axis_prepare_effect"):
            return GuardrailResult.allow()
        if name in ("update_task_status", "update_task_statuses"):
            verdict = _guard_task_status_update(ctx.deps, name, args, logger)
        elif name in ("write_plan", "remove_task"):
            verdict = _guard_plan_mutation(ctx.deps, name, args)
        elif name in ("read_plan", "add_task"):
            verdict = GuardrailResult.allow()
        else:
            action = args.get("action")
            operation = args.get("operation")
            if not requires_effect(name, action=action, operation=operation):
                verdict = GuardrailResult.allow()
            else:
                verdict = await _guard_effectful_browser_call(
                    ctx, name, action, operation, args, call.tool_call_id, approvals, logger,
                )

        # Every guard-authored string here is a fixed template with only
        # already-safe values interpolated (task/effect ids the model
        # itself assigned, risk labels, status enums) — never raw page
        # content or a bridge identifier — so this is safe to log in full.
        # This is what actually explains a tool exhausting its retries
        # (`UnexpectedModelBehavior: Tool '...' exceeded max retries`): the
        # guard's own reason for repeatedly rejecting the call.
        if verdict.action != "allow":
            _guard_debug_logger.info("guard %s(%s): %s", name, verdict.action, verdict.message)
        return verdict

    return guard


def _build_system_reminders(interval: int, max_fires: int) -> SystemReminders:
    return SystemReminders(reminders=[Reminder(content=text, interval=interval, max_fires=max_fires) for text in _STATIC_REMINDER_TEXTS])


def build_axis_agent(
    model: Any, event_logger: Optional[RunEventLogger] = None, max_retries: int = DEFAULT_RETRIES,
    approvals: Optional[Phase2ApprovalsConfig] = None, reminder_interval: int = 5, reminder_max_fires: int = 4,
    *, deps_type: Any = AxisRunDeps, browser_capability: Any = None,
    planning_capability: Any = None, task_control_capability: Any = None,
    tool_guardrail: Any = None, system_reminders: Any = None,
    extra_capabilities: tuple[Any, ...] = (), name: str = "axis_agent",
) -> Agent[AxisRunDeps, AxisRunOutput]:
    """Build the one authoritative Axis root agent. `model` may be a real
    provider model or a Pydantic AI test model (TestModel/FunctionModel).
    The eight browser tools (including `browser_tabs`) are registered
    exactly once, via the browser Capability's `capabilities=[...]` — never
    also passed as `tools=`. `Planning` is intentionally NOT included here —
    see the module docstring; it is passed per run segment by
    `run_axis_task` via `agent.run(capabilities=[...])`."""
    logger = event_logger or RunEventLogger()
    approval_policy = approvals or Phase2ApprovalsConfig()
    browser_capability = browser_capability or build_browser_capability(logger)
    task_control_capability = task_control_capability or build_task_control_capability(logger)
    tool_guardrail = tool_guardrail or ToolGuardrail(guard=build_axis_guard(approval_policy, logger), tools=None)
    system_reminders = system_reminders or _build_system_reminders(reminder_interval, reminder_max_fires)
    capabilities = [browser_capability, task_control_capability]
    if planning_capability is not None:
        capabilities.append(planning_capability)
    capabilities.extend([tool_guardrail, system_reminders, *extra_capabilities])

    agent: Agent[AxisRunDeps, AxisRunOutput] = Agent(
        model=model,
        deps_type=deps_type,
        output_type=[AxisTaskResult, DeferredToolRequests],
        instructions=AXIS_SAFETY_INSTRUCTIONS,
        capabilities=capabilities,
        retries=max_retries,
        name=name,
    )

    @agent.output_validator
    async def _verify_completion_is_earned(ctx: RunContext[AxisRunDeps], output: AxisRunOutput) -> AxisRunOutput:
        if isinstance(output, DeferredToolRequests):
            return output
        if output.status == "completed":
            if not _completion_is_verified(ctx.deps.control_state):
                raise ModelRetry(
                    "status='completed' requires a passing browser_assert after your most recent "
                    "state-changing action (click/fill/press/select/check/uncheck/upload/drag), or a "
                    "fresh browser_observe for a read-only task — a successful action or wait alone is "
                    "not proof of the outcome."
                )
            gate_message = await _phase2_completion_gate(ctx.deps)
            if gate_message:
                raise ModelRetry(gate_message)
        return output

    @agent.instructions
    def _tell_initial_tab(ctx: RunContext[AxisRunDeps]) -> str:
        if ctx.deps.initial_tab_handle:
            return (
                f"Call browser_tabs (operation='list') to see currently available tabs. You started "
                f"this run with {ctx.deps.initial_tab_handle!r} focused, but that is only a starting "
                f"point — use whichever tab(s) the task actually requires."
            )
        return "Call browser_tabs (operation='list') to see currently available tabs before acting."

    return agent


def bind_axis_run_deps(
    browser_tools: BrowserAgentTools, *, job_id: Optional[str] = None, conversation_id: Optional[str] = None,
    event_logger: Optional[RunEventLogger] = None,
) -> Tuple[Optional[AxisRunDeps], Optional[Dict[str, Any]]]:
    """Refresh the whole-browser tab registry and produce trusted
    `AxisRunDeps` for this session, with a fresh `AxisRunState` and a fresh,
    task-scoped Planning store. Returns `(deps, None)` on success or
    `(None, error_dict)` only if the bridge itself is unreachable or reports
    zero tabs at all."""
    logger = event_logger or RunEventLogger()
    refreshed = browser_tools.refresh_tabs()
    if not refreshed["ok"]:
        return None, refreshed["error"]
    tabs = refreshed["data"]["tabs"]
    if not tabs:
        return None, {"code": NO_MANAGED_TAB, "message": "The browser has no tabs available.", "retryable": True}
    initial_handle = next((t["browserSessionId"] for t in tabs if t.get("active")), tabs[0]["browserSessionId"])
    run_id = _new_run_id()
    deps = AxisRunDeps(
        run_id=run_id,
        browser_tools=browser_tools,
        plan_store=create_task_plan_store(logger, run_id),
        run_state=AxisRunState(),
        initial_tab_handle=initial_handle,
        job_id=job_id,
        conversation_id=conversation_id,
    )
    return deps, None


def no_managed_tab_result(error: Dict[str, Any]) -> AxisTaskResult:
    """The typed failure to report when the browser has no tabs at all —
    i.e. before an `AxisRunDeps` can even be constructed."""
    return AxisTaskResult(
        status="failed",
        summary=error.get("message", "No browser tabs are available."),
        error_code=NO_MANAGED_TAB,
        retryable=True,
    )


def next_run(deps: AxisRunDeps, event_logger: Optional[RunEventLogger] = None) -> AxisRunDeps:
    """A fresh `AxisRunDeps` for a new AXIS *task* against the same bound
    `BrowserAgentTools` instance (and its tab registry). Everything
    task-scoped — `run_id`, `run_state`, the Planning store, Task Intent,
    the effect ledger, approval state, and the cached plan snapshot — is
    rebuilt from scratch (a brand-new `AxisRunDeps`, never
    `dataclasses.replace` of the mutable sub-objects, which would carry
    them forward by reference). Verification/intent/effects/approvals from
    a previous task must never leak into this one."""
    logger = event_logger or RunEventLogger()
    run_id = _new_run_id()
    return AxisRunDeps(
        run_id=run_id,
        browser_tools=deps.browser_tools,
        plan_store=create_task_plan_store(logger, run_id),
        run_state=AxisRunState(),
        initial_tab_handle=deps.initial_tab_handle,
        job_id=deps.job_id,
        conversation_id=deps.conversation_id,
    )


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# The pinned Pydantic AI version's UsageLimitExceeded carries no structured
# field naming which limit was hit (see pydantic_ai/usage.py's
# check_before_request/check_before_tool_call/check_tokens — each raises
# UsageLimitExceeded with only a human-readable f-string). Classifying by
# the limit name it names in that message is the only option available
# against this pinned version; each raise site's message always contains
# its limit's exact attribute name, so this is a stable (if unofficial)
# signal, not a guess at unrelated wording.
_LIMIT_MESSAGE_MARKERS = (
    ("tool_calls_limit", TOOL_CALL_LIMIT_EXCEEDED, "tool_calls"),
    ("request_limit", REQUEST_LIMIT_EXCEEDED, "requests"),
    ("total_tokens_limit", TOKEN_LIMIT_EXCEEDED, "total_tokens"),
    ("input_tokens_limit", TOKEN_LIMIT_EXCEEDED, "input_tokens"),
    ("output_tokens_limit", TOKEN_LIMIT_EXCEEDED, "output_tokens"),
    ("per_request_input_tokens_limit", TOKEN_LIMIT_EXCEEDED, "input_tokens"),
)


_USAGE_ATTR_TO_LIMIT_FIELD = {
    "requests": "max_requests",
    "tool_calls": "max_tool_calls",
    "input_tokens": "max_total_tokens",
    "output_tokens": "max_total_tokens",
    "total_tokens": "max_total_tokens",
}
_USAGE_ATTR_TO_KIND = {"requests": "requests", "tool_calls": "tool_calls"}


def _classify_usage_limit_error(exc: UsageLimitExceeded, limits: AxisRunLimits, usage: RunUsage) -> Tuple[str, Dict[str, Any]]:
    """Map a caught UsageLimitExceeded to exactly one precise error code
    plus a safe diagnostic dict ({"kind", "used", "limit"}) — never the raw
    exception string."""
    message = str(exc)
    for marker, code, usage_attr in _LIMIT_MESSAGE_MARKERS:
        if marker in message:
            limit_field = _USAGE_ATTR_TO_LIMIT_FIELD[usage_attr]
            diagnostic = {
                "kind": _USAGE_ATTR_TO_KIND.get(usage_attr, "tokens"),
                "used": getattr(usage, usage_attr, None),
                "limit": getattr(limits, limit_field),
            }
            return code, diagnostic
    return TOKEN_LIMIT_EXCEEDED, {"kind": "unknown", "used": None, "limit": None}


def _stream_handler_for(run_id: str, logger: RunEventLogger, show_text: bool):
    async def handler(ctx: RunContext[AxisRunDeps], events: AsyncIterable[AgentStreamEvent]) -> None:
        logger.model_request_started(run_id)
        start = time.perf_counter()
        async for event in events:
            if isinstance(event, PartStartEvent):
                part = event.part
                if show_text and isinstance(part, TextPart) and part.content:
                    logger.model_text(run_id, part.content)
                elif isinstance(part, ThinkingPart) and part.content:
                    logger.model_reasoning(run_id, part.content)
        logger.model_response_received(run_id, _elapsed_ms(start))

    return handler


async def run_axis_task(
    agent: Agent[AxisRunDeps, AxisRunOutput],
    deps: AxisRunDeps,
    user_prompt: Optional[str],
    *,
    message_history: Optional[List[ModelMessage]] = None,
    deferred_tool_results: Optional[DeferredToolResults] = None,
    limits: Optional[AxisRunLimits] = None,
    event_logger: Optional[RunEventLogger] = None,
    show_model_text: bool = True,
    enable_live_model_events: bool = False,
) -> Tuple[AxisRunOutput, List[ModelMessage]]:
    """Run one *run segment* — the initial call or one approval resume — to
    either a typed `AxisTaskResult` or a framework `DeferredToolRequests`
    (never raises; every provider/model/budget/timeout/cancellation failure
    becomes a `status='failed'`/`'cancelled'` `AxisTaskResult`). The caller
    (`axis.cli`) is responsible for looping resume segments — via
    `deferred_tool_results` built from `axis.approvals.resolve_approval` —
    until a non-deferred result comes back; that whole loop is one AXIS
    *task*. `Planning` is passed per call via `capabilities=[...]`, bound to
    `deps.plan_store` (see `axis.agent`'s module docstring), so the plan
    survives every resume within the task."""
    logger = event_logger or RunEventLogger()

    if limits is None:
        try:
            limits = load_axis_config().limits
        except AxisConfigError:
            _error_logger.error("Invalid axis.yaml configuration for run %s.", deps.run_id)
            logger.run_failed(deps.run_id)
            return AxisTaskResult(
                status="failed",
                summary="Execution limits are misconfigured; check axis.yaml.",
                error_code=CONFIGURATION_ERROR, retryable=False,
            ), []

    start = time.perf_counter()
    logger.run_started(deps.run_id)
    usage = RunUsage()
    planning_capability = build_planning_capability(deps.plan_store)

    captured: List[ModelMessage] = []
    try:
        with capture_run_messages() as messages:
            captured = messages
            run_kwargs: Dict[str, Any] = dict(
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                usage_limits=limits.to_usage_limits(),
                usage=usage,
                capabilities=[planning_capability],
            )
            if enable_live_model_events:
                run_kwargs["event_stream_handler"] = _stream_handler_for(deps.run_id, logger, show_model_text)
            run_coro = agent.run(user_prompt, **run_kwargs)
            if limits.max_wall_clock_seconds is None:
                result = await run_coro
            else:
                result = await asyncio.wait_for(run_coro, timeout=limits.max_wall_clock_seconds)
    except asyncio.TimeoutError:
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        logger.limit_reached(deps.run_id, "wall_clock", limit=limits.max_wall_clock_seconds)
        return AxisTaskResult(
            status="failed",
            summary=f"The task did not complete within {limits.max_wall_clock_seconds:.0f}s.",
            error_code=WALL_CLOCK_LIMIT_EXCEEDED, retryable=True,
        ), captured
    except UsageLimitExceeded as exc:
        code, diagnostic = _classify_usage_limit_error(exc, limits, usage)
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        logger.limit_reached(deps.run_id, diagnostic["kind"], diagnostic.get("used"), diagnostic.get("limit"))
        return AxisTaskResult(
            status="failed", summary=f"Execution limit exceeded: {diagnostic['kind']}.",
            error_code=code, retryable=True,
        ), captured
    except (ModelHTTPError, ModelAPIError) as exc:
        _error_logger.error("Provider error during run %s: %s.", deps.run_id, type(exc).__name__)
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return AxisTaskResult(
            status="failed", summary="The model provider returned an error.",
            error_code=PROVIDER_ERROR, retryable=True,
        ), captured
    except UnexpectedModelBehavior as exc:
        _error_logger.error("Unexpected model behavior during run %s: %s.", deps.run_id, type(exc).__name__)
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return AxisTaskResult(
            status="failed", summary="The model did not produce a valid, verified result.",
            error_code=MODEL_ERROR, retryable=False,
        ), captured
    except asyncio.CancelledError:
        logger.run_cancelled(deps.run_id, _elapsed_ms(start))
        return AxisTaskResult(status="cancelled", summary="The task was cancelled."), captured
    except Exception as exc:  # noqa: BLE001 - this boundary must never raise
        _error_logger.error("Unexpected error during run %s: %s.", deps.run_id, type(exc).__name__)
        logger.run_failed(deps.run_id, _elapsed_ms(start))
        return AxisTaskResult(
            status="failed", summary="An unexpected internal error occurred.",
            error_code=UNEXPECTED_ERROR, retryable=False,
        ), captured

    deps.last_plan_snapshot = await diff_and_emit_plan_events(logger, deps.run_id, deps.last_plan_snapshot, deps.plan_store)
    if deps.last_plan_snapshot != deps.control_state.plan:
        deps.control_state.plan = list(deps.last_plan_snapshot)
        deps.control_state.plan_revision += 1

    if isinstance(result.output, DeferredToolRequests):
        logger.run_completed(deps.run_id, _elapsed_ms(start))
        return result.output, result.all_messages()

    logger.usage(deps.run_id, usage.requests, usage.tool_calls, usage.total_tokens)
    logger.run_completed(deps.run_id, _elapsed_ms(start))
    return result.output, result.all_messages()
