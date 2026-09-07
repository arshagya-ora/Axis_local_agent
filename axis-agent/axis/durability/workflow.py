"""Deterministic AXIS job orchestration; all external I/O is activity-backed."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, List, Optional

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
    from axis.approvals import decide_approval, resolve_recorded_approval
    from axis.durability.activities import (
        BrowserRebindRequest, BrowserRebindSelectionRequest, EmitEventRequest, LeaseReleaseRequest,
        LeaseRequest, acquire_browser_lease, emit_durable_event, get_worker_runtime_info,
        refresh_browser_binding, release_browser_lease, select_browser_rebind_candidate,
    )
    from axis.durability.models import (
        AxisContinuationState, AxisDurableDeps, AxisJobInput, AxisJobState, AxisJobStatus,
        AxisVersionManifest, BrowserRecoveryProjection, CancelSignal, DurableQuestion, DurableRuntimeSettings,
        LeaseProof, PauseSignal, RebindBrowserSignal, RebindCandidate, ResumeSignal, SelectRebindCandidateSignal,
        SubmitApprovalSignal, SubmitUserInputSignal,
    )
    from axis.durability.reconciliation import classify_reconciliation
    from axis.durability.runtime import get_durable_agent, mark_active_effect_unknown
    from axis.effects import EffectLedger
    from axis.models import AxisControlState, AxisTaskResult

_UNRESOLVED_FOR_HISTORY = frozenset({
    "prepared", "awaiting_approval", "approved", "starting", "executing",
    "executed_unverified", "unknown_after_crash", "reconciling",
})


@workflow.defn
class AxisJobWorkflow:
    def __init__(self) -> None:
        self._deps: Optional[AxisDurableDeps] = None
        self._status: AxisJobStatus = "created"
        self._task_summary = ""
        self._paused = False
        self._cancel_requested = False
        self._cancel_reason: Optional[str] = None
        self._rebind_requested = False
        self._rebind_target: Optional[BrowserRecoveryProjection] = None
        self._pending_question: Optional[DurableQuestion] = None
        self._user_input: Optional[str] = None
        self._result: Optional[AxisTaskResult] = None
        self._version_manifest: Optional[AxisVersionManifest] = None
        self._settings: Optional[DurableRuntimeSettings] = None
        self._last_error_code: Optional[str] = None
        self._run_segments = 0
        self._total_run_segments = 0
        self._last_reconciled_effect_id: Optional[str] = None
        self._rebind_candidates: List[RebindCandidate] = []
        self._rebind_selected_candidate_id: Optional[str] = None
        self._answer_history: List[str] = []

    async def _emit(self, event_type: str, *, detail: Optional[str] = None) -> None:
        await workflow.execute_activity(
            emit_durable_event,
            EmitEventRequest(run_id=self._deps.run_id if self._deps else "unknown", event_type=event_type, detail=detail),
            start_to_close_timeout=timedelta(seconds=10),
        )

    @workflow.signal
    def submit_approval(self, signal: SubmitApprovalSignal) -> None:
        if self._deps is None:
            return
        state = self._deps.control_state.approval_state
        match = next(((call_id, request) for call_id, request in state.requests_by_tool_call_id.items()
                      if request.approval_id == signal.request_id and not request.resolved), None)
        if match is None:
            return
        decide_approval(
            state, tool_call_id=match[0], request_id=signal.request_id,
            decision_nonce=signal.decision_nonce, approved=signal.approved,
        )

    @workflow.signal
    def pause_job(self, signal: PauseSignal) -> None:
        self._paused = True

    @workflow.signal
    def resume_job(self, signal: ResumeSignal) -> None:
        self._paused = False

    @workflow.signal
    def cancel_job(self, signal: CancelSignal) -> None:
        if not self._cancel_requested:
            self._cancel_requested = True
            self._cancel_reason = signal.reason

    @workflow.signal
    def submit_user_input(self, signal: SubmitUserInputSignal) -> None:
        pending = self._pending_question
        if pending is None or pending.answered:
            return
        if signal.request_id != pending.request_id or not signal.answer_nonce:
            return
        pending.answered = True
        pending.answer_nonce = signal.answer_nonce
        self._user_input = signal.text

    @workflow.signal
    def rebind_browser(self, signal: RebindBrowserSignal) -> None:
        self._rebind_requested = True
        if self._deps is None:
            return
        control = self._deps.control_state
        ledger = EffectLedger(control.effects)
        for call_id, request in list(control.approval_state.requests_by_tool_call_id.items()):
            if not request.resolved:
                if any(effect.effect_id == request.effect_id for effect in control.effects):
                    ledger.mark_denied(request.effect_id)
                control.approval_state.requests_by_tool_call_id[call_id] = request.model_copy(
                    update={"resolved": True, "decided": False}
                )
        if self._rebind_target is None:
            self._rebind_target = self._deps.browser_projection.model_copy(deep=True)
        self._deps.browser_projection = self._deps.browser_projection.invalidated()
        control.observed_current_run = False
        control.assertion_passed_after_effect = False

    @workflow.signal
    def select_rebind_candidate(self, signal: SelectRebindCandidateSignal) -> None:
        if not any(c.candidate_id == signal.candidate_id for c in self._rebind_candidates):
            return
        self._rebind_selected_candidate_id = signal.candidate_id
        self._rebind_requested = True

    @workflow.query
    def get_status(self) -> AxisJobStatus:
        return self._status

    @workflow.query
    def get_rebind_candidates(self) -> List[RebindCandidate]:
        return self._rebind_candidates

    @workflow.query
    def get_job_state(self) -> AxisJobState:
        deps = self._deps
        control = deps.control_state if deps else AxisControlState()
        pending = next((r for r in control.approval_state.requests_by_tool_call_id.values() if not r.resolved), None)
        return AxisJobState(
            job_id=deps.job_id if deps else "", status=self._status, task_summary=self._task_summary,
            browser_scope_key=deps.browser_scope_key if deps else "", plan=control.plan,
            plan_revision=control.plan_revision, task_intent=control.task_intent, effects=control.effects,
            acceptance={effect.effect_key: effect.status in ("succeeded", "reconciled_succeeded") for effect in control.effects},
            pending_approval=pending, pending_question=self._pending_question, paused=self._paused,
            cancel_requested=self._cancel_requested,
            rebind_required=deps.browser_projection.rebind_required if deps else False,
            rebind_candidates=self._rebind_candidates,
            binding_generation=deps.browser_projection.binding_generation if deps else 0,
            observation_generation=deps.browser_projection.observation_generation if deps else 0,
            run_segments=self._total_run_segments, version_manifest=self._version_manifest,
            result=self._result, last_error_code=self._last_error_code,
        )

    @workflow.query
    def get_plan(self) -> List[Any]:
        return self._deps.control_state.plan if self._deps else []

    @workflow.query
    def get_pending_approval(self) -> Any:
        return self.get_job_state().pending_approval

    @workflow.query
    def get_effect_summary(self) -> List[Any]:
        return self._deps.control_state.effects if self._deps else []

    async def _load_and_check_worker_runtime(self) -> bool:
        info = await workflow.execute_activity(
            get_worker_runtime_info, start_to_close_timeout=timedelta(seconds=10),
        )
        if self._version_manifest is None:
            self._version_manifest = info.manifest
            self._settings = info.settings
            if self._deps:
                self._deps.leases_enabled = info.settings.leases_enabled
                self._deps.lease_duration_seconds = info.settings.lease_duration_seconds
                self._deps.expected_manifest = self._version_manifest
            return True
        if not self._version_manifest.is_compatible_with(info.manifest):
            self._last_error_code = "VERSION_INCOMPATIBLE"
            self._result = AxisTaskResult(
                status="failed", summary="The worker version is incompatible with this job.",
                error_code="VERSION_INCOMPATIBLE", retryable=False,
            )
            self._status = "failed"
            await self._emit("job_failed", detail="VERSION_INCOMPATIBLE")
            return False
        self._settings = info.settings
        if self._deps:
            # Stamp every scheduled tool activity's deps with the manifest
            # this workflow just confirmed is still compatible, so an
            # activity a differently-versioned worker happens to pick up
            # off the task queue can refuse itself (see
            # axis.durability.runtime.durable_browser_handler) instead of
            # running under mismatched assumptions.
            self._deps.expected_manifest = self._version_manifest
        return True

    def _lease_token(self) -> str:
        assert self._deps is not None
        return hashlib.sha256(f"{self._deps.job_id}:{workflow.info().run_id}".encode()).hexdigest()[:32]

    async def _try_acquire_lease(self) -> bool:
        assert self._deps is not None and self._settings is not None
        if not self._settings.leases_enabled:
            self._deps.lease_proof = None
            return True
        result = await workflow.execute_activity(
            acquire_browser_lease,
            LeaseRequest(
                scope_key=self._deps.browser_scope_key, job_id=self._deps.job_id,
                token=self._lease_token(), task_queue=workflow.info().task_queue,
                duration_seconds=self._settings.lease_duration_seconds,
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        if not result.acquired or result.owner is None:
            return False
        self._deps.lease_proof = LeaseProof(
            token=result.owner.token, generation=result.owner.generation, expires_at=result.owner.expires_at,
        )
        await self._emit("browser_lease_acquired")
        return True

    async def _acquire_lease(self) -> bool:
        assert self._settings is not None
        if await self._try_acquire_lease():
            return True
        if self._settings.lease_conflict_policy == "fail":
            return False
        deadline = workflow.now().timestamp() + self._settings.lease_acquire_timeout_seconds
        self._status = "waiting_for_browser"
        while workflow.now().timestamp() < deadline and not self._cancel_requested:
            remaining = deadline - workflow.now().timestamp()
            try:
                await workflow.wait_condition(
                    lambda: self._cancel_requested or self._rebind_requested,
                    timeout=min(1.0, remaining),
                )
            except TimeoutError:
                pass
            if await self._try_acquire_lease():
                self._status = "running"
                return True
        return False

    async def _release_lease(self) -> None:
        if self._deps is None or self._deps.lease_proof is None:
            return
        proof = self._deps.lease_proof
        self._deps.lease_proof = None
        await workflow.execute_activity(
            release_browser_lease,
            LeaseReleaseRequest(
                scope_key=self._deps.browser_scope_key, job_id=self._deps.job_id,
                token=proof.token, generation=proof.generation,
            ),
            start_to_close_timeout=timedelta(seconds=20),
        )
        await self._emit("browser_lease_released")

    async def _handle_pause(self) -> bool:
        if not self._paused:
            return True
        self._status = "paused"
        await self._emit("job_paused")
        await self._release_lease()
        await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
        if self._cancel_requested:
            return False
        if not await self._acquire_lease():
            self._failure("BROWSER_SCOPE_BUSY", "The browser lease could not be reacquired.", retryable=True)
            return False
        self._status = "running"
        await self._emit("job_resumed")
        return True

    async def _handle_rebind(self) -> bool:
        if not self._rebind_requested or self._deps is None:
            return True
        self._rebind_requested = False
        if self._settings is not None and self._settings.leases_enabled:
            # A rebind refresh is browser access too.  The prior proof may
            # have expired while this job was waiting, so reacquire/renew it
            # before touching the bridge and carry the new fencing
            # generation when expiry caused a takeover.
            if not await self._try_acquire_lease():
                self._status = "waiting_for_browser"
                await self._emit("job_waiting_for_browser")
                return False

        selected_candidate_id = self._rebind_selected_candidate_id
        self._rebind_selected_candidate_id = None
        if selected_candidate_id is not None:
            result = await workflow.execute_activity(
                select_browser_rebind_candidate,
                BrowserRebindSelectionRequest(deps=self._deps, candidate_id=selected_candidate_id),
                start_to_close_timeout=timedelta(seconds=30),
            )
        else:
            old = self._rebind_target or self._deps.browser_projection
            invalid = self._deps.browser_projection
            selection = invalid.model_copy(update={
                "durable_tab_key": old.durable_tab_key,
                "last_known_sanitized_url": old.last_known_sanitized_url,
                "last_known_title": old.last_known_title,
                "active_tab_intent": old.active_tab_intent,
            })
            result = await workflow.execute_activity(
                refresh_browser_binding,
                BrowserRebindRequest(deps=self._deps, previous_projection=selection),
                start_to_close_timeout=timedelta(seconds=30),
            )
        self._deps.browser_projection = result.projection
        self._rebind_candidates = result.candidates
        if result.projection.rebind_required:
            self._status = "waiting_for_browser"
            if result.candidates:
                # Ambiguous: bounded, opaque candidates are now queryable
                # via get_rebind_candidates(); select_rebind_candidate sets
                # _rebind_requested again once the user picks one, so this
                # never becomes an unresolvable loop as long as a fresh
                # rebind_browser/select_rebind_candidate signal keeps
                # driving another _handle_rebind attempt.
                await self._emit("job_rebind_ambiguous")
            else:
                await self._emit("job_waiting_for_browser")
            return False
        self._rebind_target = None
        self._rebind_candidates = []
        self._deps.control_state.observed_current_run = False
        await self._emit("browser_rebound")
        return True

    def _history_safe(self) -> bool:
        if self._deps is None or self._pending_question is not None:
            return False
        if any(not request.resolved for request in self._deps.control_state.approval_state.requests_by_tool_call_id.values()):
            return False
        return not any(effect.status in _UNRESOLVED_FOR_HISTORY for effect in self._deps.control_state.effects)

    async def _check_history_limit(self, job_input: AxisJobInput) -> None:
        assert self._settings is not None and self._deps is not None
        if self._run_segments < self._settings.max_run_segments:
            return
        if not self._history_safe():
            self._last_error_code = "HISTORY_LIMIT_REACHED"
            self._result = AxisTaskResult(
                status="failed", summary="The durable history limit was reached with unresolved state.",
                error_code="HISTORY_LIMIT_REACHED", retryable=False,
            )
            self._status = "failed"
            return
        await self._release_lease()
        prior = job_input.continuation
        workflow.continue_as_new(job_input.model_copy(update={
            "continuation": AxisContinuationState(
                control_state=self._deps.control_state,
                browser_projection=self._deps.browser_projection,
                total_run_segments=self._total_run_segments,
                version_manifest=self._version_manifest,
                original_task_summary=(prior.original_task_summary if prior else job_input.task_summary),
                user_answers=list(self._answer_history[-5:]),
            )
        }))

    async def _run_segment(self, agent: Any, prompt: Optional[str], history: Any, deferred: Any) -> Any:
        if not await self._load_and_check_worker_runtime():
            return "incompatible"
        self._run_segments += 1
        self._total_run_segments += 1
        try:
            result = await agent.run(prompt, deps=self._deps, message_history=history, deferred_tool_results=deferred)
            return result.output, result.all_messages()
        except ActivityError:
            assert self._deps is not None
            effect_id = mark_active_effect_unknown(self._deps)
            if effect_id is None:
                raise
            await self._emit("effect_outcome_unknown")
            if self._settings and self._settings.reconcile_unknown_effects:
                if self._run_segments >= self._settings.max_run_segments:
                    self._failure(
                        "HISTORY_LIMIT_REACHED",
                        "The durable history limit was reached with an unresolved effect.",
                    )
                    return None
                self._run_segments += 1
                self._total_run_segments += 1
                await self._reconcile(effect_id)
            return None

    async def _reconcile(self, effect_id: str) -> None:
        assert self._deps is not None
        ledger = EffectLedger(self._deps.control_state.effects)
        effect = ledger.get(effect_id)
        ledger._set(effect_id, status="reconciling")
        self._deps.reconciling_effect_id = effect_id
        self._deps.control_state.observed_current_run = False
        self._status = "reconciling"
        await self._emit("reconciliation_started")
        prompt = (
            "Reconcile the last unknown mutation. Observe the intended tab freshly and check every required "
            "postcondition with browser_assert. Never repeat the mutation. If the evidence is incomplete, "
            "return needs_user_input."
        )
        try:
            await get_durable_agent().run(prompt, deps=self._deps)
        except ActivityError:
            pass
        finally:
            self._deps.reconciling_effect_id = None
        updated = ledger.get(effect_id)
        intent = self._deps.control_state.task_intent
        required = {c.criterion_id for c in (intent.acceptance_criteria if intent else [])
                    if c.required and c.criterion_id in effect.acceptance_criterion_ids}
        outcome = classify_reconciliation(required, set(updated.passed_criterion_ids), set(updated.failed_criterion_ids))
        status = {"applied": "reconciled_succeeded", "not_applied": "reconciled_not_applied",
                  "inconclusive": "manual_intervention_required"}[outcome]
        ledger._set(effect_id, status=status)
        self._last_reconciled_effect_id = effect_id
        self._status = "running"
        await self._emit(f"reconciliation_{outcome}")

    def _safe_progress_summary(self) -> str:
        assert self._deps is not None
        control = self._deps.control_state
        lines: List[str] = []
        if control.task_intent is not None:
            lines.append(f"Objective: {control.task_intent.goal}")
        if control.plan:
            lines.append("Plan: " + "; ".join(f"{p.task_id}={p.status}" for p in control.plan))
        if control.effects:
            lines.append("Effects: " + "; ".join(f"{e.effect_key}={e.status}" for e in control.effects))
        return "\n".join(lines) if lines else "No task intent, plan, or effects recorded yet."

    def _build_resumed_prompt(self, job_input: AxisJobInput) -> str:
        """The first model request after `continue_as_new` never relies on
        message history (which continue_as_new intentionally drops) or on a
        bare "Continue the task..." — it restores a bounded, explicit
        continuation context instead: the original user task, the current
        objective, a safe summary of plan/effect progress, and any user
        answers already given."""
        continuation = job_input.continuation
        original = continuation.original_task_summary if continuation else job_input.task_summary
        parts = [f"Original task: {original}", self._safe_progress_summary()]
        answers = continuation.user_answers if continuation else []
        if answers:
            parts.append("Prior user answers, most recent last: " + " | ".join(answers))
        parts.append(
            "This run segment continues a durable job after a history checkpoint (continue_as_new). "
            "Do not repeat any browser mutation already recorded above as succeeded/executed_unverified/"
            "reconciled — verify current state first, then continue toward the objective using the "
            "recorded plan and effects."
        )
        return "\n".join(parts)

    def _continuation_prompt(self) -> str:
        if self._deps is None or self._last_reconciled_effect_id is None:
            return "Continue the task from durable state."
        effect = EffectLedger(self._deps.control_state.effects).get(self._last_reconciled_effect_id)
        if effect.status == "manual_intervention_required":
            return "The prior mutation remains inconclusive and must not be repeated. Ask the user how to proceed."
        return "The prior mutation was reconciled. Continue from the recorded outcome without repeating it."

    async def _wait_for_approval(self, output: DeferredToolRequests) -> Optional[DeferredToolResults]:
        assert self._deps is not None
        approvals = output.approvals
        if len(approvals) != 1:
            denied = {call.tool_call_id: ToolDenied("Denied: only one effectful deferred call is allowed.") for call in approvals}
            return DeferredToolResults(approvals=denied)
        call_id = approvals[0].tool_call_id
        request = self._deps.control_state.approval_state.requests_by_tool_call_id.get(call_id)
        if request is None:
            return DeferredToolResults(approvals={call_id: ToolDenied("Denied: approval state is unavailable.")})
        self._status = "waiting_for_approval"
        await self._emit("job_waiting_for_approval")
        # A human approval decision can take arbitrarily long — release the
        # lease for the whole wait so no other job is blocked on this one
        # sitting idle, and reacquire it below only once a decision (or
        # cancellation/rebind) actually arrives.
        prior_binding_generation = self._deps.browser_projection.binding_generation
        await self._release_lease()
        while True:
            current = self._deps.control_state.approval_state.requests_by_tool_call_id.get(call_id)
            if current is None or current.decided is not None:
                break
            await workflow.wait_condition(
                lambda: self._cancel_requested or self._rebind_requested or self._paused or
                bool(self._deps.control_state.approval_state.requests_by_tool_call_id.get(call_id) and
                     self._deps.control_state.approval_state.requests_by_tool_call_id[call_id].decided is not None)
            )
            if self._cancel_requested:
                return None
            if self._paused:
                if not await self._handle_pause():
                    return None
                # Still only awaiting a decision, not resuming execution —
                # release again immediately rather than holding the lease
                # idle for the remainder of the approval wait.
                await self._release_lease()
                self._status = "waiting_for_approval"
                continue
            if self._rebind_requested:
                break
        if self._cancel_requested:
            return None
        if self._rebind_requested:
            await self._handle_rebind()
            current = self._deps.control_state.approval_state.requests_by_tool_call_id.get(call_id)
            if current and not current.resolved:
                self._deps.control_state.approval_state.requests_by_tool_call_id[call_id] = current.model_copy(
                    update={"resolved": True, "decided": False}
                )
            return DeferredToolResults(approvals={call_id: ToolDenied("Denied: browser context changed.")})
        resolved = resolve_recorded_approval(self._deps.control_state.approval_state, call_id)
        if resolved is None:
            return DeferredToolResults(approvals={call_id: ToolDenied("Denied: stale approval decision.")})
        ledger = EffectLedger(self._deps.control_state.effects)
        if self._deps.browser_projection.binding_generation != prior_binding_generation:
            # Defensive revalidation: the binding changed while the lease
            # was released for this wait even though no explicit rebind was
            # observed above — never resume the old deferred call against a
            # context it was never prepared/approved against.
            ledger.mark_denied(resolved.effect_id)
            return DeferredToolResults(approvals={call_id: ToolDenied("Denied: browser context changed.")})
        # Reacquire before resuming execution either way — a denial still
        # continues the run afterward, and the browser activity
        # independently re-verifies the same fenced proof again before any
        # RPC regardless.
        lease_reacquired = await self._acquire_lease()
        if not lease_reacquired:
            self._last_error_code = "BROWSER_SCOPE_BUSY"
        if resolved.decided:
            if not lease_reacquired:
                current = ledger.get(resolved.effect_id)
                ledger.mark_outcome(resolved.effect_id, "known_not_applied", current.execution_stage)
                decision = ToolDenied("Denied: the browser lease is unavailable.")
            else:
                ledger.mark_approved(resolved.effect_id)
                decision = ToolApproved()
        else:
            ledger.mark_denied(resolved.effect_id)
            decision = ToolDenied("Denied by the user.")
        self._status = "running"
        return DeferredToolResults(approvals={call_id: decision})

    async def _wait_for_user(self, output: AxisTaskResult) -> Optional[str]:
        assert output.question
        request_id = hashlib.sha256(f"{self._deps.job_id}:{self._total_run_segments}:question".encode()).hexdigest()[:16]
        self._pending_question = DurableQuestion(request_id=request_id, question=output.question.strip())
        self._status = "waiting_for_user"
        await self._emit("job_waiting_for_user")
        await self._release_lease()
        while self._pending_question and (
            not self._pending_question.answered or self._deps.browser_projection.rebind_required
        ):
            await workflow.wait_condition(
                lambda: self._cancel_requested or self._rebind_requested or self._paused or
                bool(self._pending_question and self._pending_question.answered and
                     not self._deps.browser_projection.rebind_required)
            )
            if self._cancel_requested:
                return None
            if self._paused:
                if not await self._handle_pause():
                    return None
                # _handle_pause reacquires for safe continuation, but this
                # suspension still does not need ownership until answered.
                await self._release_lease()
                self._status = (
                    "waiting_for_browser" if self._deps.browser_projection.rebind_required
                    else "waiting_for_user"
                )
            if self._rebind_requested:
                await self._handle_rebind()
                # Waiting for the user's answer does not need continued
                # ownership, whether registry refresh succeeded or remained
                # ambiguous.
                await self._release_lease()
        answer = self._user_input
        self._user_input = None
        self._pending_question = None
        if answer:
            self._answer_history.append(answer[:300])
            del self._answer_history[:-5]
        if not await self._acquire_lease():
            self._last_error_code = "BROWSER_SCOPE_BUSY"
            return None
        self._status = "running"
        return f"User answer: {answer}"

    def _finalize_cancelled(self) -> AxisTaskResult:
        self._status = "cancelled"
        self._result = AxisTaskResult(status="cancelled", summary=self._cancel_reason or "The job was cancelled.")
        return self._result

    def _failure(self, code: str, summary: str, *, retryable: bool = False) -> AxisTaskResult:
        self._status = "failed"
        self._last_error_code = code
        self._result = AxisTaskResult(status="failed", summary=summary, error_code=code, retryable=retryable)
        return self._result

    @workflow.run
    async def run(self, job_input: AxisJobInput) -> AxisTaskResult:
        self._task_summary = job_input.task_summary
        continuation = job_input.continuation
        self._version_manifest = continuation.version_manifest if continuation else None
        self._total_run_segments = continuation.total_run_segments if continuation else 0
        self._deps = AxisDurableDeps(
            run_id=f"{job_input.job_id}-run", job_id=job_input.job_id,
            browser_scope_key=job_input.browser_scope_key,
            allow_response_body=job_input.allow_response_body, max_open_tabs=job_input.max_open_tabs,
            firewall=job_input.firewall, initial_tab_handle=job_input.initial_tab_handle,
            control_state=continuation.control_state if continuation else AxisControlState(),
            browser_projection=continuation.browser_projection if continuation else BrowserRecoveryProjection(),
        )
        self._status = "running"
        await self._emit("job_created")
        if not await self._load_and_check_worker_runtime():
            return self._result
        assert self._settings is not None
        if not await self._acquire_lease():
            return self._failure("BROWSER_SCOPE_BUSY", "The browser scope is busy.", retryable=True)
        await self._emit("job_started")
        agent = get_durable_agent()
        prompt: Optional[str] = job_input.task_summary if not continuation else self._build_resumed_prompt(job_input)
        history: Any = None
        try:
            while True:
                if self._result is not None:
                    await self._release_lease()
                    return self._result
                if self._cancel_requested:
                    await self._release_lease()
                    await self._emit("job_cancelled")
                    return self._finalize_cancelled()
                if not await self._handle_pause():
                    await self._release_lease()
                    if self._result is not None:
                        return self._result
                    return self._finalize_cancelled()
                if self._rebind_requested and not await self._handle_rebind():
                    await workflow.wait_condition(lambda: self._rebind_requested or self._cancel_requested)
                    continue
                await self._check_history_limit(job_input)
                if self._result is not None:
                    await self._release_lease()
                    return self._result
                segment = await self._run_segment(agent, prompt, history, None)
                prompt = None
                if segment == "incompatible":
                    await self._release_lease()
                    return self._result
                if segment is None:
                    prompt, history = self._continuation_prompt(), None
                    continue
                output, history = segment
                if isinstance(output, DeferredToolRequests):
                    deferred = await self._wait_for_approval(output)
                    if deferred is None:
                        continue
                    await self._check_history_limit(job_input)
                    if self._result is not None:
                        await self._release_lease()
                        return self._result
                    segment = await self._run_segment(agent, None, history, deferred)
                    if segment is None:
                        prompt, history = self._continuation_prompt(), None
                        continue
                    if segment == "incompatible":
                        await self._release_lease()
                        return self._result
                    output, history = segment
                    if isinstance(output, DeferredToolRequests):
                        continue
                if output.status == "needs_user_input":
                    answer_prompt = await self._wait_for_user(output)
                    if answer_prompt is None:
                        if self._cancel_requested:
                            continue
                        return self._failure("BROWSER_SCOPE_BUSY", "The browser lease could not be reacquired.", retryable=True)
                    prompt = answer_prompt
                    continue
                self._result = output
                self._status = "completed" if output.status == "completed" else "failed" if output.status == "failed" else "cancelled"
                await self._release_lease()
                await self._emit({"completed": "job_completed", "failed": "job_failed", "cancelled": "job_cancelled"}[self._status])
                return output
        except Exception as exc:
            # `workflow.logger` (not a plain module logger) is the
            # replay-safe way to log from inside workflow code — it only
            # actually emits on the attempt that really executed this line,
            # never redundantly on deterministic replay. Exception type AND
            # message here (not type-only): this is the one place a caught
            # exception was previously reported to the user as nothing more
            # than "UNEXPECTED_ERROR", with no way to find out what it
            # actually was.
            workflow.logger.error(
                "AxisJobWorkflow failed with an unexpected exception: %s: %s", type(exc).__name__, exc,
            )
            await self._release_lease()
            self._status = "failed"
            self._last_error_code = "UNEXPECTED_ERROR"
            await self._emit("job_failed", detail="UNEXPECTED_ERROR")
            return self._failure("UNEXPECTED_ERROR", "The durable job failed safely.")
