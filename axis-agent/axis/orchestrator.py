"""The AXIS orchestrator: planner cadence, navigator steps, budgets, memory.

Ordinary async Python calling two Pydantic AI agents programmatically. Neither
agent calls the other. There is no graph, no workflow engine, no effect ledger,
no lease, and nothing durable — pause state lives in this object and dies with
the process.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic_ai import Agent, ModelHTTPError, RunCancelled, UsageLimitExceeded, UsageLimits, format_as_xml

import browser_tools as bt
from browser_tools import BrowserRuntime, ToolFault

from .agents import AxisDeps, RECOVERY_POLICY, StepGate, browser_context
from .models import (
    ActionRecord,
    AssertionRecord,
    AxisConfig,
    AxisEvent,
    AxisResult,
    BrowserState,
    CompletionVerdict,
    Evidence,
    NavigatorOutcome,
    PlanDecision,
    TabSummary,
    TaskRequirements,
    TaskRunState,
    TaskFact,
    PendingChange,
    GoalCheck,
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
    r"screen\s?shot|screengrab|screen capture|\bproof\b|\bpdf\b|\bartifact\b|"
    r"save .*(image|picture)",
    re.IGNORECASE,
)
_SCREENSHOT_REQUEST = re.compile(
    r"screen\s?shot|screengrab|screen capture|save .*(image|picture)", re.IGNORECASE,
)
_CONSOLE_REQUEST = re.compile(r"\bbrowser console\b|\bconsole (?:log|message|error|warning|output)s?\b", re.IGNORECASE)
_NETWORK_REQUEST = re.compile(
    r"\bnetwork (?:activity|request|response|error|status|detail)s?\b|\bhttp status\b|"
    r"\brequest(?:'s)? (?:status|details?)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_CURRENT_PAGE_DEPENDENCY = re.compile(r"\bcurrent page\b|\bthis page\b|\bfrom here\b|\bcompare\b", re.IGNORECASE)
_REQUIRED_TEXT = re.compile(
    r"(?:assert|verify|contains?|subject(?: is|:)?|exact text(?: is|:)?)\s+(?:that\s+)?[\"“`]([^\"”`]{1,300})[\"”`]",
    re.IGNORECASE,
)
_FORBIDDEN = re.compile(r"\bdo not\s+(send|submit|delete|purchase|buy|post|transfer|upload)\b", re.IGNORECASE)
_DOWNLOAD_REQUEST = re.compile(
    r"\bdownload(?:ed|ing|s)?\b|\bexport\b.*\b(file|csv|xlsx|pdf|zip)\b|"
    r"\bsave\b.*\b(file|report|attachment|csv|xlsx|pdf|zip)\b",
    re.IGNORECASE,
)


def evidence_requested(*texts: str | None) -> bool:
    return any(_EVIDENCE_REQUEST.search(text) for text in texts if text)


def downloads_requested(*texts: str | None) -> bool:
    return any(_DOWNLOAD_REQUEST.search(text) for text in texts if text)


def extract_requirements(*texts: str | None) -> TaskRequirements:
    combined = "\n".join(text for text in texts if text)
    urls = list(dict.fromkeys(match.rstrip(".,);]") for match in _URL.findall(combined)))
    required = list(dict.fromkeys(_REQUIRED_TEXT.findall(combined)))
    forbidden = {match.lower() for match in _FORBIDDEN.findall(combined)}
    return TaskRequirements(
        literal_urls=urls,
        required_text=required,
        require_screenshot=bool(_SCREENSHOT_REQUEST.search(combined)),
        require_download=downloads_requested(combined),
        require_console_check=bool(_CONSOLE_REQUEST.search(combined)),
        require_network_check=bool(_NETWORK_REQUEST.search(combined)),
        mutation_allowed=not bool(re.search(r"\bread[- ]only\b|\bdo not (?:change|modify)\b", combined, re.IGNORECASE)),
        forbidden_actions=forbidden,
    )


def diagnostics_requested(requirements: TaskRequirements) -> bool:
    return requirements.require_console_check or requirements.require_network_check


def _merge_requirements(current: TaskRequirements, added: TaskRequirements) -> TaskRequirements:
    return TaskRequirements(
        literal_urls=list(dict.fromkeys(current.literal_urls + added.literal_urls)),
        required_text=list(dict.fromkeys(current.required_text + added.required_text)),
        final_url_pattern=added.final_url_pattern or current.final_url_pattern,
        require_screenshot=current.require_screenshot or added.require_screenshot,
        require_download=current.require_download or added.require_download,
        require_console_check=current.require_console_check or added.require_console_check,
        require_network_check=current.require_network_check or added.require_network_check,
        mutation_allowed=current.mutation_allowed and added.mutation_allowed,
        forbidden_actions=current.forbidden_actions | added.forbidden_actions,
    )


def verify_completion(state: TaskRunState, browser_state: BrowserState | None) -> CompletionVerdict:
    """Deterministically decide whether a model completion claim is admissible."""
    reasons: list[str] = []
    latest_assertions: dict[tuple[str, str, str, str, str], AssertionRecord] = {}
    for assertion in state.assertions:
        if assertion.superseded_by:
            continue
        key = (assertion.goal_id or "", assertion.tab or "", assertion.target or "",
               assertion.assertion, repr(assertion.expected))
        latest_assertions[key] = assertion
    if any(not item.passed for item in latest_assertions.values()):
        reasons.append("A required assertion is still failing.")
    if state.unverified_mutations:
        reasons.append(f"{state.unverified_mutations} page mutation(s) remain unverified.")
    requirements = state.requirements
    if requirements.require_screenshot and not state.evidence_by_kind("screenshot"):
        reasons.append("The requested screenshot is missing.")
    if requirements.require_download and not state.downloads:
        reasons.append("The requested download is missing.")
    if requirements.require_console_check and not any(e.kind == "console" for e in state.diagnostic_evidence):
        reasons.append("Console diagnostics were requested but not collected.")
    if requirements.require_network_check and not any(e.kind == "network" for e in state.diagnostic_evidence):
        reasons.append("Network diagnostics were requested but not collected.")
    trusted = [item for item in state.evidence if item.verified and item.id and item.kind != "navigator"]
    searchable = "\n".join(
        [browser_state.snapshot if browser_state and browser_state.evidence_id else ""]
        + [item.detail for item in trusted if item.kind in {"observation", "extraction"}]
    ).casefold()
    for required in requirements.required_text:
        if required.casefold() not in searchable:
            reasons.append(f"Required text has not been verified: {required!r}.")
    current_url = browser_state.url if browser_state else None
    for literal_url in requirements.literal_urls:
        if literal_url not in state.visited_urls:
            reasons.append(f"Required URL has not been visited or verified: {literal_url!r}.")
    if requirements.final_url_pattern and not re.search(requirements.final_url_pattern, current_url or ""):
        reasons.append("The final URL condition is not met.")
    unresolved_refusals: dict[tuple[str, str | None], ActionRecord] = {}
    for record in state.actions:
        key = (record.tool, record.operation)
        if not record.executed:
            unresolved_refusals[key] = record
        elif record.success:
            unresolved_refusals.pop(key, None)
    if unresolved_refusals:
        reasons.append("A refused browser action remains unresolved.")
    if state.goal_number and not any(item.kind in {"observation", "extraction", "assertion"} for item in trusted):
        reasons.append("The browser task has no recorded page evidence.")
    return CompletionVerdict(valid=not reasons, reasons=reasons)


class _Stop(Exception):
    """Internal: carry a finished :class:`AxisResult` out of a nested step."""

    def __init__(self, result: AxisResult):
        super().__init__(result.reason)
        self.result = result


class _VisionUnavailable(Exception):
    """The first image request was explicitly rejected before any action."""


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

        self.state: TaskRunState | None = None
        self.usage_limits = UsageLimits(request_limit=config.run.max_model_requests)

        self._prestart_paused = False
        self._capture_enabled = config.tools.capture_evidence
        self._diagnose_enabled = config.tools.diagnose
        self._downloads_enabled = config.tools.downloads
        self._visual_enabled = False
        self._base_navigator = navigator

    @property
    def memory(self) -> TaskRunState | None:
        return self.state

    @property
    def usage(self):
        return self.state.usage if self.state else None

    @property
    def browser_actions(self) -> int:
        return self.state.counters.browser_actions if self.state else 0

    @property
    def planner_passes(self) -> int:
        return self.state.counters.planner_passes if self.state else 0

    @property
    def tab(self) -> str | None:
        return self.state.bound_tab if self.state else None

    @tab.setter
    def tab(self, value: str | None) -> None:
        if self.state is not None:
            self.state.bound_tab = value
            if value and value not in self.state.task_tabs:
                self.state.task_tabs = (self.state.task_tabs + [value])[-8:]

    # -- timing ----------------------------------------------------------

    def _elapsed_ms(self) -> int:
        started = self.state.started_monotonic if self.state else time.monotonic()
        return int((time.monotonic() - started) * 1000)

    # -- control ---------------------------------------------------------

    def pause(self) -> None:
        if self.state:
            self.state.status = "paused"
        else:
            self._prestart_paused = True

    def resume(self) -> None:
        self._prestart_paused = False
        if self.state and self.state.status == "paused":
            self.state.status = "active"

    def cancel(self) -> None:
        if self.state:
            self.state.status = "cancelled"
            self.state.cancellation_token.cancel()

    # -- entry points ----------------------------------------------------

    async def start_task(self, task: str) -> AxisResult:
        """Start an independent task while reusing the connected browser."""
        self.state = self.config.new_memory(task, extract_requirements(task))
        self.browser.task_started_wallclock = self.state.started_wallclock
        self.browser.visual_enabled = False
        self.browser.invalidate_visual()
        self.state.status = "paused" if self._prestart_paused else "active"
        self._set_navigator_tools()
        try:
            return await self._loop()
        except RunCancelled:
            return self._result("cancelled", reason="The user cancelled the run.")
        finally:
            self._close_telemetry()

    async def run(self, task: str) -> AxisResult:
        """Compatibility alias for start_task()."""
        return await self.start_task(task)

    async def continue_task(self, follow_up: str) -> AxisResult:
        """Append a follow-up and force an immediate planner pass.

        Keeps bounded task context, extracted data, completed goals, and the
        live browser runtime with its open tabs. The browser is only re-bound
        if the previous run lost its tab.
        """
        if self.state is None:
            return await self.start_task(follow_up)
        state = self.state
        state.add_followup(follow_up)
        added = extract_requirements(follow_up)
        state.requirements = _merge_requirements(state.requirements, added)
        if added.require_download:
            state.downloads.clear()
            state.started_wallclock = time.time()
            self.browser.task_started_wallclock = state.started_wallclock
        if added.require_screenshot:
            state.evidence = [item for item in state.evidence if item.kind != "screenshot"]
        state.last_error = None
        state.counters.consecutive_failures = 0
        state.counters.no_progress_steps = 0
        state.counters.repeated_actions = 0
        state.counters.repeated_observations = 0
        state.progress_by_tab.clear()
        state.made_progress = False
        state.counters.navigator_steps_since_plan = self.config.run.planner_interval_steps
        state.current_goal = None
        state.latest_navigator_outcome = None
        state.status = "active"
        state.completion_rejected = False
        state.repair_attempted = False
        state.direct_navigation_done = False
        state.started_monotonic = time.monotonic()
        state.model_ms = state.browser_ms = state.plan_ms = state.step_ms = state.tool_model_ms = 0
        state.cancellation_token = type(state.cancellation_token)()
        try:
            self._set_navigator_tools()
            self._reconcile_bound_tab()
            return await self._loop()
        except RunCancelled:
            return self._result("cancelled", reason="The user cancelled the run.")
        finally:
            self._close_telemetry()

    def _set_navigator_tools(self) -> None:
        state = self.state
        assert state is not None
        self._sync_navigator_tools(
            capture=evidence_requested(state.original_request, *state.follow_ups),
            diagnose=diagnostics_requested(state.requirements),
            downloads=state.requirements.require_download,
            exact=True,
        )

    def _sync_navigator_tools(
        self, *, capture: bool = False, diagnose: bool = False, downloads: bool = False,
        visual: bool = False,
        exact: bool = False,
    ) -> None:
        """Turn on an opt-in tool when the run actually needs it.

        ``browser_capture_evidence`` is added when the user asked for a
        screenshot/artifact, and ``browser_diagnose`` after a genuine failure
        worth investigating — matching the spec's "add only when requested"
        rule while making sure a task that asks for a screenshot is not left
        without the one tool that can take it.
        """
        want_capture = (capture or self.config.tools.capture_evidence) if exact else (
            self._capture_enabled or capture or self.config.tools.capture_evidence
        )
        want_diagnose = (diagnose or self.config.tools.diagnose) if exact else (
            self._diagnose_enabled or diagnose or self.config.tools.diagnose
        )
        want_downloads = (downloads or self.config.tools.downloads) if exact else (
            self._downloads_enabled or downloads or self.config.tools.downloads
        )
        want_visual = visual and self.config.run.visual_mode == "auto" and not (self.state and self.state.vision_disabled)
        previous_visual = self._visual_enabled
        if (want_capture, want_diagnose, want_downloads, want_visual) == (
            self._capture_enabled, self._diagnose_enabled, self._downloads_enabled, self._visual_enabled,
        ):
            return
        self._capture_enabled, self._diagnose_enabled, self._downloads_enabled = (
            want_capture, want_diagnose, want_downloads,
        )
        self._visual_enabled = want_visual
        if self.navigator_factory is None:
            return
        options = dict(capture=want_capture, diagnose=want_diagnose, downloads=want_downloads)
        if want_visual or previous_visual:
            options["visual"] = want_visual
        self.navigator = self.navigator_factory(**options)
        self._emit(
            "status", phase="tools_changed", capture=want_capture,
            diagnose=want_diagnose, downloads=want_downloads,
            visual=want_visual,
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
        state = self.state
        assert state is not None
        run = self.config.run
        if state.status == "cancelled":
            return self._result("cancelled", reason="The user cancelled the run.")
        if state.status == "paused":
            return self._result("paused", reason="The run is paused; task and browser state are preserved.")
        if state.counters.total_steps >= run.max_total_steps:
            return self._result("limit_reached", reason=f"Reached the maximum of {run.max_total_steps} steps.")
        if int(state.usage.requests) >= run.max_model_requests:
            return self._result("limit_reached", reason="Reached the maximum number of model requests.")
        if state.counters.browser_actions >= run.max_browser_actions:
            return self._result("limit_reached", reason="Reached the maximum number of browser actions.")
        if state.counters.consecutive_failures >= run.max_consecutive_failures:
            return self._result("failed", reason=f"{state.counters.consecutive_failures} consecutive failures.")
        if state.counters.no_progress_steps >= 3:
            return self._result("failed", reason="NO_PROGRESS: Three equivalent attempts produced no useful change.")
        return None

    def _result(self, status: str, *, answer: str | None = None, evidence: list[str] | None = None,
                reason: str = "") -> AxisResult:
        state = self.state
        if state:
            state.status = status  # type: ignore[assignment]
        result = AxisResult(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            evidence=evidence or [],
            reason=reason,
            total_steps=state.counters.total_steps if state else 0,
            planner_passes=state.counters.planner_passes if state else 0,
            browser_actions=state.counters.browser_actions if state else 0,
            model_requests=int(state.usage.requests) if state else 0,
            duration_ms=self._elapsed_ms(),
            # Agent.run includes time spent executing tools. Subtract the
            # measured bridge portion to avoid double-counting it as model time.
            model_ms=max(0, state.model_ms - state.tool_model_ms) if state else 0,
            browser_ms=state.browser_ms if state else 0,
            input_tokens=int(state.usage.input_tokens) if state else 0,
            output_tokens=int(state.usage.output_tokens) if state else 0,
            limitations=["The configured provider rejected image input; visual recovery was disabled for this run."]
            if state and state.vision_disabled else [],
        )
        self._emit(
            "final", status=result.status, reason=result.reason,
            duration_ms=result.duration_ms, model_ms=result.model_ms, browser_ms=result.browser_ms,
            debug_artifacts=None,
        )
        return result

    def _close_telemetry(self) -> None:
        state = self.state
        if state and state.debug_telemetry_started:
            artifacts = self.browser.stop_debug_telemetry()
            state.debug_telemetry_started = False
            if artifacts:
                state.evidence.append(Evidence(kind="observation", detail=f"Debug telemetry: {artifacts}"))

    # -- main loop -------------------------------------------------------

    async def _loop(self) -> AxisResult:
        state = self.state
        assert state is not None
        while True:
            stop = self._stop_reason()
            if stop is not None:
                return stop
            try:
                plan = await self._plan()
            except _Stop as stopped:
                return stopped.result

            state.counters.planner_passes += 1
            state.counters.navigator_steps_since_plan = 0
            self._emit("planner_decision", **plan.model_dump(), duration_ms=state.plan_ms)

            if plan.decision == "complete":
                verdict = verify_completion(state, state.last_browser_state)
                if not verdict.valid:
                    state.completion_rejected = True
                    state.last_error = "Completion rejected: " + " ".join(verdict.reasons)
                    state.current_goal = state.current_goal or "Verify every unresolved completion requirement."
                    continue
                return self._result(
                    "completed", answer=plan.final_answer, evidence=plan.evidence, reason=plan.reason,
                )
            if plan.decision == "ask_user":
                return self._result("needs_user", answer=plan.final_answer, evidence=plan.evidence,
                                    reason=plan.reason)
            if plan.decision == "fail":
                return self._result("failed", evidence=plan.evidence, reason=plan.reason)

            # A repair stays attached to the unfinished goal; changing its wording
            # never silently completes it or abandons pending changes.
            if not state.current_goal_id or (plan.next_goal != state.current_goal and not state.pending_changes):
                state.goal_number += 1
                state.current_goal_id = f"goal_{state.goal_number}"
                state.current_verification = plan.verification
            elif state.current_verification is None and plan.verification is not None:
                state.current_verification = plan.verification
                for pending in state.pending_changes.values():
                    if pending.goal_id == state.current_goal_id and pending.verification is None:
                        pending.verification = plan.verification
            elif plan.verification != state.current_verification:
                self._refine_verification(plan.verification)
            state.current_plan_summary = plan.plan_summary
            state.current_goal = self._preserve_literal_urls(plan.next_goal)
            state.current_success_condition = plan.success_condition
            state.repair_attempted = False

            try:
                await self._navigator_burst()
            except _Stop as stopped:
                return stopped.result

    async def _navigator_burst(self) -> None:
        """Run navigator steps until the planner is due again."""
        state = self.state
        assert state is not None
        while True:
            stop = self._stop_reason()
            if stop is not None:
                raise _Stop(stop)

            browser_state = await self._browser_state()
            # The preceding action's effect is only known once the next page
            # observation arrives, including asynchronously rendered updates.
            self._update_progress(browser_state, [])
            stop = self._stop_reason()
            if stop is not None:
                raise _Stop(stop)
            if state.counters.no_progress_steps == 2 and state.counters.navigator_steps_since_plan:
                state.last_error = "NO_PROGRESS: Two equivalent attempts made no useful change; choose a different strategy."
                return
            outcome, gate = await self._navigate(browser_state)

            state.counters.total_steps += 1
            state.counters.navigator_steps_since_plan += 1
            state.latest_navigator_outcome = outcome
            self._commit_gate(gate)
            # Follow a tab the navigator switched to, so the next observation
            # targets the page it actually moved to rather than the one it
            # left — otherwise it must burn a whole step re-activating.
            failures = [record for record in gate.records if not record.success]
            recoverable = {"STALE_REFERENCE", "REF_NOT_FOUND", "LOCATOR_ACTIONABILITY_TIMEOUT", "LOCATOR_NOT_FOUND", "LOCATOR_STRICT_MODE_VIOLATION"}
            tab = browser_state.tab
            if tab:
                targeting = [r for r in failures if r.tab == tab and r.code in recoverable]
                if targeting:
                    state.visual_failures[tab] = state.visual_failures.get(tab, 0) + len(targeting)
                elif not failures:
                    state.visual_failures[tab] = 0
                if ((outcome.needs_visual or state.visual_failures.get(tab, 0) >= 2)
                        and not gate.non_retryable and self.config.run.visual_mode == "auto"
                        and tab not in state.visual_tabs
                        and not state.vision_disabled):
                    state.visual_tabs.add(tab)
                    outcome.status = "continue"
                    outcome.reason = "Visual recovery requested; a fresh screenshot follows."
                    # A recoverable targeting failure gets this one different
                    # approach before exhausting the existing failure budget.
                    state.counters.consecutive_failures = 0
            # A goal_reached decision with no new mutation is a semantic
            # assessment of the fresh pre-step BrowserState. Do not grant the
            # same status to a step that just changed the page: that change
            # still needs a later observation or passing assertion.
            if outcome.status == "goal_reached" and failures:
                outcome.status = "continue"
                outcome.reason = failures[-1].error or "A required browser call did not succeed."
            rejection = None
            if outcome.status == "goal_reached":
                pending = [p for p in state.pending_changes.values() if p.goal_id == state.current_goal_id]
                known = {item.id for item in state.evidence if item.verified and item.goal_id == state.current_goal_id}
                if pending:
                    rejection = "Goal verification required: run the declared check after the changes on " + ", ".join(p.tab for p in pending) + "."
                elif not known or any(ref not in known for ref in outcome.evidence_ids):
                    rejection = "Unknown or unverified evidence reference; cite runtime evidence IDs."
                if rejection:
                    outcome.status = "continue"
                    outcome.reason = rejection
            state.last_error = rejection or (failures[-1].error if failures else None)
            if any(RECOVERY_POLICY.get(record.code or "") == "reconcile_tab" for record in failures):
                # Tab liveness is controller state. Reconcile it once instead
                # of asking the model to guess or repeat an internal tab id.
                self._reconcile_bound_tab()
            for item in outcome.evidence:
                state.add_evidence("navigator", item, tab=self.tab, verified=False)
            self._update_progress(browser_state, gate.records)
            self._emit(
                "navigator_step",
                **outcome.model_dump(),
                actions=gate.actions_used,
                interrupted=gate.interrupt_reason,
                duration_ms=state.step_ms,
                browser_ms=gate.browser_ms,
            )

            if gate.stopped == "cancelled":
                raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
            if gate.stopped == "paused":
                raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))

            if failures:
                state.counters.consecutive_failures += 1
            else:
                state.counters.consecutive_failures = 0
                state.repair_attempted = False

            # Back to the planner immediately.
            if outcome.status != "continue":
                if outcome.status == "goal_reached" and state.current_goal:
                    state.complete_goal(f"{state.current_goal} -> {outcome.summary}")
                    state.current_goal = None
                    state.current_goal_id = None
                return
            if rejection:
                return
            if gate.unknown_outcome:
                state.last_error = (
                    f"{state.last_error or 'An action had an unknown outcome.'} "
                    "It was not repeated; verify the page before acting again."
                )
                return
            if gate.non_retryable:
                return
            if failures:
                if state.repair_attempted:
                    return  # one repair attempt only
                state.repair_attempted = True
            if state.counters.no_progress_steps >= 2:
                state.last_error = "NO_PROGRESS: Two equivalent attempts made no useful change; choose a different strategy."
                return
            interval = self.config.run.planner_interval_steps
            if state.made_progress and not failures:
                interval = max(interval, self.config.run.planner_max_interval_steps)
            if state.counters.navigator_steps_since_plan >= interval:
                return

    # -- planner ---------------------------------------------------------

    async def _plan(self) -> PlanDecision:
        state = self.state
        assert state is not None
        run = self.config.run
        context: dict[str, Any] = {
            "task_id": state.task_id,
            "original_request": state.original_request,
            "follow_ups": state.follow_ups,
            "current_request": state.follow_ups[-1] if state.follow_ups else state.original_request,
            "requirements": state.requirements.model_dump(mode="json"),
            "current_plan_summary": state.current_plan_summary,
            "current_goal": state.current_goal,
            "success_condition": state.current_success_condition,
            "verification": state.current_verification.model_dump() if state.current_verification else None,
            "completed_goals": state.completed_goal_summaries,
            "latest_navigator_outcome": (
                state.latest_navigator_outcome.model_dump() if state.latest_navigator_outcome else None
            ),
            "recent_action_results": [record.model_dump() for record in state.actions[-state.max_recent_actions:]],
            "relevant_extracted_data": state.relevant_extracted_data if not state.facts else [],
            "facts": [fact.model_dump() for fact in state.facts],
            "memory_truncated": state.memory_truncated,
            "pending_changes": [change.model_dump() for change in state.pending_changes.values()],
            "evidence": [item.model_dump() for item in state.evidence[-16:] if item.verified],
            "last_error": state.last_error,
            "unverified_page_changes": state.unverified_mutations,
            "remaining_steps": max(0, run.max_total_steps - state.counters.total_steps),
            "remaining_browser_actions": max(0, run.max_browser_actions - state.counters.browser_actions),
            "remaining_model_requests": max(0, run.max_model_requests - int(state.usage.requests)),
        }
        if self.tab is not None and self.tab in self.browser.tabs:
            context["browser"] = {
                "tab": self.tab,
                "url": self.browser.safe_url(self.browser.tabs[self.tab].url),
                "title": self.browser.tabs[self.tab].title,
            }
        self._emit("status", phase="planner_started", steps_since_plan=state.counters.navigator_steps_since_plan)
        started = time.monotonic()
        decision = await self._run_agent(
            self.planner,
            format_as_xml(context, root_tag="task_context"),
            deps=None,
        )
        state.plan_ms = int((time.monotonic() - started) * 1000)
        return decision

    # -- navigator -------------------------------------------------------

    def _commit_gate(self, gate: StepGate) -> None:
        """Persist a step gate exactly once, including cancelled runs."""
        if gate.committed:
            return
        state = self.state
        assert state is not None
        state.record_actions(gate.records)
        state.counters.browser_actions += gate.actions_used
        for record, (mutated, verified) in zip(gate.records, gate.effects):
            if not record.sequence:
                self._register_action(record, mutated)
        if gate.active_tab_changed_to and gate.active_tab_changed_to in self.browser.tabs:
            self.tab = gate.active_tab_changed_to
        gate.committed = True

    async def _navigate(self, state: BrowserState) -> tuple[NavigatorOutcome, StepGate]:
        task = self.state
        assert task is not None
        run = self.config.run
        gate = StepGate(
            max_actions=run.max_actions_per_step,
            remaining_total_actions=max(0, run.max_browser_actions - task.counters.browser_actions),
            approval_actions=frozenset(self.config.tools.approval_required_actions),
            approval=self.approval,
            forbidden_actions=frozenset(task.requirements.forbidden_actions),
            mutation_allowed=task.requirements.mutation_allowed,
            is_paused=lambda: bool(self.state and self.state.status == "paused"),
            is_cancelled=lambda: bool(self.state and self.state.status == "cancelled"),
            on_action=lambda record, duration_ms: self._emit(
                "browser_action", tool=record.tool, operation=record.operation, args=record.args,
                success=record.success, error=record.error, refs_invalidated=record.refs_invalidated,
                skipped=False, duration_ms=duration_ms,
            ),
            on_refusal=lambda tool, operation, code, message, args: self._emit(
                "browser_action", tool=tool, operation=operation, args=args,
                success=False, error=f"{code}: {message}", refs_invalidated=False, skipped=True,
            ),
            on_record=self._register_action,
        )
        deps = AxisDeps(browser=self.browser, gate=gate)
        self._emit("status", phase="navigator_step_started", goal=state.goal)
        prompt = format_as_xml(state.model_dump(exclude_none=True), root_tag="browser_state")
        model_prompt = [prompt, self.browser.visual_content(state.screenshot)] if state.screenshot else prompt
        started = time.monotonic()
        try:
            try:
                outcome = await self._run_agent(self.navigator, model_prompt, deps=deps)
            except _VisionUnavailable:
                task.vision_disabled = True
                task.visual_tabs.clear()
                self.browser.visual_enabled = False
                self.browser.invalidate_visual()
                self._sync_navigator_tools(visual=False)
                task.last_error = "The configured provider does not support image input; continuing with text."
                self._emit("status", phase="vision_unavailable", reason=task.last_error)
                outcome = await self._run_agent(self.navigator, prompt + "\n" + task.last_error, deps=deps)
        except BaseException:
            task.browser_ms += gate.browser_ms
            task.tool_model_ms += gate.browser_ms
            task.step_ms = int((time.monotonic() - started) * 1000)
            self._commit_gate(gate)
            raise
        task.browser_ms += gate.browser_ms
        task.tool_model_ms += gate.browser_ms
        task.step_ms = int((time.monotonic() - started) * 1000)
        return outcome, gate

    async def _run_agent(self, agent: Agent[Any, Any], prompt: Any, *, deps: Any) -> Any:
        """One model request, with the shared usage record and limits.

        Pause/cancel are checked here, immediately before the request.
        """
        state = self.state
        assert state is not None
        if state.status == "cancelled":
            raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
        if state.status == "paused":
            raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))
        started = time.monotonic()
        try:
            result = await agent.run(
                prompt, deps=deps, usage=state.usage, usage_limits=self.usage_limits,
                cancellation_token=state.cancellation_token,
            )
        except UsageLimitExceeded as exc:
            raise _Stop(self._result("limit_reached", reason=clip(str(exc)) or "Usage limit exceeded.")) from exc
        except ModelHTTPError as exc:
            status = getattr(exc, "status_code", None)
            if (isinstance(prompt, list) and deps is not None and deps.gate.actions_used == 0
                    and status in {400, 422} and re.search(
                        r"(?:image|vision|multimodal).*?(?:not supported|unsupported|not allowed)|"
                        r"(?:does not support|unsupported|cannot accept).*?(?:image|vision|multimodal)",
                        str(exc.body), re.IGNORECASE | re.DOTALL)):
                raise _VisionUnavailable() from exc
            reason = (
                "Provider authentication failed."
                if status in (401, 403)
                else f"The model provider returned an error ({status})."
            )
            raise _Stop(self._result("failed", reason=reason)) from exc
        except RunCancelled:
            state.status = "cancelled"
            raise
        finally:
            state.model_ms += int((time.monotonic() - started) * 1000)
        return result.output

    # -- browser ---------------------------------------------------------

    def _reconcile_bound_tab(self) -> list[dict[str, Any]]:
        """Refresh tab liveness while retaining a valid task-scoped alias."""
        state = self.state
        assert state is not None
        previous_alias = state.bound_tab
        previous = self.browser.tabs.get(previous_alias) if previous_alias else None
        previous_identity = (
            self.browser.safe_url(previous.url), previous.title
        ) if previous is not None else (None, None)
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
        live = {item["tab"] for item in tabs}
        if state.bound_tab not in live:
            state.bound_tab = None
            previous_url, previous_title = previous_identity
            matches = [
                item for item in tabs
                if previous_url and item.get("url") == previous_url
                and (not previous_title or item.get("title") == previous_title)
            ]
            if len(matches) == 1:
                # Chrome may replace a tab underneath us while retaining its
                # public identity. Rebind to the unique alias; raw ids never
                # leave BrowserRuntime.
                self.tab = matches[0]["tab"]
        return tabs

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
        state = self.state
        assert state is not None
        ctx = browser_context(self.browser)
        tabs = self._reconcile_bound_tab()
        if self.tab is not None:
            cached = self.browser.tabs.get(self.tab)
            if cached is not None and not cached.closed and not _unobservable(cached.url):
                return self.tab

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
        state = self.state
        assert state is not None
        candidates = list(dict.fromkeys(([self.tab] if self.tab else []) + state.task_tabs[-7:]))
        return [
            TabSummary(**self.browser.public_tab(alias))
            for alias in candidates
            if alias in self.browser.tabs and not self.browser.tabs[alias].closed
        ]

    def _current_request(self) -> str:
        state = self.state
        assert state is not None
        return state.follow_ups[-1] if state.follow_ups else state.original_request

    def _preserve_literal_urls(self, goal: str | None) -> str | None:
        state = self.state
        assert state is not None
        if not goal or not state.requirements.literal_urls:
            return goal
        missing = [url for url in state.requirements.literal_urls if url not in goal]
        if len(state.requirements.literal_urls) == 1 and missing:
            return clip(f"{goal} Target URL: {missing[0]}")
        return goal

    def _direct_url(self) -> str | None:
        state = self.state
        assert state is not None
        current_urls = extract_requirements(self._current_request()).literal_urls
        urls = current_urls or state.requirements.literal_urls
        if state.direct_navigation_done or len(urls) != 1:
            return None
        if _CURRENT_PAGE_DEPENDENCY.search(self._current_request()):
            return None
        return urls[0]

    def _maybe_direct_navigate(self, tab: str) -> None:
        state = self.state
        assert state is not None
        url = self._direct_url()
        if not url:
            return
        current = self.browser.tabs.get(tab)
        if current and current.url and current.url.rstrip("/") == url.rstrip("/"):
            state.direct_navigation_done = True
            return
        result = bt.browser_navigate(
            browser_context(self.browser), tab, bt.Open(operation="open", url=url),
        )
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        record = ActionRecord(
            tool="browser_navigate", tab=tab, operation="open", args=f"tab={tab!r}, url={url!r}",
            executed=True, execution_success=bool(result.get("ok")), semantic_success=None,
            code=error.get("code"), message=error.get("message"), refs_invalidated=bool(result.get("ok")),
            meaningful_change=bool(result.get("ok")), retain_in_memory=True,
            extracted_data=f"navigated to {url!r}" if result.get("ok") else None,
        )
        state.record_actions([record])
        state.counters.browser_actions += 1
        self._register_action(record, record.success)
        if not record.success:
            state.last_error = record.error
            return
        state.direct_navigation_done = True

    def _arm_diagnostics(self, tab: str) -> None:
        state = self.state
        assert state is not None
        if not diagnostics_requested(state.requirements) or tab in state.diagnostics_armed_tabs:
            return
        raw = self.browser.tabs.get(tab)
        if raw is None:
            return
        armed = True
        for required, method in (
            (state.requirements.require_console_check, "console.read"),
            (state.requirements.require_network_check, "network.read"),
        ):
            if required:
                try:
                    self.browser._rpc(method, {"tabId": raw.raw_id, "limit": 1}, tab=tab)
                except ToolFault as fault:
                    state.last_error = f"Could not arm {method}: {fault.code}."
                    armed = False
        if armed:
            state.diagnostics_armed_tabs.add(tab)

    async def _browser_state(self) -> BrowserState:
        """A fresh, bounded observation before every navigator step.

        Calls ``BrowserRuntime.observe`` directly — the same method the
        ``browser_observe`` tool calls — so observation logic exists once.
        """
        state = self.state
        assert state is not None
        run = self.config.run
        tab = self._ensure_tab()
        self._arm_diagnostics(tab)
        self._maybe_direct_navigate(tab)
        if self.config.tools.debug_recording and not state.debug_telemetry_started:
            self.browser.start_debug_telemetry(tab)
            state.debug_telemetry_started = bool(self.browser.debug_recording or self.browser.debug_trace)

        observation: dict[str, Any] | None = None
        last: ToolFault | None = None
        observe_started = time.monotonic()
        for attempt in range(2):  # one tab reconciliation/observation repair, then stop
            try:
                observation = self.browser.observe(tab, run.observation_mode, run.observation_max_nodes)
                break
            except ToolFault as fault:
                last = fault
                if RECOVERY_POLICY.get(fault.code) == "reconcile_tab" and attempt == 0:
                    self.tab = None
                    tab = self._ensure_tab()
                    self._arm_diagnostics(tab)
                    continue
                if fault.code in FATAL_BRIDGE_CODES or not fault.retryable or attempt == 1:
                    break
        observe_ms = int((time.monotonic() - observe_started) * 1000)
        state.browser_ms += observe_ms
        self._emit(
            "status", phase="observed", tab=tab, success=observation is not None,
            duration_ms=observe_ms,
        )
        if observation is None:
            reason = clip(str(last)) if last else "The browser could not be observed."
            raise _Stop(self._result("failed", reason=reason or "The browser could not be observed."))
        want_visual = (tab in state.visual_tabs or run.include_screenshot or self.config.tools.visual)
        want_visual = want_visual and run.visual_mode == "auto" and not state.vision_disabled
        self.browser.visual_enabled = want_visual
        self._sync_navigator_tools(visual=want_visual)
        visual = self._screenshot(tab) if want_visual else None
        browser_state = BrowserState(
            tab=tab,
            url=observation.get("url"),
            title=observation.get("title"),
            tabs=self._tab_summaries(),
            scroll=observation.get("scroll"),
            snapshot=observation.get("snapshot", ""),
            truncated=bool(observation.get("truncated")) or len(observation.get("snapshot", "")) > 12_000,
            screenshot=visual.get("screenshot") if visual else None,
            visual=visual,
            refs_fresh=tab in self.browser.observations,
            unverified_page_changes=state.unverified_mutations,
            site_pattern=observation.get("sitePattern"),
            runtime_warning=observation.get("runtimeWarning"),
            recent_actions=state.actions[-4:],
            goal=state.current_goal,
            success_condition=state.current_success_condition,
            verification=state.current_verification,
            remaining_steps=max(0, run.max_total_steps - state.counters.total_steps),
            remaining_actions=min(
                run.max_actions_per_step, max(0, run.max_browser_actions - state.counters.browser_actions)
            ),
            note=state.last_error,
            requirements=state.requirements,
            goal_id=state.current_goal_id,
            facts=state.facts,
            memory_truncated=state.memory_truncated,
            pending_changes=list(state.pending_changes.values()),
        )
        state.last_browser_state = browser_state
        evidence = state.add_evidence("observation", f"Title: {browser_state.title or ''}\n{browser_state.snapshot}", tab=tab, source_url=browser_state.url)
        browser_state.evidence_id = evidence.id
        state.retain_fact(TaskFact(value=evidence.detail, tab=tab, source_url=browser_state.url,
                                   goal_id=state.current_goal_id, evidence_id=evidence.id,
                                   target="page", priority=0))
        # Navigation has a concrete URL postcondition. This never verifies an
        # input/click merely because the resulting page was observed.
        for key, pending in list(state.pending_changes.items()):
            if (pending.tab == tab and pending.goal_id == state.current_goal_id
                    and pending.expected_url and pending.expected_url == browser_state.url):
                state.counters.verified_mutation_count += pending.count
                del state.pending_changes[key]
        browser_state.unverified_page_changes = state.unverified_mutations
        browser_state.pending_changes = list(state.pending_changes.values())
        browser_state.facts = state.facts
        browser_state.memory_truncated = state.memory_truncated
        return browser_state

    def _register_action(self, record: ActionRecord, mutated: bool) -> None:
        state = self.state
        assert state is not None
        record.sequence = state.next_sequence()
        record.goal_id = state.current_goal_id
        if record.tool == "browser_downloads" and record.tab is None:
            record.tab = state.bound_tab
        if record.target:
            reference = re.search(r'"ref"\s*:\s*"(ref_\d+)"', record.target)
            if reference:
                record.target = self.browser.describe_ref(reference[1]) or record.target
        tab = self.browser.tabs.get(record.tab) if record.tab else None
        record.source_url = self.browser.safe_url(tab.url) if tab else None
        if mutated and record.tab:
            key = f"{record.goal_id}:{record.tab}"
            previous = state.pending_changes.get(key)
            check = state.current_verification
            if check is None and record.operation == "fill" and record.expected_value is not None:
                # Straightforward input goals have a deterministic target/value
                # postcondition even with an older planner specification.
                target = record.target
                names = re.findall(r'"([^"\n]+)"', target or "")
                if target and not target.startswith("{") and names:
                    target = names[0]
                elif target and target.startswith("{"):
                    locator = json.loads(target).get("locator", {})
                    target = next((locator[k] for k in ("name", "label", "text", "placeholder", "selector") if locator.get(k)), None)
                check = GoalCheck(assertion="value", expected=record.expected_value, target=target) if target else None
                if previous and previous.verification != check:
                    check = None
            state.pending_changes[key] = PendingChange(
                goal_id=record.goal_id, tab=record.tab, sequence=record.sequence,
                count=(previous.count if previous else 0) + 1,
                expected_url=record.source_url if record.tool == "browser_navigate" and not previous else None,
                verification=check,
            )
            state.counters.mutation_count += 1
        self._collect_record_evidence(record)

    def _collect_record_evidence(self, record: ActionRecord) -> None:
        state = self.state
        assert state is not None
        if not record.executed:
            return
        data = record.result_data
        def evidence(kind: str, detail: str, verified: bool = True) -> Evidence:
            item = state.add_evidence(kind, detail, tab=record.tab, source_url=record.source_url,
                                      sequence=record.sequence, verified=verified)
            record.evidence_id = item.id
            return item

        if record.tool == "browser_assert":
            assertion = AssertionRecord(
                assertion=record.operation or "unknown",
                passed=record.semantic_success is True,
                expected=data.get("expected"),
                tab=record.tab,
                message=record.message,
                target=record.target,
                goal_id=record.goal_id,
                sequence=record.sequence,
                match=data.get("match"),
            )
            item = evidence("assertion", f"{assertion.assertion} {record.target} passed={assertion.passed} expected={assertion.expected!r}", assertion.passed)
            assertion.evidence_id = item.id
            state.assertions = (state.assertions + [assertion])[-128:]
            key = f"{record.goal_id}:{record.tab}"
            pending = state.pending_changes.get(key)
            if (assertion.passed and pending and record.sequence > pending.sequence
                    and self._matches_goal_check(pending.verification, record)):
                state.counters.verified_mutation_count += pending.count
                del state.pending_changes[key]
        elif record.success and record.tool in {"browser_observe", "browser_navigate", "browser_tabs"}:
            kind = "navigation" if record.tool == "browser_navigate" else (
                "observation" if record.tool == "browser_observe" and "snapshot" in data else "extraction")
            content = record.extracted_data
            if content is not None:
                item = evidence(kind, content)
                state.retain_fact(TaskFact(value=content, tab=record.tab, source_url=record.source_url,
                                          goal_id=record.goal_id, evidence_id=item.id,
                                          tool=record.tool,
                                          target=record.target or "page", priority=0 if kind == "observation" else 1))
        elif record.tool == "browser_capture_evidence" and record.success:
            capture = data.get("capture")
            if capture in {"page_screenshot", "element_screenshot"}:
                if data.get("saved"):
                    evidence("screenshot", f"Screenshot {data.get('evidence')}: {data.get('path') or ''}")
        elif record.tool == "browser_downloads" and record.success:
            candidates = data.get("items", [data.get("download")])
            for candidate in candidates if isinstance(candidates, list) else []:
                if not isinstance(candidate, dict) or candidate.get("state") != "complete" or candidate.get("exists") is False:
                    continue
                try:
                    started = datetime.fromisoformat(candidate.get("startTime", "").replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError, AttributeError):
                    continue
                if started < state.started_wallclock or candidate.get("taskEligible") is False:
                    continue
                if not candidate.get("filename"):
                    continue
                check = state.current_verification
                if check is None or check.assertion != "download" or not isinstance(check.expected, str) or not check.expected:
                    continue
                filename = candidate["filename"].replace("\\", "/").rsplit("/", 1)[-1]
                if check.expected not in {filename, candidate.get("url"), candidate.get("finalUrl")}:
                    continue
                item = evidence("download", json.dumps(candidate, ensure_ascii=False))
                state.downloads = (state.downloads + [item])[-16:]
                for key, pending in list(state.pending_changes.items()):
                    if (pending.goal_id == state.current_goal_id and pending.verification == check
                            and pending.tab == record.tab
                            and record.sequence > pending.sequence):
                        state.counters.verified_mutation_count += pending.count
                        del state.pending_changes[key]
        elif record.tool == "browser_diagnose" and record.success and record.operation in {"console", "network"}:
            detail = str(data.get("events", []))[:4_000]
            item = evidence(record.operation, detail or "No events were returned.")
            state.diagnostic_evidence = (state.diagnostic_evidence + [item])[-32:]
            state.retain_fact(TaskFact(value=detail, tab=record.tab, source_url=record.source_url,
                                      goal_id=record.goal_id, evidence_id=item.id,
                                      tool=record.tool, target=record.operation, priority=1))

    def _refine_verification(self, check: GoalCheck | None) -> bool:
        """Correct inferred wording only with an actual post-mutation exact check.

        Keep pending changes until a further check; retain superseded failures
        with their replacement evidence ID for an auditable correction.
        """
        state = self.state
        assert state is not None
        old = state.current_verification
        if (not old or not check or old == check or old.assertion != check.assertion
                or old.assertion not in {"text", "title", "value"}
                or (old.target and old.target != check.target)):
            return False
        user_text = "\n".join([state.original_request, *state.follow_ups,
                               *state.requirements.required_text]).casefold()
        if isinstance(old.expected, str) and old.expected.casefold() in user_text:
            return False
        pending = [p for p in state.pending_changes.values() if p.goal_id == state.current_goal_id]
        if not pending:
            return False
        replacements = []
        for change in pending:
            replacement = next((a for a in reversed(state.assertions)
                if a.passed and a.evidence_id and not a.superseded_by
                and a.goal_id == change.goal_id and a.tab == change.tab
                and a.sequence > change.sequence
                and self._matches_goal_check(check, ActionRecord(
                    tool="browser_assert", operation=a.assertion, target=a.target,
                    result_data={"expected": a.expected, "match": a.match}))), None)
            if replacement is None:
                return False
            replacements.append(replacement)
        state.current_verification = check
        for change, replacement in zip(pending, replacements):
            change.verification = check
            for assertion in state.assertions:
                if (not assertion.passed and assertion.goal_id == change.goal_id
                        and assertion.tab == change.tab and assertion.target == replacement.target
                        and assertion.assertion == old.assertion and assertion.expected == old.expected):
                    assertion.superseded_by = replacement.evidence_id
        self._emit("verification_refined", verification=check.model_dump(),
                   evidence_ids=[a.evidence_id for a in replacements])
        return True

    @staticmethod
    def _matches_goal_check(check: GoalCheck | None, record: ActionRecord) -> bool:
        if check is None or check.assertion != record.operation:
            return False
        actual = record.result_data.get("expected")
        if record.result_data.get("match") in {"regex", "contains"}:
            return False
        if check.expected is not None and actual != check.expected:
            return False
        if check.target:
            try:
                identity = json.loads(record.target or "{}")
                locator = identity.get("locator", {})
                targets = [str(v).casefold() for k, v in locator.items()
                           if k in {"name", "label", "text", "placeholder", "selector", "role"}]
                if check.target.casefold() not in targets:
                    return False
            except (ValueError, TypeError, AttributeError):
                return False
        return True

    @staticmethod
    def _normalized_url(url: str | None) -> str:
        if not url:
            return ""
        parts = urlsplit(url)
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))

    @classmethod
    def _page_fingerprint(cls, browser_state: BrowserState, records: list[ActionRecord]) -> str:
        text = re.sub(r"\[ref_\d+\]", "[ref]", browser_state.snapshot)
        text = " ".join(text.split())
        return hashlib.sha256(f"{cls._normalized_url(browser_state.url)}|{text}".encode()).hexdigest()

    def _update_progress(self, browser_state: BrowserState, records: list[ActionRecord]) -> None:
        state = self.state
        assert state is not None
        page_fingerprint = self._page_fingerprint(browser_state, records)
        tab = browser_state.tab or ""
        entry = state.progress_by_tab.setdefault(tab, {})
        if not records:
            state.made_progress = False
            pending = entry.pop("pending", None)
            if pending:
                changed = pending["page"] != page_fingerprint
                if changed or pending["positive"]:
                    entry["streak"] = 0
                    state.made_progress = True
                    for other in state.progress_by_tab.values():
                        other["streak"] = 0
                elif pending["action"] == entry.get("last_action"):
                    entry["streak"] = entry.get("streak", 0) + 1
                else:
                    entry["streak"] = 1
                entry["last_action"] = pending["action"]
            state.counters.repeated_observations = (
                state.counters.repeated_observations + 1 if entry.get("page") == page_fingerprint else 0)
            entry["page"] = page_fingerprint
            state.counters.no_progress_steps = entry.get("streak", 0)
            return
        for record in records:
            record_tab = record.tab or tab
            tracked = state.progress_by_tab.setdefault(record_tab, {})
            if record.success and record.tool in {"browser_observe", "browser_assert"}:
                text = record.extracted_data
                positive = record.tool == "browser_assert" or (text is not None and text != tracked.get("extracted"))
                if record.tool == "browser_observe" and "snapshot" in record.result_data:
                    observed = BrowserState(tab=record_tab, url=record.source_url or browser_state.url,
                                            snapshot=record.result_data["snapshot"])
                    fingerprint = self._page_fingerprint(observed, [])
                    positive = fingerprint != tracked.get("page")
                    tracked["page"] = fingerprint
                tracked["extracted"] = text
                if positive:
                    state.made_progress = True
                    tracked["streak"] = 0
                    state.counters.no_progress_steps = 0
                    if "pending" in tracked:
                        tracked["pending"]["positive"] = True
            if not record.success or record.tool not in {"browser_act", "browser_navigate", "browser_visual"} or record.operation == "capture":
                continue
            # Stable target descriptions distinguish two controls whose freshly
            # minted references happen to have different numbers each step.
            target = record.target or record.args or ""
            target = re.sub(r"\b(ref|screenshot)_\d+\b", r"\1", target)
            payload = re.sub(r"\b(ref|screenshot)_\d+\b", r"\1", record.args or "")
            action = f"{record.tool}|{record.operation}|{target}|{payload}"
            tracked["pending"] = {"page": tracked.get("page", page_fingerprint),
                                  "action": action, "positive": record.meaningful_change is True}

    def _screenshot(self, tab: str) -> dict[str, Any] | None:
        """Capture bounded visual input through the same execution gate."""
        state = self.state
        assert state is not None
        gate = StepGate(max_actions=1,
                        remaining_total_actions=self.config.run.max_browser_actions - state.counters.browser_actions,
                        approval_actions=frozenset(self.config.tools.approval_required_actions),
                        approval=self.approval, forbidden_actions=frozenset(state.requirements.forbidden_actions),
                        mutation_allowed=state.requirements.mutation_allowed,
                        is_paused=lambda: state.status == "paused", is_cancelled=lambda: state.status == "cancelled",
                        on_record=self._register_action)
        kwargs = {"tab": tab, "command": bt.VisualCapture(operation="capture")}
        current_url = self.browser.safe_url(self.browser.tab(tab).url)
        refused = gate.check("browser_visual", kwargs, {
            "tab": tab, "operation": "capture", "current_url": current_url,
            "expected_effect": "capture", "reason": "This action is configured to require user approval.",
        })
        if refused:
            self._commit_gate(gate)
            state.last_error = "Visual recovery unavailable: " + (self._envelope_error(refused) or "capture refused")
            state.visual_tabs.discard(tab)
            self._emit("status", phase="vision_unavailable", reason=state.last_error)
            return None
        started = time.monotonic()
        result = bt.browser_visual(browser_context(self.browser), **kwargs)
        elapsed = int((time.monotonic() - started) * 1000)
        gate.record("browser_visual", kwargs, result, elapsed)
        self._commit_gate(gate)
        state.browser_ms += elapsed
        if not result["ok"]:
            state.last_error = "Visual recovery unavailable: " + (self._envelope_error(result) or "capture failed")
            state.visual_tabs.discard(tab)
            self._emit("status", phase="vision_unavailable", reason=state.last_error)
            return None
        return result["data"]


__all__ = ["AxisOrchestrator", "EventCallback", "ApprovalCallback"]
