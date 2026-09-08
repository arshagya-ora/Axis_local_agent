"""The AXIS orchestrator: planner cadence, navigator steps, budgets, memory.

Ordinary async Python calling two Pydantic AI agents programmatically. Neither
agent calls the other. There is no graph, no workflow engine, no effect ledger,
no lease, and nothing durable — pause state lives in this object and dies with
the process.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, ModelHTTPError, RunUsage, UsageLimitExceeded, UsageLimits, format_as_xml

import browser_tools as bt
from browser_tools import BrowserRuntime, ToolFault

from .agents import AxisDeps, StepGate, browser_context
from .models import (
    ActionRecord,
    AxisConfig,
    AxisEvent,
    AxisResult,
    BrowserState,
    NavigatorOutcome,
    PlanDecision,
    TabSummary,
    TaskMemory,
    clip,
)

EventCallback = Callable[[AxisEvent], None]
ApprovalCallback = Callable[[str, str | None, dict[str, Any]], bool]

# A schemeless/internal page a real content script cannot attach to, even
# under a "*" host permission — chrome:// pages (no real origin) and
# about:blank (no origin until it navigates) both hit
# "Extension manifest must request permission to access this host." from the
# bridge. Never treat either as an observable or a landing tab.
_UNOBSERVABLE_PREFIXES = ("chrome://", "about:")
# The fallback destination when no observable tab exists. Must be a real
# http(s) page a content script can attach to — "about:blank" cannot be, see
# above. Matches the Google flow the project's own smoke test already treats
# as a first-class target (scripts/browser_foundation_smoke.py).
_FALLBACK_TAB_URL = "https://www.google.com/webhp?hl=en&pws=0"


def _unobservable(url: str | None) -> bool:
    return (url or "").startswith(_UNOBSERVABLE_PREFIXES)

# Bridge faults that end the run rather than feeding another planner pass.
FATAL_BRIDGE_CODES = frozenset({"BRIDGE_UNAVAILABLE", "FIREWALL_DENIED"})

# The spec keeps browser_capture_evidence out of the ordinary tool set and adds
# it only "when evidence or an artifact is requested" — this is how a request
# is recognised in the task text, so a user who asks for a screenshot actually
# gets the tool that can take one.
_EVIDENCE_REQUEST = re.compile(
    r"screen\s?shot|screengrab|screen capture|\bcapture\b|\bevidence\b|\bproof\b|"
    r"\bpdf\b|\bartifact\b|save .*(image|picture|file)",
    re.IGNORECASE,
)
_DOWNLOAD_REQUEST = re.compile(
    r"\bdownload(?:ed|ing|s)?\b|\bexport\b.*\b(file|csv|xlsx|pdf|zip)\b|"
    r"\bsave\b.*\b(file|report|attachment)\b",
    re.IGNORECASE,
)


def evidence_requested(*texts: str | None) -> bool:
    return any(_EVIDENCE_REQUEST.search(text) for text in texts if text)


def downloads_requested(*texts: str | None) -> bool:
    return any(_DOWNLOAD_REQUEST.search(text) for text in texts if text)


class _Stop(Exception):
    """Internal: carry a finished :class:`AxisResult` out of a nested step."""

    def __init__(self, result: AxisResult):
        super().__init__(result.reason)
        self.result = result


class AxisOrchestrator:
    """Drives one task (and its follow-ups) across a live browser runtime."""

    def __init__(
        self,
        *,
        browser: BrowserRuntime,
        planner: Agent[Any, PlanDecision],
        navigator: Agent[Any, NavigatorOutcome],
        config: AxisConfig,
        on_event: EventCallback | None = None,
        approval: ApprovalCallback | None = None,
        navigator_factory: Callable[..., Agent[Any, NavigatorOutcome]] | None = None,
    ):
        self.browser = browser
        self.planner = planner
        self.navigator = navigator
        self.config = config
        self.on_event = on_event
        self.approval = approval
        # Rebuilds the navigator with the opt-in tools enabled. Without one the
        # tool set is fixed for the whole run (the default the tests use).
        self.navigator_factory = navigator_factory

        self.memory: TaskMemory | None = None
        self.usage = RunUsage()
        self.usage_limits = UsageLimits(request_limit=config.run.max_model_requests)
        self.browser_actions = 0
        self.planner_passes = 0
        self.tab: str | None = None

        self._paused = False
        self._cancelled = False
        self._completion_rejected = False
        self._repair_attempted = False
        self._capture_enabled = config.tools.capture_evidence
        self._diagnose_enabled = config.tools.diagnose
        self._downloads_enabled = config.tools.downloads
        self._debug_telemetry_started = False
        self._started = time.monotonic()
        self._model_ms = 0
        self._browser_ms = 0
        self._plan_ms = 0
        self._step_ms = 0

    # -- timing ----------------------------------------------------------

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    # -- control ---------------------------------------------------------

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def cancel(self) -> None:
        self._cancelled = True

    # -- entry points ----------------------------------------------------

    async def run(self, task: str) -> AxisResult:
        self.memory = self.config.new_memory(task)
        self._completion_rejected = False
        self._started = time.monotonic()
        self._model_ms = self._browser_ms = 0
        self._sync_navigator_tools(
            capture=evidence_requested(task), downloads=downloads_requested(task),
        )
        return await self._loop()

    async def continue_task(self, follow_up: str) -> AxisResult:
        """Append a follow-up and force an immediate planner pass.

        Keeps bounded task context, extracted data, completed goals, and the
        live browser runtime with its open tabs. The browser is only re-bound
        if the previous run lost its tab.
        """
        if self.memory is None:
            return await self.run(follow_up)
        self.memory.add_followup(follow_up)
        self.memory.last_error = None
        self.memory.consecutive_failures = 0
        self.memory.navigator_steps_since_plan = self.config.run.planner_interval_steps
        self.memory.current_goal = None
        self._paused = False
        self._cancelled = False
        self._completion_rejected = False
        self._started = time.monotonic()
        self._model_ms = self._browser_ms = 0
        self._sync_navigator_tools(
            capture=evidence_requested(follow_up), downloads=downloads_requested(follow_up),
        )
        return await self._loop()

    def _sync_navigator_tools(
        self, *, capture: bool = False, diagnose: bool = False, downloads: bool = False,
    ) -> None:
        """Turn on an opt-in tool when the run actually needs it.

        ``browser_capture_evidence`` is added when the user asked for a
        screenshot/artifact, and ``browser_diagnose`` after a genuine failure
        worth investigating — matching the spec's "add only when requested"
        rule while making sure a task that asks for a screenshot is not left
        without the one tool that can take it.
        """
        want_capture = self._capture_enabled or capture or self.config.tools.capture_evidence
        want_diagnose = self._diagnose_enabled or diagnose or self.config.tools.diagnose
        want_downloads = self._downloads_enabled or downloads or self.config.tools.downloads
        if (want_capture, want_diagnose, want_downloads) == (
            self._capture_enabled, self._diagnose_enabled, self._downloads_enabled,
        ):
            return
        self._capture_enabled, self._diagnose_enabled, self._downloads_enabled = (
            want_capture, want_diagnose, want_downloads,
        )
        if self.navigator_factory is None:
            return
        self.navigator = self.navigator_factory(
            capture=want_capture, diagnose=want_diagnose, downloads=want_downloads,
        )
        self._emit(
            "status", phase="tools_changed", capture=want_capture,
            diagnose=want_diagnose, downloads=want_downloads,
        )

    # -- events ----------------------------------------------------------

    def _emit(self, kind: str, **detail: Any) -> None:
        if self.on_event is None:
            return
        detail.setdefault("at_ms", self._elapsed_ms())
        try:
            self.on_event(AxisEvent(kind=kind, detail=detail))
        except Exception:  # a caller's callback must never break a run
            pass

    # -- budgets and stopping --------------------------------------------

    def _stop_reason(self) -> AxisResult | None:
        memory = self.memory
        assert memory is not None
        run = self.config.run
        if self._cancelled:
            return self._result("cancelled", reason="The user cancelled the run.")
        if self._paused:
            return self._result("paused", reason="The run is paused; task and browser state are preserved.")
        if memory.total_steps >= run.max_total_steps:
            return self._result("limit_reached", reason=f"Reached the maximum of {run.max_total_steps} steps.")
        if int(self.usage.requests) >= run.max_model_requests:
            return self._result("limit_reached", reason="Reached the maximum number of model requests.")
        if self.browser_actions >= run.max_browser_actions:
            return self._result("limit_reached", reason="Reached the maximum number of browser actions.")
        if memory.consecutive_failures >= run.max_consecutive_failures:
            return self._result("failed", reason=f"{memory.consecutive_failures} consecutive failures.")
        return None

    def _result(self, status: str, *, answer: str | None = None, evidence: list[str] | None = None,
                reason: str = "") -> AxisResult:
        memory = self.memory
        debug_artifacts = self.browser.stop_debug_telemetry() if self._debug_telemetry_started else {}
        self._debug_telemetry_started = False
        result = AxisResult(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            evidence=evidence or [],
            reason=reason,
            total_steps=memory.total_steps if memory else 0,
            planner_passes=self.planner_passes,
            browser_actions=self.browser_actions,
            model_requests=int(self.usage.requests),
            duration_ms=self._elapsed_ms(),
            model_ms=self._model_ms,
            browser_ms=self._browser_ms,
        )
        self._emit(
            "final", status=result.status, reason=result.reason,
            duration_ms=result.duration_ms, model_ms=result.model_ms, browser_ms=result.browser_ms,
            debug_artifacts=debug_artifacts or None,
        )
        return result

    # -- main loop -------------------------------------------------------

    async def _loop(self) -> AxisResult:
        memory = self.memory
        assert memory is not None
        while True:
            stop = self._stop_reason()
            if stop is not None:
                return stop
            try:
                plan = await self._plan()
            except _Stop as stopped:
                return stopped.result

            self.planner_passes += 1
            memory.navigator_steps_since_plan = 0
            self._emit("planner_decision", **plan.model_dump(), duration_ms=self._plan_ms)

            if plan.decision == "complete":
                if memory.unverified_mutations and not self._completion_rejected:
                    self._completion_rejected = True
                    memory.last_error = (
                        "Completion rejected once: the page was changed and no observation or "
                        "passing assertion has been made since. Verify the result, then decide again."
                    )
                    memory.current_goal = memory.current_goal or "Verify the result of the last change."
                    continue
                return self._result(
                    "completed", answer=plan.final_answer, evidence=plan.evidence, reason=plan.reason,
                )
            if plan.decision == "ask_user":
                return self._result("needs_user", answer=plan.final_answer, evidence=plan.evidence,
                                    reason=plan.reason)
            if plan.decision == "fail":
                return self._result("failed", evidence=plan.evidence, reason=plan.reason)

            if memory.current_goal and plan.next_goal and plan.next_goal != memory.current_goal:
                memory.complete_goal(memory.current_goal)
            memory.current_plan_summary = plan.plan_summary
            memory.current_goal = plan.next_goal
            memory.current_success_condition = plan.success_condition
            self._repair_attempted = False

            try:
                await self._navigator_burst()
            except _Stop as stopped:
                return stopped.result

    async def _navigator_burst(self) -> None:
        """Run navigator steps until the planner is due again."""
        memory = self.memory
        assert memory is not None
        while True:
            stop = self._stop_reason()
            if stop is not None:
                raise _Stop(stop)

            state = await self._browser_state()
            outcome, gate = await self._navigate(state)

            memory.total_steps += 1
            memory.navigator_steps_since_plan += 1
            memory.latest_navigator_outcome = outcome
            memory.record_actions(gate.records)
            self.browser_actions += gate.actions_used
            mutated_this_step = False
            for record, (mutated, verified) in zip(gate.records, gate.effects):
                if mutated:
                    memory.mutation_count += 1
                    mutated_this_step = True
                if verified:
                    memory.verified_mutation_count = memory.mutation_count
            # Follow a tab the navigator switched to, so the next observation
            # targets the page it actually moved to rather than the one it
            # left — otherwise it must burn a whole step re-activating.
            if gate.active_tab_changed_to and gate.active_tab_changed_to in self.browser.tabs:
                self.tab = gate.active_tab_changed_to
            failures = [record for record in gate.records if not record.success]
            # A goal_reached decision with no new mutation is a semantic
            # assessment of the fresh pre-step BrowserState. Do not grant the
            # same status to a step that just changed the page: that change
            # still needs a later observation or passing assertion.
            if outcome.status == "goal_reached" and not failures and not mutated_this_step:
                memory.verified_mutation_count = memory.mutation_count
            memory.last_error = failures[-1].error if failures else None
            self._emit(
                "navigator_step",
                **outcome.model_dump(),
                actions=gate.actions_used,
                interrupted=gate.interrupt_reason,
                duration_ms=getattr(self, "_step_ms", 0),
                browser_ms=gate.browser_ms,
            )

            if gate.stopped == "cancelled":
                raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
            if gate.stopped == "paused":
                raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))

            if failures:
                memory.consecutive_failures += 1
                # A genuine failure worth investigating — the one condition the
                # spec allows browser_diagnose to be added under.
                if gate.unknown_outcome or gate.non_retryable or memory.consecutive_failures > 1:
                    self._sync_navigator_tools(diagnose=True)
            else:
                memory.consecutive_failures = 0
                self._repair_attempted = False

            # Back to the planner immediately.
            if outcome.status != "continue":
                if outcome.status == "goal_reached" and memory.current_goal:
                    memory.complete_goal(f"{memory.current_goal} -> {outcome.summary}")
                    memory.current_goal = None
                return
            if gate.unknown_outcome:
                memory.last_error = (
                    f"{memory.last_error or 'An action had an unknown outcome.'} "
                    "It was not repeated; verify the page before acting again."
                )
                return
            if gate.non_retryable:
                return
            if failures:
                if self._repair_attempted:
                    return  # one repair attempt only
                self._repair_attempted = True
            if memory.navigator_steps_since_plan >= self.config.run.planner_interval_steps:
                return

    # -- planner ---------------------------------------------------------

    async def _plan(self) -> PlanDecision:
        memory = self.memory
        assert memory is not None
        run = self.config.run
        context: dict[str, Any] = {
            "original_task": memory.original_task,
            "user_followups": memory.user_followups,
            "current_plan_summary": memory.current_plan_summary,
            "current_goal": memory.current_goal,
            "success_condition": memory.current_success_condition,
            "completed_goals": memory.completed_goal_summaries,
            "latest_navigator_outcome": (
                memory.latest_navigator_outcome.model_dump() if memory.latest_navigator_outcome else None
            ),
            "recent_action_results": [record.model_dump() for record in memory.recent_action_results],
            "relevant_extracted_data": memory.relevant_extracted_data,
            "last_error": memory.last_error,
            "unverified_page_changes": memory.unverified_mutations,
            "remaining_steps": max(0, run.max_total_steps - memory.total_steps),
            "remaining_browser_actions": max(0, run.max_browser_actions - self.browser_actions),
            "remaining_model_requests": max(0, run.max_model_requests - int(self.usage.requests)),
        }
        if self.tab is not None:
            context["browser"] = {
                "tab": self.tab,
                "url": self.browser.safe_url(self.browser.tabs[self.tab].url),
                "title": self.browser.tabs[self.tab].title,
            }
        self._emit("status", phase="planner_started", steps_since_plan=memory.navigator_steps_since_plan)
        started = time.monotonic()
        decision = await self._run_agent(
            self.planner,
            format_as_xml(context, root_tag="task_context"),
            deps=None,
        )
        self._plan_ms = int((time.monotonic() - started) * 1000)
        return decision

    # -- navigator -------------------------------------------------------

    async def _navigate(self, state: BrowserState) -> tuple[NavigatorOutcome, StepGate]:
        run = self.config.run
        gate = StepGate(
            max_actions=run.max_actions_per_step,
            remaining_total_actions=max(0, run.max_browser_actions - self.browser_actions),
            approval_actions=frozenset(self.config.tools.approval_required_actions),
            approval=self.approval,
            is_paused=lambda: self._paused,
            is_cancelled=lambda: self._cancelled,
            on_action=lambda record, duration_ms: self._emit(
                "browser_action", tool=record.tool, operation=record.operation, args=record.args,
                success=record.success, error=record.error, refs_invalidated=record.refs_invalidated,
                skipped=False, duration_ms=duration_ms,
            ),
            on_refusal=lambda tool, operation, code, message, args: self._emit(
                "browser_action", tool=tool, operation=operation, args=args,
                success=False, error=f"{code}: {message}", refs_invalidated=False, skipped=True,
            ),
        )
        deps = AxisDeps(browser=self.browser, gate=gate)
        self._emit("status", phase="navigator_step_started", goal=state.goal)
        prompt = format_as_xml(state.model_dump(exclude_none=True), root_tag="browser_state")
        started = time.monotonic()
        outcome = await self._run_agent(self.navigator, prompt, deps=deps)
        self._browser_ms += gate.browser_ms
        self._step_ms = int((time.monotonic() - started) * 1000)
        return outcome, gate

    async def _run_agent(self, agent: Agent[Any, Any], prompt: str, *, deps: Any) -> Any:
        """One model request, with the shared usage record and limits.

        Pause/cancel are checked here, immediately before the request.
        """
        if self._cancelled:
            raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
        if self._paused:
            raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))
        started = time.monotonic()
        try:
            result = await agent.run(
                prompt, deps=deps, usage=self.usage, usage_limits=self.usage_limits,
            )
        except UsageLimitExceeded as exc:
            raise _Stop(self._result("limit_reached", reason=clip(str(exc)) or "Usage limit exceeded.")) from exc
        except ModelHTTPError as exc:
            status = getattr(exc, "status_code", None)
            reason = (
                "Provider authentication failed."
                if status in (401, 403)
                else f"The model provider returned an error ({status})."
            )
            raise _Stop(self._result("failed", reason=reason)) from exc
        finally:
            self._model_ms += int((time.monotonic() - started) * 1000)
        return result.output

    # -- browser ---------------------------------------------------------

    def _ensure_tab(self) -> str:
        """Bind (once) to a live, observable tab. Never restarts a browser
        that is fine.

        A schemeless/internal page (``chrome://...``, ``about:blank``) cannot
        host a content script even though our firewall allows navigating
        there — the bridge's own extension-side host-permission check refuses
        to read it. Whole-browser access can land on one of those as the
        "focused" tab, or such a tab can already be open from an earlier run,
        so any unobservable candidate is skipped in favor of an ordinary tab,
        falling back to a freshly created one already pointed at a real page.
        """
        if self.tab is not None and not self.browser.tabs[self.tab].closed:
            return self.tab
        ctx = browser_context(self.browser)

        listed = bt.browser_tabs(ctx, bt.ListTabs(operation="list"))
        self._emit(
            "status", phase="tab_list", success=listed["ok"],
            tabs=len(listed["data"]["tabs"]) if listed["ok"] else None,
            error=self._envelope_error(listed),
        )
        if not listed["ok"]:
            raise _Stop(self._result(
                "failed", reason=self._envelope_error(listed) or "The browser bridge failed to list tabs.",
            ))
        tabs = listed["data"]["tabs"]

        def observable(candidates: list[dict[str, Any]]) -> str | None:
            return next((t["tab"] for t in candidates if not _unobservable(t.get("url"))), None)

        chosen = None
        if self.config.browser.initial_tab == "focused":
            active = [t for t in tabs if t.get("active")]
            chosen = observable(active) or (active[0]["tab"] if active else None)
        if chosen is None:
            chosen = observable(tabs) or (tabs[0]["tab"] if tabs else None)
        needs_new_tab = chosen is None or (
            self.browser.tabs.get(chosen) and _unobservable(self.browser.tabs[chosen].url)
        )
        if needs_new_tab:
            created = bt.browser_tabs(ctx, bt.CreateTab(operation="create", url=_FALLBACK_TAB_URL))
            self._emit(
                "status", phase="tab_create", success=created["ok"],
                tab=created.get("tab"), error=self._envelope_error(created),
            )
            if not created["ok"]:
                raise _Stop(self._result(
                    "failed",
                    reason=self._envelope_error(created) or "The browser bridge refused to open a new tab.",
                ))
            chosen = created["tab"]
        else:
            self._emit("status", phase="tab_selected", tab=chosen, url=self.browser.tabs[chosen].url)
        self.tab = chosen
        return chosen

    @staticmethod
    def _envelope_error(envelope: dict[str, Any]) -> str | None:
        """The full ``code: message`` from a browser_tools envelope's error, so
        a browser-layer failure is never reported to the user or the debug
        trace as an opaque, code-free string."""
        error = envelope.get("error")
        if not isinstance(error, dict):
            return None
        return clip(f"{error.get('code')}: {error.get('message')}")

    def _tab_summaries(self) -> list[TabSummary]:
        return [
            TabSummary(**self.browser.public_tab(alias))
            for alias, state in self.browser.tabs.items()
            if not state.closed
        ]

    async def _browser_state(self) -> BrowserState:
        """A fresh, bounded observation before every navigator step.

        Calls ``BrowserRuntime.observe`` directly — the same method the
        ``browser_observe`` tool calls — so observation logic exists once.
        """
        memory = self.memory
        assert memory is not None
        run = self.config.run
        tab = self._ensure_tab()
        if self.config.tools.debug_recording and not self._debug_telemetry_started:
            self.browser.start_debug_telemetry(tab)
            self._debug_telemetry_started = bool(self.browser.debug_recording or self.browser.debug_trace)

        observation: dict[str, Any] | None = None
        last: ToolFault | None = None
        observe_started = time.monotonic()
        for attempt in range(2):  # one repair attempt, then stop
            try:
                observation = self.browser.observe(tab, run.observation_mode, run.observation_max_nodes)
                break
            except ToolFault as fault:
                last = fault
                if fault.code in FATAL_BRIDGE_CODES or not fault.retryable or attempt == 1:
                    break
        observe_ms = int((time.monotonic() - observe_started) * 1000)
        self._browser_ms += observe_ms
        self._emit(
            "status", phase="observed", tab=tab, success=observation is not None,
            duration_ms=observe_ms,
        )
        if observation is None:
            reason = clip(str(last)) if last else "The browser could not be observed."
            raise _Stop(self._result("failed", reason=reason or "The browser could not be observed."))
        screenshot = self._screenshot(tab) if run.include_screenshot else None
        state = BrowserState(
            tab=tab,
            url=observation.get("url"),
            title=observation.get("title"),
            tabs=self._tab_summaries(),
            scroll=observation.get("scroll"),
            snapshot=observation.get("snapshot", ""),
            truncated=bool(observation.get("truncated")),
            screenshot=screenshot,
            refs_fresh=tab in self.browser.observations,
            unverified_page_changes=memory.unverified_mutations,
            site_pattern=observation.get("sitePattern"),
            runtime_warning=observation.get("runtimeWarning"),
            recent_actions=memory.recent_action_results[-4:],
            goal=memory.current_goal,
            success_condition=memory.current_success_condition,
            remaining_steps=max(0, run.max_total_steps - memory.total_steps),
            remaining_actions=min(
                run.max_actions_per_step, max(0, run.max_browser_actions - self.browser_actions)
            ),
            note=memory.last_error,
        )
        return state

    def _screenshot(self, tab: str) -> str | None:
        """Capture a screenshot and hand the model only its safe alias — image
        bytes never enter the model history."""
        result = bt.browser_capture_evidence(
            browser_context(self.browser), tab,
            bt.PageCapture(capture="page_screenshot", save=True),
        )
        if not result["ok"]:
            return None
        return f"{result['data'].get('evidence')} (saved page screenshot)"


__all__ = ["AxisOrchestrator", "EventCallback", "ApprovalCallback"]
