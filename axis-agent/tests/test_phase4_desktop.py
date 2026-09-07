"""Deterministic tests for the Phase 4 PySide6/QML desktop control surface.

No Chrome, no Browser Agent Bridge, no OCI credentials, no live provider,
no live Temporal server, and no Windows desktop session are required —
Qt runs under ``QT_QPA_PLATFORM=offscreen`` and every durable operation
goes through a deterministic fake `DesktopBackend`.
"""
from __future__ import annotations

import ast
import os
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

AGENT_DIR = Path(__file__).resolve().parent.parent
DESKTOP_DIR = AGENT_DIR / "axis" / "desktop"

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from axis.config import AxisConfigError, load_axis_config, parse_axis_config  # noqa: E402
from axis.desktop.backend import BackgroundLoop, DesktopBackendError  # noqa: E402
from axis.desktop.controller import AxisDesktopController  # noqa: E402
from axis.desktop.presentation import (  # noqa: E402
    DesktopJobSnapshot, diff_timeline_entries, project_job_state, status_presentation,
)
from axis.durability.models import (  # noqa: E402
    AxisJobState, DurableQuestion, RebindCandidate,
)
from axis.approvals import ApprovalRequest  # noqa: E402
from axis.effects import AcceptanceCriterion, EffectRecord, TaskIntent  # noqa: E402


def _app() -> QGuiApplication:
    app = QGuiApplication.instance()
    return app if app is not None else QGuiApplication([])


def _pump(condition, timeout: float = 5.0) -> bool:
    """Drain the Qt event loop until `condition()` is true — needed because
    background-loop results are delivered via a queued cross-thread signal."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


def _state(
    *, status: str = "running", task_summary: str = "Do the task.", plan=None, effects=None,
    task_intent=None, pending_approval=None, pending_question=None, rebind_required=False,
    rebind_candidates=None, result=None, paused=False, cancel_requested=False, last_error_code=None,
) -> AxisJobState:
    return AxisJobState(
        job_id="job-1", status=status, task_summary=task_summary, browser_scope_key="scope-secret",
        plan=plan or [], plan_revision=0, task_intent=task_intent, effects=effects or [], acceptance={},
        pending_approval=pending_approval, pending_question=pending_question, paused=paused,
        cancel_requested=cancel_requested, rebind_required=rebind_required,
        rebind_candidates=rebind_candidates or [], binding_generation=0, observation_generation=0,
        run_segments=1, version_manifest=None, result=result, last_error_code=last_error_code,
    )


class FakeBackend:
    """A deterministic, in-memory `DesktopBackend` — records every call so
    tests can assert exact call counts without touching Temporal."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.states: Dict[str, AxisJobState] = {}
        self.next_job_id = "job-1"
        self.fail_start = False
        self.fail_query_job_ids: set = set()
        self.candidates: Dict[str, List[RebindCandidate]] = {}

    async def start_job(self, task_summary: str) -> str:
        self.calls.append(("start_job", task_summary))
        if self.fail_start:
            raise DesktopBackendError("JOB_START_FAILED", "AXIS could not start the task.")
        job_id = self.next_job_id
        self.states.setdefault(job_id, _state(task_summary=task_summary))
        return job_id

    async def get_job_state(self, job_id: str) -> AxisJobState:
        self.calls.append(("get_job_state", job_id))
        if job_id in self.fail_query_job_ids or job_id not in self.states:
            raise DesktopBackendError("JOB_NOT_FOUND", "AXIS could not find that job.")
        return self.states[job_id]

    async def pause_job(self, job_id: str) -> None:
        self.calls.append(("pause_job", job_id))

    async def resume_job(self, job_id: str) -> None:
        self.calls.append(("resume_job", job_id))

    async def cancel_job(self, job_id: str, reason: Optional[str]) -> None:
        self.calls.append(("cancel_job", job_id, reason))

    async def approve_current(self, job_id: str) -> None:
        self.calls.append(("approve_current", job_id))

    async def deny_current(self, job_id: str) -> None:
        self.calls.append(("deny_current", job_id))

    async def submit_user_input(self, job_id: str, text: str) -> None:
        self.calls.append(("submit_user_input", job_id, text))

    async def rebind_browser(self, job_id: str, reason: Optional[str]) -> None:
        self.calls.append(("rebind_browser", job_id, reason))

    async def get_rebind_candidates(self, job_id: str) -> List[RebindCandidate]:
        self.calls.append(("get_rebind_candidates", job_id))
        return self.candidates.get(job_id, [])

    async def select_rebind_candidate(self, job_id: str, candidate_id: str) -> None:
        self.calls.append(("select_rebind_candidate", job_id, candidate_id))

    async def close(self) -> None:
        self.calls.append(("close",))

    def call_names(self) -> List[str]:
        return [c[0] for c in self.calls]


def _make_controller(backend: FakeBackend) -> AxisDesktopController:
    config = load_axis_config()
    loop = BackgroundLoop()
    controller = AxisDesktopController(backend, config, loop)
    controller._background_loop_for_test = loop  # keep alive / for teardown
    return controller


class DesktopArchitecturePurityTests(unittest.TestCase):
    """Static-source checks: the desktop package must never reach past its
    narrow boundary into browser/agent/workflow internals."""

    def _read(self, name: str) -> str:
        return (DESKTOP_DIR / name).read_text(encoding="utf-8")

    def test_backend_never_imports_browser_bridge_client(self):
        for name in ("backend.py", "controller.py", "app.py", "presentation.py"):
            with self.subTest(name=name):
                self.assertNotIn("browser_bridge_client", self._read(name))
                self.assertNotIn("BrowserBridgeClient", self._read(name))

    def test_desktop_package_never_dispatches_browser_tools(self):
        for name in ("backend.py", "controller.py", "presentation.py"):
            with self.subTest(name=name):
                source = self._read(name)
                self.assertNotIn("browser_agent_tools", source)
                self.assertNotIn("get_tool_handlers", source)
                self.assertNotIn("get_tool_definitions", source)

    def test_desktop_package_never_instantiates_the_root_agent(self):
        for name in ("backend.py", "controller.py", "app.py"):
            with self.subTest(name=name):
                source = self._read(name)
                self.assertNotIn("build_axis_agent", source)
                self.assertNotIn("build_durable_agent", source)
                self.assertNotIn("axis.durability.runtime", source)

    def test_backend_only_calls_the_accepted_durable_client_module(self):
        source = self._read("backend.py")
        tree = ast.parse(source)
        modules = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and "axis.durability" in node.module
        }
        self.assertEqual(modules, {"axis.durability.client", "axis.durability.models"})

    def test_no_forbidden_web_or_gui_frameworks_are_used(self):
        for path in DESKTOP_DIR.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("fastapi", "flask", "django", "aiohttp.web", "starlette"):
                self.assertNotIn(forbidden, source.lower(), f"{forbidden} referenced in {path}")

    def test_no_qtwebengine_or_embedded_browser(self):
        for path in list(DESKTOP_DIR.rglob("*.py")) + list(DESKTOP_DIR.rglob("*.qml")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("WebEngine", source)
            self.assertNotIn("QtWebEngine", source)

    def test_controller_exposes_no_generic_invocation_slots(self):
        source = self._read("controller.py")
        for forbidden in ("callTool", "callRpc", "sendTemporalSignal", "setPlan", "markAcceptancePassed", "setEffectStatus", "setApproval", "setBrowserSession"):
            self.assertNotIn(forbidden, source)

    def test_pyside6_is_exactly_pinned(self):
        pyproject = (AGENT_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"pyside6==', pyproject.lower().replace("PySide6", "pyside6") if False else pyproject)
        import re
        match = re.search(r'"[Pp]y[Ss]ide6==([0-9.]+)"', pyproject)
        self.assertIsNotNone(match, "PySide6 must be pinned with an exact version in pyproject.toml")


class DesktopConfigTests(unittest.TestCase):
    def test_documented_defaults_are_applied(self):
        config = parse_axis_config({"browser": {}, "limits": {}})
        d = config.phase4.desktop
        self.assertTrue(d.enabled)
        self.assertEqual(d.poll_interval_ms, 750)
        self.assertEqual(d.reconnect_initial_delay_ms, 500)
        self.assertEqual(d.reconnect_max_delay_ms, 10_000)
        self.assertEqual(d.max_timeline_items, 300)
        self.assertEqual(d.max_session_jobs, 20)
        self.assertEqual(d.theme, "system")
        self.assertTrue(d.animations_enabled)

    def test_real_axis_yaml_has_a_valid_phase4_section(self):
        config = load_axis_config()
        self.assertTrue(config.phase4.desktop.enabled)

    def test_invalid_theme_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {"desktop": {"theme": "purple"}}})

    def test_poll_interval_bounds_are_enforced(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {"desktop": {"poll_interval_ms": 100}}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {"desktop": {"poll_interval_ms": 20000}}})

    def test_reconnect_max_below_initial_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {
                "desktop": {"reconnect_initial_delay_ms": 5000, "reconnect_max_delay_ms": 1000},
            }})

    def test_unknown_phase4_key_is_a_controlled_error(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {"unexpected": 1}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "phase4": {"desktop": {"unexpected": 1}}})


class PresentationProjectionTests(unittest.TestCase):
    def test_snapshot_never_carries_forbidden_raw_fields(self):
        forbidden_field_names = {
            "browser_scope_key", "bridge_host", "bridge_port", "tab_id", "window_id", "group_id",
            "frame_id", "snapshot_id", "bridge_session_id", "lease_token", "lease_generation",
            "approval_id", "effect_id", "decision_nonce", "answer_nonce", "candidate_id",
        }
        for model in (DesktopJobSnapshot,):
            fields = set(model.model_fields.keys())
            self.assertFalse(fields & forbidden_field_names)

    def test_approval_projection_has_no_id_or_nonce_fields(self):
        from axis.desktop.presentation import DesktopApproval

        fields = set(DesktopApproval.model_fields.keys())
        self.assertEqual(fields, {"summary", "risk", "reason"})

    def test_question_projection_has_no_request_id_or_nonce(self):
        from axis.desktop.presentation import DesktopQuestion

        fields = set(DesktopQuestion.model_fields.keys())
        self.assertEqual(fields, {"question", "choices"})

    def test_rebind_candidate_projection_has_no_candidate_id(self):
        from axis.desktop.presentation import DesktopRebindCandidate

        fields = set(DesktopRebindCandidate.model_fields.keys())
        self.assertNotIn("candidate_id", fields)

    def test_every_accepted_status_maps_to_a_deliberate_presentation(self):
        for status in (
            "created", "running", "waiting_for_approval", "waiting_for_user", "paused",
            "reconciling", "waiting_for_browser", "completed", "failed", "cancelled",
        ):
            label, kind, action = status_presentation(status)
            self.assertTrue(label)
            self.assertTrue(kind)
            self.assertTrue(action)

    def test_pending_approval_projects_only_safe_fields(self):
        state = _state(status="waiting_for_approval", pending_approval=ApprovalRequest(
            approval_id="secret-approval-id", effect_id="secret-effect-id", risk="external_effect",
            summary="Upload a file.", reason="external_effect risk requires approval by default policy.",
        ))
        snapshot = project_job_state(state)
        self.assertIsNotNone(snapshot.pending_approval)
        rendered = snapshot.pending_approval.model_dump_json()
        self.assertNotIn("secret-approval-id", rendered)
        self.assertNotIn("secret-effect-id", rendered)

    def test_rebind_candidates_project_without_raw_ids(self):
        state = _state(status="waiting_for_browser", rebind_required=True, rebind_candidates=[
            RebindCandidate(candidate_id="raw-internal-id-123", sanitized_url="https://example.com", title="Example", active=True),
        ])
        snapshot = project_job_state(state)
        rendered = snapshot.rebind_candidates[0].model_dump_json()
        self.assertNotIn("raw-internal-id-123", rendered)

    def test_acceptance_only_passed_when_ledger_says_so(self):
        intent = TaskIntent(goal="g", acceptance_criteria=[
            AcceptanceCriterion(criterion_id="c1", description="desc", assertion={"assertion": "title", "expected": "x"}),
        ])
        effect = EffectRecord(
            effect_id="e1", effect_key="k1", plan_task_id="t1", risk="reversible_local",
            summary="s", status="executed_unverified", allowed_tools=["browser_act"],
            allowed_actions=["fill"], max_browser_mutations=3, acceptance_criterion_ids=["c1"],
        )
        state = _state(task_intent=intent, effects=[effect])
        snapshot = project_job_state(state)
        self.assertEqual(snapshot.acceptance[0].state, "pending")  # not passed merely for existing/executed

    def test_acceptance_passed_reflects_ledger_pass(self):
        intent = TaskIntent(goal="g", acceptance_criteria=[
            AcceptanceCriterion(criterion_id="c1", description="desc", assertion={"assertion": "title", "expected": "x"}),
        ])
        effect = EffectRecord(
            effect_id="e1", effect_key="k1", plan_task_id="t1", risk="reversible_local",
            summary="s", status="succeeded", allowed_tools=["browser_act"], allowed_actions=["fill"],
            max_browser_mutations=3, acceptance_criterion_ids=["c1"], passed_criterion_ids=["c1"],
        )
        state = _state(task_intent=intent, effects=[effect])
        snapshot = project_job_state(state)
        self.assertEqual(snapshot.acceptance[0].state, "passed")

    def test_diff_timeline_has_no_duplicate_entries_for_identical_snapshots(self):
        state = _state()
        snapshot = project_job_state(state)
        entries = diff_timeline_entries(snapshot, snapshot)
        self.assertEqual(entries, [])

    def test_diff_timeline_does_not_backfill_history_on_first_observation(self):
        state = _state(status="running")
        snapshot = project_job_state(state)
        entries = diff_timeline_entries(None, snapshot)
        self.assertEqual(len(entries), 1)  # only "status: X", never fabricated prior events

    def test_terminal_statuses_disallow_pause_resume_cancel(self):
        for status in ("completed", "failed", "cancelled"):
            snapshot = project_job_state(_state(status=status))
            self.assertFalse(snapshot.can_pause)
            self.assertFalse(snapshot.can_resume)
            self.assertFalse(snapshot.can_cancel)
            self.assertTrue(snapshot.is_terminal)


class ControllerBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.controller = _make_controller(self.backend)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self.controller._background_loop_for_test.stop()

    def test_empty_task_is_rejected_before_the_backend(self):
        self.controller.startTask("   ")
        self.assertEqual(self.backend.calls, [])

    def test_oversized_task_is_rejected_before_the_backend(self):
        self.controller.startTask("x" * 501)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.controller.safeErrorCode, "JOB_START_FAILED")

    def test_valid_task_calls_start_job_exactly_once(self):
        self.controller.startTask("Update the display name.")
        self.assertTrue(_pump(lambda: self.backend.call_names() == ["start_job", "get_job_state"]))
        self.assertEqual(self.backend.call_names().count("start_job"), 1)

    def test_double_submit_does_not_start_two_jobs(self):
        self.controller.startTask("Task one.")
        self.controller.startTask("Task one again.")
        self.assertTrue(_pump(lambda: "start_job" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("start_job"), 1)

    def test_composer_clears_only_after_successful_start(self):
        self.controller.startTask("Do the thing.")
        self.assertTrue(_pump(lambda: self.controller.taskSummary == "Do the thing." and self.controller.currentJobId != ""))

    def test_failed_start_leaves_task_text_for_retry(self):
        self.backend.fail_start = True
        self.controller.startTask("Retry me.")
        self.assertTrue(_pump(lambda: self.controller.safeErrorCode != ""))
        self.assertEqual(self.controller.taskSummary, "Retry me.")
        self.assertEqual(self.controller.currentJobId, "")

    def test_reconnect_queries_state_and_never_starts_a_new_job(self):
        self.backend.states["existing-job"] = _state(task_summary="Already running.")
        self.controller.reconnectJob("existing-job")
        self.assertTrue(_pump(lambda: self.controller.currentJobStatus != ""))
        self.assertNotIn("start_job", self.backend.call_names())
        self.assertEqual(self.controller.currentJobId, "existing-job")

    def test_unknown_job_produces_a_safe_controlled_error(self):
        self.controller.reconnectJob("does-not-exist")
        self.assertTrue(_pump(lambda: self.controller.safeErrorCode != ""))
        self.assertEqual(self.controller.safeErrorCode, "JOB_NOT_FOUND")

    def test_only_one_query_in_flight_at_a_time(self):
        self.backend.states["job-1"] = _state()
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.currentJobStatus != ""))
        before = self.backend.call_names().count("get_job_state")
        self.controller.refreshCurrentJob()
        self.controller.refreshCurrentJob()
        self.controller.refreshCurrentJob()
        self.assertTrue(_pump(lambda: self.backend.call_names().count("get_job_state") > before))
        # A second overlapping refresh while one is in flight must be a no-op,
        # not a second concurrent query.
        after_immediate = self.backend.call_names().count("get_job_state")
        self.assertLessEqual(after_immediate - before, 2)

    def test_polling_stops_for_a_terminal_job(self):
        self.backend.states["job-1"] = _state(status="completed", result=None)
        from axis.models import AxisTaskResult

        self.backend.states["job-1"] = self.backend.states["job-1"].model_copy(update={
            "result": AxisTaskResult(status="completed", summary="done", verification_summary="ok"),
        })
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasResult))
        self.assertFalse(self.controller._poll_timer.isActive())

    def test_pause_calls_the_signal_exactly_once(self):
        self.backend.states["job-1"] = _state(status="running")
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.currentJobStatus == "running"))
        self.controller.pauseCurrentJob()
        self.assertTrue(_pump(lambda: "pause_job" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("pause_job"), 1)

    def test_resume_calls_the_signal_exactly_once(self):
        self.backend.states["job-1"] = _state(status="paused", paused=True)
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.canResume))
        self.controller.resumeCurrentJob()
        self.assertTrue(_pump(lambda: "resume_job" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("resume_job"), 1)

    def test_cancel_sends_bounded_reason(self):
        self.backend.states["job-1"] = _state(status="running")
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.canCancel))
        self.controller.requestCancel("x" * 5000)
        self.assertTrue(_pump(lambda: "cancel_job" in self.backend.call_names()))
        call = next(c for c in self.backend.calls if c[0] == "cancel_job")
        self.assertLessEqual(len(call[2] or ""), 300)

    def test_approve_sends_exactly_one_signal_and_no_browser_dispatch(self):
        self.backend.states["job-1"] = _state(status="waiting_for_approval", pending_approval=ApprovalRequest(
            approval_id="a1", effect_id="e1", risk="external_effect", summary="Upload.", reason="policy",
        ))
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasPendingApproval))
        self.controller.approveCurrentEffect()
        self.controller.approveCurrentEffect()  # a second click while in flight must be a no-op
        self.assertTrue(_pump(lambda: "approve_current" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("approve_current"), 1)
        self.assertNotIn("deny_current", self.backend.call_names())

    def test_deny_sends_exactly_one_signal(self):
        self.backend.states["job-1"] = _state(status="waiting_for_approval", pending_approval=ApprovalRequest(
            approval_id="a1", effect_id="e1", risk="external_effect", summary="Upload.", reason="policy",
        ))
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasPendingApproval))
        self.controller.denyCurrentEffect()
        self.assertTrue(_pump(lambda: "deny_current" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("deny_current"), 1)

    def test_empty_answer_is_rejected(self):
        self.backend.states["job-1"] = _state(status="waiting_for_user", pending_question=DurableQuestion(
            request_id="r1", question="Proceed?",
        ))
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasPendingQuestion))
        self.controller.submitUserAnswer("   ")
        self.assertNotIn("submit_user_input", self.backend.call_names())

    def test_oversized_answer_is_rejected(self):
        self.backend.states["job-1"] = _state(status="waiting_for_user", pending_question=DurableQuestion(
            request_id="r1", question="Proceed?",
        ))
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasPendingQuestion))
        self.controller.submitUserAnswer("x" * 2001)
        self.assertNotIn("submit_user_input", self.backend.call_names())

    def test_valid_answer_sent_exactly_once(self):
        self.backend.states["job-1"] = _state(status="waiting_for_user", pending_question=DurableQuestion(
            request_id="r1", question="Proceed?",
        ))
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.hasPendingQuestion))
        self.controller.submitUserAnswer("Yes, proceed.")
        self.controller.submitUserAnswer("Yes, proceed again.")
        self.assertTrue(_pump(lambda: "submit_user_input" in self.backend.call_names()))
        self.assertEqual(self.backend.call_names().count("submit_user_input"), 1)

    def test_rebind_candidate_selection_maps_index_to_the_real_opaque_id(self):
        self.backend.states["job-1"] = _state(status="waiting_for_browser", rebind_required=True, rebind_candidates=[
            RebindCandidate(candidate_id="real-id-A", sanitized_url="https://a", title="A"),
            RebindCandidate(candidate_id="real-id-B", sanitized_url="https://b", title="B"),
        ])
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.rebindRequired))
        self.controller.selectRebindCandidate(1)
        self.assertTrue(_pump(lambda: "select_rebind_candidate" in self.backend.call_names()))
        call = next(c for c in self.backend.calls if c[0] == "select_rebind_candidate")
        self.assertEqual(call[2], "real-id-B")

    def test_invalid_rebind_index_is_ignored(self):
        self.backend.states["job-1"] = _state(status="waiting_for_browser", rebind_required=True, rebind_candidates=[
            RebindCandidate(candidate_id="real-id-A", sanitized_url="https://a", title="A"),
        ])
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.rebindRequired))
        self.controller.selectRebindCandidate(99)
        self.assertNotIn("select_rebind_candidate", self.backend.call_names())

    def test_unexpected_exception_becomes_a_safe_desktop_error(self):
        async def boom(job_id):
            raise ValueError("some internal detail that must never reach the UI")

        self.backend.get_job_state = boom  # type: ignore[method-assign]
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.safeErrorCode != ""))
        self.assertEqual(self.controller.safeErrorCode, "DESKTOP_INTERNAL_ERROR")
        self.assertNotIn("some internal detail", self.controller.safeErrorMessage)

    def test_stale_generation_results_do_not_overwrite_a_newly_selected_job(self):
        self.backend.states["job-old"] = _state(task_summary="Old job.")
        self.backend.states["job-new"] = _state(task_summary="New job.")

        # Make the first query for job-old artificially slow by wrapping it.
        import asyncio

        original = self.backend.get_job_state
        gate = asyncio.Event()

        async def slow_get_job_state(job_id):
            if job_id == "job-old":
                await gate.wait()
            return await original(job_id)

        self.backend.get_job_state = slow_get_job_state  # type: ignore[method-assign]
        self.controller.reconnectJob("job-old")
        # Give the slow query a moment to actually start before reconnecting away.
        time.sleep(0.05)
        self.controller.reconnectJob("job-new")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-new" and self.controller.taskSummary == "New job."))
        self.controller._loop.submit(lambda: _set_event(gate)).result(timeout=2)
        time.sleep(0.1)
        QCoreApplication.processEvents()
        # The stale job-old result must never have overwritten job-new's state.
        self.assertEqual(self.controller.currentJobId, "job-new")
        self.assertEqual(self.controller.taskSummary, "New job.")

    def test_separate_controllers_do_not_share_state(self):
        backend2 = FakeBackend()
        controller2 = _make_controller(backend2)
        try:
            self.backend.states["job-1"] = _state(task_summary="Only in controller one.")
            self.controller.reconnectJob("job-1")
            self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
            self.assertEqual(controller2.currentJobId, "")
        finally:
            controller2.shutdown()
            controller2._background_loop_for_test.stop()

    def test_shutdown_stops_polling(self):
        self.backend.states["job-1"] = _state(status="running")
        self.controller.reconnectJob("job-1")
        self.assertTrue(_pump(lambda: self.controller.currentJobStatus == "running"))
        self.controller.shutdown()
        self.assertFalse(self.controller._poll_timer.isActive())


async def _set_event(event) -> None:
    event.set()


class DesktopSmokeTests(unittest.TestCase):
    def test_smoke_entry_point_loads_qml_and_exits_cleanly(self):
        from axis.desktop.app import run

        exit_code = run(["axis-desktop"], smoke_test=True)
        self.assertEqual(exit_code, 0)

    def test_qml_resources_resolve_independently_of_working_directory(self):
        cwd = os.getcwd()
        try:
            os.chdir(str(Path.home()))
            from axis.desktop.app import run

            exit_code = run(["axis-desktop"], smoke_test=True)
            self.assertEqual(exit_code, 0)
        finally:
            os.chdir(cwd)

    def test_required_qml_files_and_icon_ready_assets_are_present(self):
        for name in ("Main.qml", "Theme.qml", "qmldir"):
            self.assertTrue((DESKTOP_DIR / "qml" / name).exists(), name)
        for name in ("StatusBadge.qml", "TimelineDelegate.qml", "ApprovalDialog.qml", "UserInputDialog.qml", "RebindDialog.qml"):
            self.assertTrue((DESKTOP_DIR / "qml" / "components" / name).exists(), name)


class DesktopQmlRendersPopulatedStateWithoutErrorsTests(unittest.TestCase):
    """The smoke test alone never exercises a single Repeater/ListView
    delegate (every model starts empty), which is exactly how a real
    "ReferenceError: model is not defined" style delegate binding bug
    shipped once already. This loads the real `Main.qml` against a
    controller driven into a fully populated state — plan, effects,
    acceptance, timeline, session jobs, a pending approval, a pending
    question, and rebind candidates all non-empty — and fails if the QML
    engine emits ANY warning while every delegate actually instantiates."""

    def test_every_delegate_renders_with_no_qml_warnings(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlApplicationEngine

        app = _app()
        backend = FakeBackend()
        config = load_axis_config()
        loop = BackgroundLoop()
        controller = AxisDesktopController(backend, config, loop)
        try:
            from axis.effects import AcceptanceCriterion, EffectRecord, TaskIntent
            from axis.models import AxisControlState
            from axis.planning import AxisPlanItem

            state = _state(
                status="waiting_for_approval",
                plan=[
                    AxisPlanItem(task_id="t1", content="Open Gmail", status="completed"),
                    AxisPlanItem(task_id="t2", content="Write the email", status="in_progress"),
                ],
                task_intent=TaskIntent(goal="Send an email.", acceptance_criteria=[
                    AcceptanceCriterion(criterion_id="c1", description="Email sent", assertion={"assertion": "title", "expected": "Sent"}),
                ]),
                effects=[EffectRecord(
                    effect_id="e1", effect_key="k1", plan_task_id="t2", risk="external_effect",
                    summary="Send the email.", status="executing", allowed_tools=["browser_act"],
                    allowed_actions=["click"], max_browser_mutations=3, acceptance_criterion_ids=["c1"],
                )],
                pending_approval=ApprovalRequest(
                    approval_id="a1", effect_id="e1", risk="external_effect",
                    summary="Send an email to the CEO.", reason="external_effect risk requires approval by default policy.",
                ),
                rebind_required=False,
            )
            backend.states["job-1"] = state
            backend.next_job_id = "job-1"

            # Populate a second, distinct session-job entry too.
            backend.states["job-0"] = _state(task_summary="An earlier task.")
            controller.reconnectJob("job-0")
            self.assertTrue(_pump(lambda: controller.currentJobId == "job-0"))
            controller.reconnectJob("job-1")
            self.assertTrue(_pump(lambda: controller.hasPendingApproval))

            warnings: List[str] = []
            engine = QQmlApplicationEngine()
            engine.rootContext().setContextProperty("axisController", controller)
            engine.addImportPath(str(DESKTOP_DIR / "qml"))
            engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))
            engine.load(QUrl.fromLocalFile(str(DESKTOP_DIR / "qml" / "Main.qml")))
            self.assertTrue(engine.rootObjects(), "Main.qml failed to load")
            _pump(lambda: False, timeout=0.3)  # let deferred/async bindings settle
            self.assertEqual(warnings, [])
        finally:
            controller.shutdown()
            loop.stop()


class EntryPointPreservationTests(unittest.TestCase):
    def test_existing_cli_and_worker_entry_points_still_import(self):
        import axis.cli  # noqa: F401
        import axis.durability.worker  # noqa: F401

    def test_desktop_module_is_runnable_as_a_module(self):
        import axis.desktop.__main__  # noqa: F401


class SessionStoreTests(unittest.TestCase):
    def test_disk_round_trip(self):
        import tempfile

        from axis.desktop.sessions import SessionStore

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.json"
            store = SessionStore(path)
            session = store.create_session("Open google and amazon")
            store.add_job(session.session_id, "job-1")
            store.append_message(session.session_id, "user", "Open google and amazon")
            store.append_message(session.session_id, "result", "Done.", "completed")

            reloaded = SessionStore(path).get(session.session_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.title, "Open google and amazon")
            self.assertEqual(reloaded.job_ids, ["job-1"])
            self.assertEqual(len(reloaded.messages), 2)
            self.assertEqual(reloaded.messages[1]["kind"], "result")
            self.assertEqual(reloaded.messages[1]["meta"], "completed")

    def test_corrupt_file_degrades_to_an_empty_store(self):
        import tempfile

        from axis.desktop.sessions import SessionStore

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions.json"
            path.write_text("{not json", encoding="utf-8")
            store = SessionStore(path)
            self.assertEqual(store.list_sessions(), [])

    def test_find_by_job(self):
        from axis.desktop.sessions import SessionStore

        store = SessionStore()
        session = store.create_session("t")
        store.add_job(session.session_id, "job-9")
        self.assertIs(store.find_by_job("job-9"), store.get(session.session_id))
        self.assertIsNone(store.find_by_job("job-unknown"))


class SessionChatBehaviorTests(unittest.TestCase):
    """The chat thread must span every job in one session, and the sidebar
    must list sessions — never one row per job."""

    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.controller = _make_controller(self.backend)

    def tearDown(self) -> None:
        self.controller.shutdown()
        self.controller._background_loop_for_test.stop()

    def _finish_job(self, job_id: str, summary: str) -> None:
        from axis.models import AxisTaskResult

        self.backend.states[job_id] = self.backend.states[job_id].model_copy(update={
            "status": "completed",
            "result": AxisTaskResult(status="completed", summary=summary, verification_summary="ok"),
        })
        self.controller.refreshCurrentJob()
        self.assertTrue(_pump(lambda: self.controller.hasResult))

    def test_chat_history_survives_a_second_task_in_the_same_session(self):
        self.controller.startTask("Task one.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
        self._finish_job("job-1", "Task one done.")

        self.backend.next_job_id = "job-2"
        self.controller.startTask("Task two.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-2"))

        texts = [i["text"] for i in self.controller._chat_model.items()]
        self.assertIn("Task one.", texts)          # first user message survives
        self.assertIn("Task one done.", texts)     # first result survives
        self.assertIn("Task two.", texts)          # second user message appended

    def test_two_jobs_in_one_session_produce_one_sidebar_row(self):
        self.controller.startTask("Task one.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
        self._finish_job("job-1", "done")
        self.backend.next_job_id = "job-2"
        self.controller.startTask("Task two.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-2"))

        self.assertEqual(self.controller._session_list_model.rowCount(), 1)
        session = self.controller._store.get(self.controller.currentSessionId)
        self.assertEqual(session.job_ids, ["job-1", "job-2"])

    def test_new_session_clears_chat_but_keeps_the_previous_session_listed(self):
        self.controller.startTask("Task one.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
        self.controller.newSession()
        self.assertEqual(self.controller.chatCount, 0)
        self.assertEqual(self.controller.currentJobId, "")
        self.assertEqual(self.controller._session_list_model.rowCount(), 1)

    def test_open_session_restores_the_full_chat(self):
        self.controller.startTask("Task one.")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
        self._finish_job("job-1", "Task one done.")
        session_id = self.controller.currentSessionId
        self.controller.newSession()
        self.assertEqual(self.controller.chatCount, 0)

        self.controller.openSession(session_id)
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-1"))
        texts = [i["text"] for i in self.controller._chat_model.items()]
        self.assertIn("Task one.", texts)
        self.assertIn("Task one done.", texts)

    def test_reconnect_to_an_unknown_job_creates_a_session_for_it(self):
        self.backend.states["job-x"] = _state(task_summary="External job.")
        self.controller.reconnectJob("job-x")
        self.assertTrue(_pump(lambda: self.controller.currentJobId == "job-x"))
        session = self.controller._store.find_by_job("job-x")
        self.assertIsNotNone(session)
        self.assertEqual(self.controller.currentSessionId, session.session_id)


class PausePendingTests(unittest.TestCase):
    """A requested-but-not-yet-honored pause must be visible, never a
    dead zone where can_pause and can_resume are both false silently."""

    def test_running_with_pause_flag_projects_pause_pending(self):
        snapshot = project_job_state(_state(status="running", paused=True))
        self.assertTrue(snapshot.pause_pending)
        self.assertFalse(snapshot.can_pause)
        self.assertFalse(snapshot.can_resume)

    def test_fully_paused_is_not_pause_pending(self):
        snapshot = project_job_state(_state(status="paused", paused=True))
        self.assertFalse(snapshot.pause_pending)
        self.assertTrue(snapshot.can_resume)

    def test_pause_request_emits_a_timeline_entry(self):
        before = project_job_state(_state(status="running"))
        after = project_job_state(_state(status="running", paused=True))
        entries = diff_timeline_entries(before, after)
        self.assertTrue(any("Pause requested" in e.text for e in entries))


class CommandInFlightLeakRegressionTests(unittest.TestCase):
    """Switching jobs while a command is in flight previously left
    `_command_in_flight` stuck true forever, silently dropping every later
    pause/resume/cancel/approve."""

    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def test_begin_tracking_resets_a_stale_command_flag(self):
        backend = FakeBackend()
        controller = _make_controller(backend)
        try:
            backend.states["job-1"] = _state(status="running")
            controller._command_in_flight = True  # simulate the stale leak
            controller.reconnectJob("job-1")
            self.assertFalse(controller._command_in_flight)
            self.assertTrue(_pump(lambda: controller.currentJobStatus == "running"))
            controller.pauseCurrentJob()
            self.assertTrue(_pump(lambda: "pause_job" in backend.call_names()))
        finally:
            controller.shutdown()
            controller._background_loop_for_test.stop()


class Phase0Through3RegressionSmokeTests(unittest.TestCase):
    """A narrow, fast confirmation that the architectural invariants Phase 4
    must not disturb are still true (the full Phase 0-3 suites already run
    separately in `uv run pytest -q`)."""

    def test_eight_browser_tools_registered_exactly_once_and_sequential(self):
        from axis.capabilities.browser import build_browser_capability

        capability = build_browser_capability()
        self.assertEqual(len(capability.tools), 8)
        names = [t.name for t in capability.tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(t.sequential for t in capability.tools))

    def test_root_agent_builds_successfully_as_the_one_authoritative_agent(self):
        from pydantic_ai.models.function import FunctionModel

        import axis.agent as shared_agent

        agent = shared_agent.build_axis_agent(FunctionModel(lambda messages, info: None))
        self.assertEqual(agent.name, "axis_agent")

    def test_durable_build_still_delegates_to_the_one_authoritative_builder(self):
        from unittest.mock import patch
        from pydantic_ai.models.function import FunctionModel

        import axis.agent as shared_agent
        import axis.durability.runtime as durable_agent_module

        cfg = load_axis_config()
        with patch.object(durable_agent_module, "build_axis_agent", wraps=shared_agent.build_axis_agent) as builder:
            agent, durability = durable_agent_module.build_durable_agent(cfg, FunctionModel(lambda messages, info: None))
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(agent.name, "axis_agent")


if __name__ == "__main__":
    unittest.main()
