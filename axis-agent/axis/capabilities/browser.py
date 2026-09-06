"""The real Pydantic AI Capability wrapping the eight frozen browser tools
(v2 contract: the original seven semantic tools plus ``browser_tabs``).

Every tool is built directly from `browser_agent_tools.get_tool_definitions()`
via `Tool.from_schema` — no schema is duplicated, retyped, or regenerated
from a simplified wrapper signature, so the frozen contract at
`contracts/browser_tool_contract_v2.json` cannot drift because of this file.

AXIS has whole-browser, multi-tab access: there is no single trusted
`browserSessionId` this layer forces onto every call anymore. Instead, every
tool call's handle is validated against `BrowserAgentTools`'s own trusted
tab registry (`_require_tab`, called inside every tool method) — a
fabricated, stale, or closed handle is rejected there with `TAB_NOT_FOUND`/
`TAB_CLOSED`, which is what actually enforces the trust boundary now. This
layer only adds:

1. Tool-level timing/success events and safe, redacted failure handling.
2. `AxisRunDeps.run_state` updates from successful tool results, so the
   root agent's completion check (`axis.agent`) can tell a verified outcome
   from a merely-attempted one.
3. Tab-lifecycle / observation events (tab created/activated/closed,
   observation created/invalidated, assertion result, evidence saved) for
   live CLI observability (`axis.events`/`axis.cli`).

Everything else — schemas, validation, scope checks, redaction, error
envelopes, the actual tab registry — is exactly what `browser_agent_tools.py`
already does; this file never reimplements any of it.
"""
from __future__ import annotations

import logging
import inspect
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Union

AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai import RunContext, Tool  # noqa: E402
from pydantic_ai.capabilities import Capability  # noqa: E402

from axis.effects import criterion_matches_assertion, requires_effect  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402
from axis.models import AxisRunDeps, TOOL_EXECUTION_ERROR  # noqa: E402
from browser_agent_tools import (  # noqa: E402
    BROWSER_AGENT_INSTRUCTIONS,
    get_tool_definitions,
    get_tool_handlers,
)

CAPABILITY_ID = "axis.browser"
CAPABILITY_DESCRIPTION = (
    "The eight Axis browser tools (tabs, observe, act, navigate, wait, assert, "
    "capture_evidence, diagnose) for driving every ordinary tab in the whole browser."
)

_SESSION_ID_FIELD = "browserSessionId"
_error_logger = logging.getLogger("axis.errors")

# browser_act actions that change page/business state and therefore require
# a later passing browser_assert before a task can be reported "completed".
# hover/scroll are excluded — they don't themselves assert a business
# outcome, so completion for a task that only ever hovered/scrolled falls
# back to the "observed_current_run" path instead.
_STATE_CHANGING_ACT_ACTIONS = frozenset({"click", "fill", "press", "select", "check", "uncheck", "upload", "drag"})


def _tab_handle_of(kwargs: Dict[str, Any], result: Dict[str, Any]) -> Optional[str]:
    handle = result.get("browserSessionId") if isinstance(result, dict) else None
    if isinstance(handle, str) and handle:
        return handle
    value = kwargs.get(_SESSION_ID_FIELD)
    return value if isinstance(value, str) else None


def _update_run_state_and_emit(
    tool_name: str, kwargs: Dict[str, Any], result: Dict[str, Any], deps: AxisRunDeps, logger: RunEventLogger,
) -> None:
    if not isinstance(result, dict) or not result.get("ok"):
        return
    run_id = deps.run_id
    state = deps.run_state
    handle = _tab_handle_of(kwargs, result)
    data = result.get("data") or {}

    if tool_name == "browser_observe":
        state.observed_current_run = True
        if handle:
            logger.observation_created(run_id, handle)
    elif tool_name == "browser_navigate":
        # A navigation invalidates any earlier assertion proof as well as
        # any earlier observation — an assert that passed against the
        # previous page is not evidence about the new one. It does NOT
        # clear verification_required: if an earlier state-changing action
        # on *this* page still hasn't been verified, navigating away
        # doesn't retroactively excuse that.
        state.observed_current_run = False
        state.assertion_passed_after_effect = False
        if handle:
            logger.observation_invalidated(run_id, handle, "navigation")
    elif tool_name == "browser_act":
        if kwargs.get("action") in _STATE_CHANGING_ACT_ACTIONS:
            state.verification_required = True
            state.assertion_passed_after_effect = False
        if handle and data.get("observationInvalidated"):
            logger.observation_invalidated(run_id, handle, "action")
    elif tool_name == "browser_assert":
        passed = data.get("passed") is True
        if passed:
            state.assertion_passed_after_effect = True
        if handle:
            logger.assertion_result(run_id, passed, handle)
    elif tool_name == "browser_capture_evidence":
        if data.get("saved") and data.get("path"):
            logger.evidence_saved(run_id, data.get("path"))
    elif tool_name == "browser_tabs":
        operation = kwargs.get("operation")
        new_handle = data.get("browserSessionId")
        if operation == "create" and new_handle:
            logger.tab_created(run_id, new_handle)
        elif operation == "activate" and new_handle:
            logger.tab_activated(run_id, new_handle)
        elif operation == "close" and new_handle:
            logger.tab_closed(run_id, new_handle)


def _mark_effect_started_if_applicable(tool_name: str, kwargs: Dict[str, Any], deps: AxisRunDeps, logger: RunEventLogger) -> None:
    """Phase 2: the browser wrapper — not the ToolGuardrail — owns the
    "handler entered" transition, since only this layer actually calls the
    real handler. A ``None`` active effect here means the guardrail was
    bypassed (e.g. a standalone Phase 1.2 test exercising this capability
    directly, with no Task Intent/effect ever prepared) — silently do
    nothing rather than fail a call that was never gated in the first
    place."""
    if not requires_effect(tool_name, action=kwargs.get("action"), operation=kwargs.get("operation")):
        return
    effect = deps.effect_ledger.get_active()
    if effect is None:
        return
    deps.effect_ledger.mark_started(effect.effect_id)
    logger.effect_started(deps.run_id)


def _apply_effect_outcome(tool_name: str, kwargs: Dict[str, Any], result: Dict[str, Any], deps: AxisRunDeps, logger: RunEventLogger) -> None:
    """Phase 2: after the handler returns (success or safe failure),
    transition the active effect. Increments no mutation count itself
    (that already happened in ``mark_started``, at handler entry) — this
    only resolves this attempt to executed-unverified or failed."""
    if requires_effect(tool_name, action=kwargs.get("action"), operation=kwargs.get("operation")):
        effect = deps.effect_ledger.get_active()
        if effect is not None and effect.status == "executing":
            if isinstance(result, dict) and result.get("ok"):
                trusted_session = _tab_handle_of(kwargs, result)
                if trusted_session:
                    deps.effect_ledger.mark_executed_unverified(effect.effect_id, trusted_session)
                    logger.effect_executed_unverified(deps.run_id)
            else:
                deps.effect_ledger.mark_failed(effect.effect_id)
                logger.effect_failed(deps.run_id)
        return
    if tool_name == "browser_assert" and isinstance(result, dict) and result.get("ok"):
        _check_acceptance(kwargs, result, deps, logger)


def _check_acceptance(kwargs: Dict[str, Any], result: Dict[str, Any], deps: AxisRunDeps, logger: RunEventLogger) -> None:
    """Phase 2 acceptance matching: a criterion passes only through a
    matching, successful ``browser_assert`` against the effect's own bound
    (trusted, runtime-resolved) tab — never through observation, wait, a
    mutating action's own result, or model say-so."""
    effect = deps.effect_ledger.get_active()
    if effect is None or effect.status != "executed_unverified":
        return
    intent = deps.task_intent
    if intent is None:
        return
    trusted_session = _tab_handle_of(kwargs, result)
    if not trusted_session:
        return
    data = result.get("data") or {}
    passed = data.get("passed") is True
    matched_any = False
    for criterion_id in effect.acceptance_criterion_ids:
        if criterion_id in effect.passed_criterion_ids:
            continue
        criterion = next((c for c in intent.acceptance_criteria if c.criterion_id == criterion_id), None)
        if criterion is None:
            continue
        if passed and criterion_matches_assertion(criterion, kwargs, effect=effect, trusted_session_id=trusted_session):
            deps.effect_ledger.mark_acceptance_passed(effect.effect_id, criterion_id)
            logger.acceptance_passed(deps.run_id, criterion_id)
            matched_any = True
    if not matched_any:
        logger.acceptance_failed(deps.run_id)
        return
    updated = deps.effect_ledger.get(effect.effect_id)
    required_ids = {
        c.criterion_id for c in intent.acceptance_criteria
        if c.required and c.criterion_id in updated.acceptance_criterion_ids
    }
    if required_ids and required_ids.issubset(set(updated.passed_criterion_ids)):
        deps.effect_ledger.mark_succeeded(effect.effect_id)
        logger.effect_succeeded(deps.run_id)


def _safe_tool_failure(trusted_session_id: Optional[str]) -> Dict[str, Any]:
    # Never echo the raw exception text here — full exception details go to
    # the log below (type only, no message, no arguments, no traceback).
    return {
        "ok": False,
        "browserSessionId": trusted_session_id,
        "data": None,
        "error": {
            "code": TOOL_EXECUTION_ERROR,
            "message": "The browser tool failed unexpectedly.",
            "retryable": True,
            "diagnostic": None,
        },
    }


BrowserHandlerAdapter = Callable[
    [str, RunContext[Any], Dict[str, Any]], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]
]


def _make_tool_function(
    tool_name: str, event_logger: RunEventLogger, *, handler_adapter: Optional[BrowserHandlerAdapter],
    persist_in_handler: bool, catch_exceptions: bool,
) -> Callable[..., Any]:
    if handler_adapter is None:
        def call_sync(ctx: RunContext[AxisRunDeps], **kwargs: Any) -> Dict[str, Any]:
            handler = get_tool_handlers(ctx.deps.browser_tools)[tool_name]
            requested_handle = kwargs.get(_SESSION_ID_FIELD)
            event_logger.tool_started(
                ctx.deps.run_id, tool_name,
                tab_handle=requested_handle if isinstance(requested_handle, str) else None,
            )
            start = time.perf_counter()
            if persist_in_handler:
                _mark_effect_started_if_applicable(tool_name, kwargs, ctx.deps, event_logger)
            try:
                result = handler(kwargs)
                success = isinstance(result, dict) and bool(result.get("ok"))
            except Exception as exc:  # noqa: BLE001
                if not catch_exceptions:
                    raise
                _error_logger.error("Browser tool %s raised %s.", tool_name, type(exc).__name__)
                success = False
                result = _safe_tool_failure(requested_handle if isinstance(requested_handle, str) else None)
                if persist_in_handler:
                    _apply_effect_outcome(tool_name, kwargs, result, ctx.deps, event_logger)
            else:
                if persist_in_handler:
                    _update_run_state_and_emit(tool_name, kwargs, result, ctx.deps, event_logger)
                    _apply_effect_outcome(tool_name, kwargs, result, ctx.deps, event_logger)
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            event_logger.tool_completed(
                ctx.deps.run_id, tool_name, duration_ms, success,
                tab_handle=_tab_handle_of(kwargs, result),
            )
            return result

        return call_sync

    async def call_handler(ctx: RunContext[AxisRunDeps], **kwargs: Any) -> Dict[str, Any]:
        requested_handle = kwargs.get(_SESSION_ID_FIELD)
        event_logger.tool_started(ctx.deps.run_id, tool_name, tab_handle=requested_handle if isinstance(requested_handle, str) else None)
        start = time.perf_counter()
        if persist_in_handler:
            _mark_effect_started_if_applicable(tool_name, kwargs, ctx.deps, event_logger)
        try:
            result = handler_adapter(tool_name, ctx, kwargs)
            if inspect.isawaitable(result):
                result = await result
            success = isinstance(result, dict) and bool(result.get("ok"))
        except Exception as exc:  # noqa: BLE001 - a handler bug must never crash the agent run
            if not catch_exceptions:
                raise
            # Exception TYPE only — never the message, arguments, or a
            # traceback (which could contain page content, URLs, or values
            # a caller passed in).
            _error_logger.error("Browser tool %s raised %s.", tool_name, type(exc).__name__)
            success = False
            result = _safe_tool_failure(requested_handle if isinstance(requested_handle, str) else None)
            if persist_in_handler:
                _apply_effect_outcome(tool_name, kwargs, result, ctx.deps, event_logger)
        else:
            if persist_in_handler:
                _update_run_state_and_emit(tool_name, kwargs, result, ctx.deps, event_logger)
                _apply_effect_outcome(tool_name, kwargs, result, ctx.deps, event_logger)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        event_logger.tool_completed(ctx.deps.run_id, tool_name, duration_ms, success, tab_handle=_tab_handle_of(kwargs, result))
        return result

    return call_handler


def build_browser_capability(
    event_logger: Optional[RunEventLogger] = None, *, handler_adapter: Optional[BrowserHandlerAdapter] = None,
    activity_metadata: Optional[Callable[[str], Dict[str, Any]]] = None,
    persist_in_handler: bool = True, catch_exceptions: bool = True,
) -> Capability[AxisRunDeps]:
    """Build the real Pydantic AI Capability bundling the eight frozen
    browser tools and their operating instructions. Every tool is
    `sequential=True`: AXIS browser operations share the tab registry and
    per-tab observation/ref state and must not execute concurrently within
    one run (this does not stop one task from using multiple tabs — it
    just means the tool calls that do so run one at a time, in order)."""
    logger = event_logger or RunEventLogger()
    definitions = get_tool_definitions()  # frozen 8-tool v2 contract, unmodified
    tools = []
    for entry in definitions:
        tool = Tool.from_schema(
            function=_make_tool_function(
                entry["function"]["name"], logger, handler_adapter=handler_adapter,
                persist_in_handler=persist_in_handler, catch_exceptions=catch_exceptions,
            ),
            name=entry["function"]["name"],
            description=entry["function"]["description"],
            json_schema=entry["function"]["parameters"],
            takes_ctx=True,
            sequential=True,
        )
        if activity_metadata:
            tool.metadata = activity_metadata(entry["function"]["name"])
        tools.append(tool)
    return Capability(
        id=CAPABILITY_ID,
        description=CAPABILITY_DESCRIPTION,
        instructions=BROWSER_AGENT_INSTRUCTIONS,
        tools=tools,
    )
