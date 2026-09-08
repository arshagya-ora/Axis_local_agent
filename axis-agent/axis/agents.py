"""Planner and navigator construction, plus the guarded browser tool set.

Two agents share one model instance. The planner has no tools; the navigator
gets a small explicit Python list of the browser tools it actually needs. The
guard in this module is what makes ``BrowserRuntime`` the executor for every
call: budgets, pause/cancel, approval, interruption, and the bounded
``ActionRecord`` all happen here, in front of the unmodified tool functions in
``browser_tools``.
"""

from __future__ import annotations

import dataclasses
import functools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import ToolSearch
from pydantic_ai.models import Model

import browser_tools as bt
from browser_tools import BrowserRuntime

from .models import ActionRecord, AxisConfig, NavigatorOutcome, PlanDecision, clip

# Ordinary navigator tool set. capture/diagnose are deliberately absent.
DEFAULT_TOOL_FUNCTIONS = (
    bt.browser_tabs,
    bt.browser_observe,
    bt.browser_act,
    bt.browser_navigate,
    bt.browser_wait,
    bt.browser_assert,
)
OPTIONAL_TOOL_FUNCTIONS = {
    "browser_capture_evidence": bt.browser_capture_evidence,
    "browser_diagnose": bt.browser_diagnose,
    "browser_downloads": bt.browser_downloads,
}

# Actions that change page state and therefore need later verification.
MUTATING_ACTIONS = frozenset({
    "click", "fill", "type_sequentially", "press", "select", "check", "uncheck",
    "upload", "drag", "accept_dialog", "dismiss_dialog",
})
MUTATING_TOOLS = frozenset({"browser_act", "browser_navigate"})
# Failures whose real effect on the page is unknown: never repeat these.
UNKNOWN_OUTCOME_CODES = frozenset({"TIMEOUT", "BRIDGE_UNAVAILABLE", "BRIDGE_ERROR"})
NON_RETRYABLE_CODES = frozenset({
    "FIREWALL_DENIED", "SENSITIVE_OPERATION_BLOCKED", "UPLOAD_DENIED",
    "APPROVAL_DENIED", "TAB_LIMIT", "INVALID_ARGUMENT",
})
STOP_CODES = frozenset({"RUN_PAUSED", "RUN_CANCELLED"})


def browser_context(runtime: BrowserRuntime) -> Any:
    """A minimal stand-in for ``RunContext`` so the orchestrator can call the
    same tool functions the model calls. They read only ``ctx.deps``."""
    return SimpleNamespace(deps=runtime)


@dataclass
class AxisDeps:
    """What both the guard and the browser tools need. ``browser`` is passed
    straight through to the unmodified tool functions."""

    browser: BrowserRuntime
    gate: "StepGate"


def _command_operation(kwargs: dict[str, Any]) -> str | None:
    command = kwargs.get("command")
    for attribute in ("action", "operation", "condition", "assertion", "capture", "diagnostic"):
        value = getattr(command, attribute, None)
        if isinstance(value, str):
            return value
    mode = kwargs.get("mode")
    return mode if isinstance(mode, str) else None


def _extracted(tool: str, data: dict[str, Any]) -> str | None:
    """The part of a tool result worth carrying in bounded task memory."""
    if tool == "browser_observe":
        return data.get("snapshot") or None
    if tool == "browser_assert":
        return f"assert {data.get('assertion')} passed={data.get('passed')} expected={data.get('expected')!r}"
    if tool == "browser_tabs":
        tabs = data.get("tabs")
        if not isinstance(tabs, list):
            return None
        # A bare count gave the planner nothing to answer a "list the tabs"
        # style task from, so it kept re-browsing forever hoping for more
        # detail that never came (the raw tool result never reaches it by
        # design). List operations carry the titles/URLs; single-tab
        # operations (create/activate/close) just confirm the one tab.
        if len(tabs) > 1:
            lines = "; ".join(f"{t.get('tab')}: {t.get('title')!r} — {t.get('url')}" for t in tabs)
            return f"{len(tabs)} open tabs: {lines}"
        return f"{len(tabs)} open tabs" if tabs is not None else None
    if tool == "browser_capture_evidence":
        return f"evidence {data.get('evidence')} ({data.get('capture')})"
    if tool == "browser_diagnose":
        return data.get("summary") or f"{data.get('diagnostic')}: {data.get('count')} events"
    if tool == "browser_downloads":
        items = data.get("items")
        if isinstance(items, list):
            return f"{len(items)} downloads: " + "; ".join(str(item.get("filename")) for item in items[:8])
        download = data.get("download")
        return f"download {download.get('state')}: {download.get('filename')}" if isinstance(download, dict) else None
    if tool == "browser_navigate":
        return f"navigated to {data.get('url')!r} ({data.get('title')!r})"
    return None


def _describe_args(kwargs: dict[str, Any]) -> str | None:
    """A compact, safe rendering of one call's arguments for debug tracing.
    Everything here is already alias-safe (tab aliases, refs, URLs, keys) —
    the same values the model itself sent — never a raw browser identifier."""
    parts: list[str] = []
    for key, value in kwargs.items():
        if hasattr(value, "model_dump"):
            value = value.model_dump(exclude_none=True, exclude_defaults=True)
        parts.append(f"{key}={value!r}")
    return ", ".join(parts) if parts else None


def _refusal(tab: Any, code: str, message: str) -> dict[str, Any]:
    """Same envelope shape ``browser_tools`` returns, so a refusal reads to the
    model exactly like any other failed call."""
    return {
        "ok": False,
        "tab": tab if isinstance(tab, str) else None,
        "data": None,
        "error": {"code": code, "message": message, "retryable": False, "detail": None},
    }


@dataclass
class StepGate:
    """Per-navigator-step execution gate. One instance per step.

    Sequencing is Pydantic AI's (`Tool(..., sequential=True)`); this decides
    whether each call in that sequence is allowed to reach the bridge at all.
    """

    max_actions: int
    remaining_total_actions: int
    approval_actions: frozenset[str] = frozenset()
    approval: Callable[[str, str | None, dict[str, Any]], bool] | None = None
    is_paused: Callable[[], bool] = lambda: False
    is_cancelled: Callable[[], bool] = lambda: False
    on_action: Callable[[ActionRecord, int], None] | None = None
    # Called for a call the gate refused before it ever reached the bridge
    # (budget, interruption, pause/cancel, denied approval) — never stored in
    # bounded TaskMemory, but essential for tracing a step's root cause.
    on_refusal: Callable[[str, str | None, str, str, str | None], None] | None = None

    actions_used: int = 0
    interrupted: bool = False
    interrupt_reason: str | None = None
    unknown_outcome: bool = False
    non_retryable: bool = False
    stopped: str | None = None
    # Total time this step spent inside the bridge, for run-level attribution.
    browser_ms: int = 0
    # Set when a call changed which tab is active, so the orchestrator can
    # follow it instead of observing the tab the navigator just left.
    active_tab_changed_to: str | None = None
    records: list[ActionRecord] = field(default_factory=list)
    # (mutated, verified) per record, in execution order, so the orchestrator
    # can tell whether a mutation was followed by evidence.
    effects: list[tuple[bool, bool]] = field(default_factory=list)

    def _interrupt(self, reason: str) -> None:
        if not self.interrupted:
            self.interrupted = True
            self.interrupt_reason = reason

    def check(self, tool: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """Return a refusal envelope if this call must not reach the bridge."""
        tab = kwargs.get("tab")
        operation = _command_operation(kwargs)
        args = _describe_args(kwargs)

        def refuse(code: str, message: str) -> dict[str, Any]:
            if self.on_refusal is not None:
                self.on_refusal(tool, operation, code, message, args)
            return _refusal(tab, code, message)

        if self.is_cancelled():
            self.stopped = "cancelled"
            self._interrupt("the run was cancelled")
            return refuse("RUN_CANCELLED", "The run was cancelled; stop and report.")
        if self.is_paused():
            self.stopped = "paused"
            self._interrupt("the run was paused")
            return refuse("RUN_PAUSED", "The run is paused; stop and report.")
        if self.interrupted:
            return refuse(
                "STEP_INTERRUPTED",
                f"Remaining actions were cancelled because {self.interrupt_reason}. "
                "Report what happened; a fresh observation follows.",
            )
        if self.actions_used >= self.max_actions:
            self._interrupt("the per-step action budget was reached")
            return refuse(
                "ACTION_BUDGET_EXHAUSTED",
                f"This step allows at most {self.max_actions} browser actions. Report now.",
            )
        if self.actions_used >= self.remaining_total_actions:
            self._interrupt("the total browser action budget was reached")
            return refuse("ACTION_BUDGET_EXHAUSTED", "The run's browser action budget is exhausted.")
        if operation in self.approval_actions:
            approved = True
            if self.approval is not None:
                approved = bool(self.approval(tool, operation, {"tab": tab}))
            if not approved:
                self._interrupt("approval was denied")
                self.non_retryable = True
                return refuse("APPROVAL_DENIED", f"The user did not approve {operation!r}.")
        return None

    def record(
        self, tool: str, kwargs: dict[str, Any], result: dict[str, Any], duration_ms: int = 0,
    ) -> ActionRecord:
        """Turn one executed call into a bounded record and decide whether the
        rest of this step must be interrupted."""
        operation = _command_operation(kwargs)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        success = bool(result.get("ok"))
        code = error.get("code")
        changed = data.get("whatChanged") if isinstance(data.get("whatChanged"), dict) else {}
        refs_invalidated = bool(data.get("refsInvalidated")) or bool(changed.get("urlChanged"))
        rerendered = bool(changed.get("domChanged")) or bool(changed.get("navigated"))
        mutating = tool in MUTATING_TOOLS and (tool == "browser_navigate" or operation in MUTATING_ACTIONS)

        mutated = success and mutating
        verified = success and (
            tool == "browser_observe" or (tool == "browser_assert" and bool(data.get("passed")))
        )

        if not success:
            if code in STOP_CODES:
                self.stopped = "cancelled" if code == "RUN_CANCELLED" else "paused"
            if mutating and code in UNKNOWN_OUTCOME_CODES:
                self.unknown_outcome = True
                self._interrupt(f"a {operation!r} action failed with an unknown outcome ({code})")
            elif code in NON_RETRYABLE_CODES:
                self.non_retryable = True
                self._interrupt(f"a non-retryable failure occurred ({code})")
            else:
                self._interrupt(f"an action failed ({code})")
        elif refs_invalidated:
            self._interrupt("the page navigated and the current refs are invalid")
        elif rerendered:
            self._interrupt("the page rerendered substantially")
        elif tool == "browser_act" and isinstance(data.get("openedTabs"), list) and data["openedTabs"]:
            candidates = [alias for alias in data["openedTabs"] if isinstance(alias, str)]
            self.active_tab_changed_to = candidates[-1] if candidates else None
            self._interrupt("the action opened a popup tab")
        elif tool == "browser_wait" and isinstance(data.get("openedTab"), str):
            self.active_tab_changed_to = data["openedTab"]
            self._interrupt("the popup tab is now the active browser context")
        elif tool == "browser_tabs" and operation in {"activate", "close", "create"}:
            # Record which tab is now active. Without this the orchestrator
            # keeps observing the tab the navigator just left, so the model
            # has to spend a whole extra step re-activating the new one.
            if operation in {"activate", "create"} and success:
                new_tab = result.get("tab") or data.get("tab")
                if isinstance(new_tab, str):
                    self.active_tab_changed_to = new_tab
            self._interrupt("the active tab changed")

        record = ActionRecord(
            tool=tool,
            operation=operation,
            args=_describe_args(kwargs),
            success=success,
            extracted_data=_extracted(tool, data) if success else None,
            error=None if success else clip(f"{code}: {error.get('message')}"),
            refs_invalidated=refs_invalidated,
            retain_in_memory=(
                (not success) or mutating or tool in {"browser_assert", "browser_capture_evidence"}
                # A tabs.list result is the answer to "what tabs are open" — if it
                # only lives in this step's transient NavigatorOutcome, the
                # planner loses it by the next pass and can never accumulate
                # enough to call the task complete.
                or (tool == "browser_tabs" and operation == "list")
                or tool == "browser_downloads"
            ),
        )
        self.records.append(record)
        self.effects.append((mutated, verified))
        self.actions_used += 1
        self.browser_ms += duration_ms
        if self.on_action is not None:
            self.on_action(record, duration_ms)
        return record


def guarded(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap one ``browser_tools`` function so every call goes through the gate.

    ``functools.wraps`` keeps the name, docstring, and signature, so Pydantic
    generates exactly the same tool schema it would for the bare function.
    """

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ctx = args[0] if args else kwargs.pop("ctx")
        rest = args[1:]
        deps: AxisDeps = ctx.deps
        named = dict(kwargs)
        # `tab` is the only positional the tools take after ctx.
        if rest:
            named.setdefault("tab", rest[0])
        refusal = deps.gate.check(function.__name__, named)
        if refusal is not None:
            return refusal
        browser_ctx = dataclasses.replace(ctx, deps=deps.browser)
        started = time.perf_counter()
        result = function(browser_ctx, *rest, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        deps.gate.record(function.__name__, named, result, duration_ms)
        return result

    return wrapper


def navigator_tools(config: AxisConfig) -> list[Tool[AxisDeps]]:
    """The navigator's tool list. A plain Python list — no registry, no
    capability framework, no tool-selection agent."""
    selected = list(DEFAULT_TOOL_FUNCTIONS)
    optional: list[Callable[..., Any]] = []
    if config.tools.capture_evidence:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_capture_evidence"])
    if config.tools.diagnose:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_diagnose"])
    if config.tools.downloads:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_downloads"])

    def build(function: Callable[..., Any], *, defer: bool) -> Tool[AxisDeps]:
        return Tool(
            guarded(function),
            takes_ctx=True,
            sequential=True,
            timeout=config.run.tool_timeout_seconds,
            defer_loading=defer,
        )

    # When tool search is on, the opt-in tools stay hidden until the navigator
    # actually searches for them; the six ordinary tools always stay visible.
    return [build(f, defer=False) for f in selected] + [
        build(f, defer=config.tools.tool_search) for f in optional
    ]


def build_model(config: AxisConfig, *, client: Any | None = None, model_name: str | None = None) -> Model:
    """One provider/model instance, shared by both agents.

    Talks to OCI GenAI's OpenAI-compatible endpoint through the existing
    ``provider_config`` helpers; nothing about auth is duplicated here.
    """
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    import provider_config as pc

    if client is None:
        loaded = pc.load_provider_config()
        if config.provider.base_url or config.provider.model:
            loaded = dataclasses.replace(
                loaded,
                base_url=config.provider.base_url or loaded.base_url,
                model=config.provider.model or loaded.model,
            )
        client = pc.build_async_openai_client(loaded)
        model_name = model_name or loaded.model
    if not model_name:
        raise ValueError("A model name is required when an OpenAI client is supplied directly.")
    provider = OpenAIProvider(openai_client=client)
    model_class = OpenAIResponsesModel if config.provider.api == "responses" else OpenAIChatModel
    return model_class(model_name, provider=provider, settings=config.provider.settings or None)


def build_planner(config: AxisConfig, model: Model) -> Agent[None, PlanDecision]:
    """The planner has no tools, by design. Its AgentSpec (prompt, name,
    retries, ...) is loaded straight from ``planner_agent.yaml`` with
    ``Agent.from_file`` — only the shared model instance and the typed output
    are supplied by code."""
    return Agent.from_file(config.planner_spec_path, model=model, output_type=PlanDecision)


def build_navigator(config: AxisConfig, model: Model) -> Agent[AxisDeps, NavigatorOutcome]:
    tools = navigator_tools(config)
    capabilities = [ToolSearch()] if config.tools.tool_search else None
    return Agent.from_file(
        config.navigator_spec_path,
        model=model,
        output_type=NavigatorOutcome,
        deps_type=AxisDeps,
        tools=tools,
        capabilities=capabilities,
    )
