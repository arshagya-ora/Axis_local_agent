"""Deterministic and Temporal-integration tests for AXIS Phase 3 durable
execution.

Pure unit tests (models, reconciliation classification, lease decision
logic) never touch Temporal. The integration tests drive a real
`AxisJobWorkflow` inside `temporalio.testing.WorkflowEnvironment` (time-
skipping) with a real `Worker` and the real `pydantic_ai.durable_exec.
temporal` machinery — no OCI credentials, no Chrome, no live Browser Agent
Bridge: the bridge client is mocked and pre-seeded into the durable agent's
process-local browser-tools cache, and the model is a deterministic
`FunctionModel`. `conftest.py` forces `ALLOW_MODEL_REQUESTS = False` at
import time.
"""
from __future__ import annotations

import asyncio
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import axis.durability.runtime as durable_agent_module
from axis.durability.activities import capture_version_manifest, set_worker_runtime_info
from axis.durability.runtime import build_durable_agent, seed_browser_tools, set_durable_agent
from axis.durability.leases import (
    BrowserLeaseWorkflow, LeaseWorkflowInput, _AcquireSignal, _ReleaseSignal, _RenewSignal,
    acquire_result, browser_scope_key, fenced_operation_result,
)
from axis.durability.models import (
    AxisDurableDeps,
    AxisJobInput,
    AxisVersionManifest,
    BrowserRecoveryProjection,
    DurableEffectRecord,
    DurableRuntimeSettings,
    LeaseOwner,
    PauseSignal,
    RebindBrowserSignal,
    RebindCandidate,
    ResumeSignal,
    SelectRebindCandidateSignal,
    SubmitApprovalSignal,
    SubmitUserInputSignal,
    WorkerRuntimeInfo,
    derive_effect_id,
)
from axis.durability.reconciliation import classify_reconciliation
from axis.durability.workflow import AxisJobWorkflow
from axis.durability.worker import registered_components
from axis.effects import (
    AcceptanceCriterion, ProposedEffect, TaskIntent, classify_mutation_outcome,
)
from browser_agent_tools import BrowserAgentTools, FirewallConfig
from browser_bridge_client import BrowserBridgeClient

try:
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
except ImportError:  # pragma: no cover - dependency pin guarantees this is present
    PydanticAIPlugin = None  # type: ignore[assignment]


def _manifest(**overrides: Any) -> AxisVersionManifest:
    base = dict(
        agent_version="axis-phase3.0", pydantic_ai_version="2.40.0", harness_version="0.29.0",
        temporal_sdk_version="1.32.0", browser_contract_version="2.0.0", browser_contract_hash="abc123",
        prompt_version="p1", policy_version="pol1", config_hash="cfg1", result_schema_version="r1",
        workflow_schema_version="axis-job-state.v1",
    )
    base.update(overrides)
    return AxisVersionManifest(**base)


class TestDurableModels(unittest.TestCase):
    def test_derive_effect_id_is_deterministic(self):
        self.assertEqual(derive_effect_id("j1", "e1", "t1", 1), derive_effect_id("j1", "e1", "t1", 1))

    def test_derive_effect_id_differs_per_ordinal(self):
        self.assertNotEqual(derive_effect_id("j1", "e1", "t1", 1), derive_effect_id("j1", "e1", "t1", 2))

    def test_deps_round_trips_through_pydantic_type_adapter(self):
        import pydantic

        deps = AxisDurableDeps(run_id="r1", job_id="j1", browser_scope_key="s1", firewall=FirewallConfig())
        ta = pydantic.TypeAdapter(AxisDurableDeps)
        self.assertEqual(ta.validate_python(ta.dump_python(deps)), deps)

    def test_no_raw_browser_ids_in_durable_effect_record_fields(self):
        forbidden = {"tabId", "windowId", "groupId", "frameId", "bridgeSessionId"}
        fields = set(DurableEffectRecord.model_fields.keys())
        self.assertFalse(forbidden & fields)

    def test_version_manifest_compatible_with_itself(self):
        m = _manifest()
        self.assertTrue(m.is_compatible_with(_manifest()))

    def test_version_manifest_incompatible_on_contract_change(self):
        m = _manifest()
        self.assertFalse(m.is_compatible_with(_manifest(browser_contract_hash="different")))

    def test_version_manifest_compatible_despite_agent_version_bump(self):
        m = _manifest()
        self.assertTrue(m.is_compatible_with(_manifest(agent_version="axis-phase3.1")))


class TestReconciliationClassification(unittest.TestCase):
    def test_all_required_passed_is_applied(self):
        self.assertEqual(classify_reconciliation({"c1", "c2"}, {"c1", "c2"}, set()), "applied")

    def test_explicit_failure_with_no_pass_is_inconclusive(self):
        self.assertEqual(classify_reconciliation({"c1"}, set(), {"c1"}), "inconclusive")

    def test_trusted_pre_dispatch_record_is_not_applied(self):
        self.assertEqual(
            classify_reconciliation({"c1"}, set(), {"c1"}, dispatch_proven_not_started=True),
            "not_applied",
        )

    def test_nothing_checked_is_inconclusive(self):
        self.assertEqual(classify_reconciliation({"c1"}, set(), set()), "inconclusive")

    def test_partial_pass_is_inconclusive_not_applied(self):
        self.assertEqual(classify_reconciliation({"c1", "c2"}, {"c1"}, set()), "inconclusive")

    def test_no_required_criteria_is_inconclusive(self):
        self.assertEqual(classify_reconciliation(set(), set(), set()), "inconclusive")


class TestLeaseDecisionLogic(unittest.TestCase):
    def test_scope_key_is_one_way_derived_and_stable(self):
        key1 = browser_scope_key(bridge_host="localhost", bridge_port=8765)
        key2 = browser_scope_key(bridge_host="localhost", bridge_port=8765)
        self.assertEqual(key1, key2)
        self.assertNotIn("8765", key1)  # not a raw reversible identifier

    def test_different_scopes_get_different_keys(self):
        key1 = browser_scope_key(bridge_host="a", bridge_port=1)
        key2 = browser_scope_key(bridge_host="b", bridge_port=1)
        self.assertNotEqual(key1, key2)

    def test_acquire_result_true_only_for_matching_owner(self):
        owner = LeaseOwner(job_id="job-a", token="tok-a")
        self.assertTrue(acquire_result(owner, "job-a", "tok-a").acquired)
        self.assertFalse(acquire_result(owner, "job-b", "tok-b").acquired)
        self.assertFalse(acquire_result(None, "job-a", "tok-a").acquired)


# ---------------------------------------------------------------------------
# Temporal integration tests
# ---------------------------------------------------------------------------


def _rpc_success(method, params=None, **_):
    if method == "page.accessibilityTree":
        return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://a", "title": "t", "snapshotId": "snap-1"}
    if method in ("locator.fill", "locator.click", "locator.setInputFiles"):
        return {"whatChanged": None}
    if method == "expect.locator.toHaveValue":
        return {}
    raise AssertionError(f"unexpected rpc: {method} {params}")


def _seed_mock_browser_tools(scope_key: str, tab_id: int = 456):
    import browser_agent_tools as bat
    from browser_bridge_client import BrowserBridgeClient

    client = MagicMock(spec=BrowserBridgeClient)
    client.rpc.side_effect = lambda method, params=None, **_: (
        {"tabs": [{"id": tab_id, "windowId": 1, "active": True, "url": "https://a", "title": "t"}]} if method == "tabs.list"
        else _rpc_success(method, params)
    )
    tools = bat.BrowserAgentTools(client)
    seed_browser_tools(scope_key, tools)
    return tools, client


def _intent_args(*, risk="reversible_local", actions=("fill",), max_mutations=3, assertion=None) -> Dict[str, Any]:
    criterion = assertion or {"assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo"}
    return {
        "goal": "Update the display name.", "constraints": [], "ambiguities": [],
        "proposed_effects": [{
            "effect_key": "e1", "summary": "Fill the display name field.", "risk": risk,
            "allowed_tools": ["browser_act"], "allowed_actions": list(actions),
            "max_browser_mutations": max_mutations, "acceptance_criterion_ids": ["c1"],
        }],
        "acceptance_criteria": [{"criterion_id": "c1", "description": "name updated", "assertion": criterion}],
    }


class _DurableTestHarness:
    """Starts one time-skipping Temporal environment + worker for one test
    body. Not shared across tests — simpler and avoids event-loop reuse
    pitfalls, at the cost of a per-test startup (the test-server binary is
    cached after the first download)."""

    def __init__(
        self, scripted, task_queue: Optional[str] = None, *, leases_enabled: bool = True,
        runtime_settings: Optional[DurableRuntimeSettings] = None,
    ):
        self.scripted = scripted
        self.task_queue = task_queue or f"axis-test-{uuid.uuid4().hex[:8]}"
        self.env: Optional[WorkflowEnvironment] = None
        self.client: Optional[Client] = None
        self.worker: Optional[Worker] = None
        self._worker_cm = None
        self.leases_enabled = leases_enabled
        self.runtime_settings = runtime_settings

    async def __aenter__(self) -> "_DurableTestHarness":
        from axis.config import load_axis_config

        self.env = await WorkflowEnvironment.start_time_skipping()
        self.client = await Client.connect(
            self.env.client.service_client.config.target_host, plugins=[PydanticAIPlugin()],
        )
        cfg = load_axis_config()
        agent, durability = build_durable_agent(cfg, FunctionModel(self.scripted))
        set_durable_agent(agent)
        set_worker_runtime_info(WorkerRuntimeInfo(
            manifest=_manifest(),
            settings=self.runtime_settings or DurableRuntimeSettings(
                leases_enabled=self.leases_enabled, lease_conflict_policy="fail",
            ),
        ))
        workflows, activities = registered_components(durability)
        self._worker_cm = Worker(
            self.client, task_queue=self.task_queue, workflows=workflows, activities=activities,
        )
        self.worker = await self._worker_cm.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._worker_cm.__aexit__(*exc_info)
        await self.env.shutdown()

    async def start_job(
        self, *, job_id: Optional[str] = None, scope_key: str = "test-scope", lease_conflict_policy: str = "wait",
    ) -> Any:
        job_id = job_id or f"job-{uuid.uuid4().hex[:8]}"
        job_input = AxisJobInput(
            job_id=job_id, task_summary="Do the task.", browser_scope_key=scope_key,
        )
        return await self.client.start_workflow(
            AxisJobWorkflow.run, job_input, id=job_id, task_queue=self.task_queue, execution_timeout=timedelta(seconds=30),
        )


def _final_result_call(info, **kwargs):
    name = info.output_tools[0].name
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=kwargs)])


def _last_tool_result(messages, tool_name: str):
    from pydantic_ai.messages import ToolReturnPart

    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                return part.content
    return None


def _active_tab_handle(messages) -> str:
    result = _last_tool_result(messages, "browser_tabs")
    assert result is not None and result.get("ok"), result
    tabs = result["data"]["tabs"]
    return next(t["browserSessionId"] for t in tabs if t.get("active"))


def _local_turn(messages) -> int:
    """1-based turn number *within the current `agent.run()` call* — each
    fresh call (a reconciliation sub-conversation, or the post-
    reconciliation continuation segment) starts with its own `messages`
    list, independent of the global scripted-step counter."""
    return sum(1 for m in messages if isinstance(m, ModelResponse)) + 1


def run_async(coro):
    return asyncio.run(coro)


class TestReadOnlyJob(unittest.TestCase):
    def test_read_only_task_completes_without_intent_or_plan(self):
        step = {"n": 0}
        scope_key = "scope-readonly"
        _seed_mock_browser_tools(scope_key)

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="browser_observe", args={"browserSessionId": _active_tab_handle(messages)},
                )])
            return _final_result_call(info, status="completed", summary="looked", verification_summary="listed tabs")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)
                result = await handle.result()
                self.assertEqual(result.status, "completed")

        run_async(body())


class TestMutationTrajectory(unittest.TestCase):
    def test_full_mutation_trajectory_completes_with_checkpoint(self):
        scope_key = "scope-mutation"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 5:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "fill", "value": "Axis POC Demo", "locator": {"selector": "#display-name"},
                })])
            if n == 6:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": handle, "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
                })])
            if n == 7:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="asserted")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)
                result = await handle.result()
                self.assertEqual(result.status, "completed")
                fill_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.fill"]
                self.assertEqual(len(fill_calls), 1)

        run_async(body())

    def test_mutation_without_intent_is_blocked_before_bridge_rpc(self):
        scope_key = "scope-blocked"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": "tab-1", "action": "fill", "value": "x", "locator": {"selector": "#x"},
                })])
            return _final_result_call(info, status="failed", summary="blocked", error_code="TASK_INTENT_REQUIRED")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)
                result = await handle.result()
                self.assertNotIn("locator.fill", [c.args[0] for c in client.rpc.call_args_list])

        run_async(body())


class TestApprovalFlow(unittest.TestCase):
    def test_external_effect_defers_and_denial_prevents_execution(self):
        scope_key = "scope-approval-deny"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": "tab-1", "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="APPROVAL_DENIED")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)

                async def wait_for_pending():
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                state = await wait_for_pending()
                self.assertIsNotNone(state.pending_approval)
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state.pending_approval.approval_id, approved=False, decision_nonce="n1",
                ))
                result = await handle.result()
                self.assertEqual(result.status, "failed")
                self.assertNotIn("locator.setInputFiles", [c.args[0] for c in client.rpc.call_args_list])

        run_async(body())

    def test_duplicate_approval_signal_is_idempotent_and_conflicting_signal_is_rejected(self):
        scope_key = "scope-approval-idempotent"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": "tab-1", "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="APPROVAL_DENIED")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)

                async def wait_for_pending():
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                state = await wait_for_pending()
                request_id = state.pending_approval.approval_id
                # Duplicate identical signal (same nonce) — idempotent no-op.
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(request_id=request_id, approved=False, decision_nonce="n1"))
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(request_id=request_id, approved=False, decision_nonce="n1"))
                # Conflicting decision for the same request — rejected (ignored).
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(request_id=request_id, approved=True, decision_nonce="n2"))
                result = await handle.result()
                self.assertEqual(result.status, "failed")
                self.assertNotIn("locator.setInputFiles", [c.args[0] for c in client.rpc.call_args_list])

        run_async(body())

    def test_approved_call_executes_exactly_once(self):
        scope_key = "scope-approval-approve"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 5:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="ACCEPTANCE_UNRESOLVED")

        async def body():
            settings = DurableRuntimeSettings(
                leases_enabled=True, lease_conflict_policy="fail", lease_duration_seconds=1,
            )
            async with _DurableTestHarness(scripted, runtime_settings=settings) as h:
                handle = await h.start_job(scope_key=scope_key)

                async def wait_for_pending():
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                state = await wait_for_pending()
                # Let the original fencing proof expire. Approval must
                # reacquire with a new generation before the handler runs.
                await h.env.sleep(timedelta(seconds=2))
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state.pending_approval.approval_id, approved=True, decision_nonce="n1",
                ))
                result = await handle.result()
                upload_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.setInputFiles"]
                self.assertEqual(len(upload_calls), 1)

        run_async(body())


class TestReconciliationEndToEnd(unittest.TestCase):
    def test_ambiguous_mutation_is_never_repeated_and_reconciles_to_applied(self):
        """Forces `locator.fill` to raise mid-mutation (simulating a worker
        crash/transport loss whose outcome cannot be proven) and asserts
        the central safety invariant directly: `browser_act fill` is
        called at most once for the whole job, never blindly retried —
        the effect instead moves to `unknown_after_crash`, and a fresh
        `browser_observe`/`browser_assert` reconciliation determines it was
        actually applied, before the task continues to completion."""
        scope_key = "scope-reconcile-applied"
        tools, client = _seed_mock_browser_tools(scope_key)

        def rpc_side_effect(method, params=None, **_):
            if method == "tabs.list":
                return {"tabs": [{"id": 456, "windowId": 1, "active": True, "url": "https://a", "title": "t"}]}
            if method == "locator.fill":
                raise RuntimeError("simulated worker crash / transport loss mid-mutation")
            return _rpc_success(method, params)

        client.rpc.side_effect = rpc_side_effect
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 5:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "fill", "value": "Axis POC Demo", "locator": {"selector": "#display-name"},
                })])
            if n == 10:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="update_task_status", args={"task_id": "t1", "status": "completed"},
                )])
            if n > 10:
                return _final_result_call(info, status="completed", summary="reconciled and done", verification_summary="reconciled")
            # From here on, either the reconciliation sub-conversation (a
            # fresh history) or the post-reconciliation continuation
            # segment (also a fresh history) is asking questions — both
            # follow the same observe-then-assert-then-finish shape, so a
            # position within *this* conversation, not the global step
            # counter, decides what to return.
            local_n = _local_turn(messages)
            if local_n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if local_n == 2:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": handle})])
            if local_n == 3:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": handle, "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
                })])
            if n == 9:
                return _final_result_call(info, status="failed", summary="reconciliation checked", error_code="RECONCILIATION_COMPLETE")
            return _final_result_call(info, status="completed", summary="reconciled and done", verification_summary="reconciled")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle = await h.start_job(scope_key=scope_key)
                result = await handle.result()
                self.assertEqual(result.status, "completed")

                fill_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.fill"]
                self.assertEqual(len(fill_calls), 1)  # the ambiguous mutation was never repeated

                state = await handle.query(AxisJobWorkflow.get_job_state)
                effect = next(e for e in state.effects if e.effect_key == "e1")
                self.assertEqual(effect.status, "reconciled_succeeded")

        run_async(body())


class TestLeaseConcurrency(unittest.TestCase):
    def test_lease_is_released_during_approval_wait_so_a_second_job_is_not_blocked(self):
        """Fix #1: the browser lease must be released before waiting for
        human approval, not held idle for the whole wait. Two
        `AxisJobWorkflow`s target the same `browser_scope_key`; job1 parks
        in `waiting_for_approval` (releasing its lease per the fix), and
        job2 — which would previously have been rejected with
        `BROWSER_SCOPE_BUSY` while job1 merely waited on a human — must be
        able to acquire the now-free lease and reach its OWN pending
        approval too. Denying both, then a third job against the same
        now-free scope succeeding, proves the lease is never leaked."""
        scope_key = "scope-lease-contention"
        tools, client = _seed_mock_browser_tools(scope_key)

        def scripted(messages, info):
            # Each job has its own independent message history, so a
            # per-conversation turn count (not a globally shared counter)
            # correctly drives two concurrently-running jobs through the
            # same fixed sequence without interleaving into each other.
            turn = _local_turn(messages)
            if turn == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if turn == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if turn == 4:
                handle = _active_tab_handle(messages) if _last_tool_result(messages, "browser_tabs") else "placeholder"
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="APPROVAL_DENIED")

        async def body():
            async with _DurableTestHarness(scripted) as h:
                handle1 = await h.start_job(job_id="job-lease-1", scope_key=scope_key)

                async def wait_for_pending(handle):
                    for _ in range(80):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                await wait_for_pending(handle1)  # job1 parked awaiting approval; its lease is released

                handle2 = await h.start_job(job_id="job-lease-2", scope_key=scope_key, lease_conflict_policy="fail")
                state2 = await wait_for_pending(handle2)  # must NOT be rejected as BROWSER_SCOPE_BUSY

                # job1 is unaffected by job2 also reaching pending approval.
                state1 = await handle1.query(AxisJobWorkflow.get_job_state)
                self.assertEqual(state1.status, "waiting_for_approval")

                await handle1.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state1.pending_approval.approval_id, approved=False, decision_nonce="n1",
                ))
                result1 = await handle1.result()
                self.assertEqual(result1.status, "failed")

                await handle2.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state2.pending_approval.approval_id, approved=False, decision_nonce="n1",
                ))
                result2 = await handle2.result()
                self.assertEqual(result2.status, "failed")

                # A third job against the same now-free scope must be able
                # to acquire the lease — whatever it does with the task
                # afterward, it must not fail with BROWSER_SCOPE_BUSY,
                # which would mean an earlier job's release leaked.
                handle3 = await h.start_job(job_id="job-lease-3", scope_key=scope_key, lease_conflict_policy="fail")
                state3 = await wait_for_pending(handle3)
                await handle3.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state3.pending_approval.approval_id, approved=False, decision_nonce="n1",
                ))
                result3 = await handle3.result()
                self.assertNotEqual(result3.error_code, "BROWSER_SCOPE_BUSY")

        run_async(body())

    def test_a_second_holder_cannot_acquire_while_a_mutation_lease_is_outstanding(self):
        """A long-running mutation holds its lease continuously (it is only
        ever released around a *wait*, never mid-execution) — proven here
        directly at the lease-fencing layer that actually enforces
        exclusivity: while job1's lease is outstanding and unexpired, a
        second holder's acquire attempt with a different token is refused,
        never silently granted."""
        async def body():
            async with _DurableTestHarness(lambda messages, info: None, leases_enabled=False) as h:
                handle = await h.client.start_workflow(
                    BrowserLeaseWorkflow.run, LeaseWorkflowInput(),
                    id=f"lease-{uuid.uuid4().hex[:8]}", task_queue=h.task_queue,
                )
                await handle.signal(BrowserLeaseWorkflow.acquire, _AcquireSignal(
                    job_id="job1", token="job1-token", duration_seconds=120,
                ))
                # Simulate the mutation still running well past a short
                # instant — the lease workflow's own clock (via time-skipping)
                # advances, but the lease itself must still be outstanding.
                await h.env.sleep(timedelta(seconds=60))
                owner_mid_mutation = await handle.query(BrowserLeaseWorkflow.get_owner)
                self.assertIsNotNone(owner_mid_mutation)
                self.assertEqual(owner_mid_mutation.job_id, "job1")

                await handle.signal(BrowserLeaseWorkflow.acquire, _AcquireSignal(
                    job_id="job2", token="job2-token", duration_seconds=30,
                ))
                owner_after_contention = await handle.query(BrowserLeaseWorkflow.get_owner)
                self.assertEqual(owner_after_contention.job_id, "job1")  # job2 never took over
                self.assertEqual(owner_after_contention.generation, owner_mid_mutation.generation)

        run_async(body())


class TestApprovalWaitLongerThanLeaseDuration(unittest.TestCase):
    def test_approval_wait_past_the_lease_duration_still_resumes_safely(self):
        """The lease is released for the whole approval wait (fix #1), so
        it does not matter that the wait outlives `lease_duration_seconds`
        — nothing is depending on the old lease surviving. Resuming after
        approval reacquires a fresh lease and completes the mutation."""
        scope_key = "scope-lease-outlives-wait"
        tools, client = _seed_mock_browser_tools(scope_key)

        def scripted(messages, info):
            turn = _local_turn(messages)
            if turn == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if turn == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if turn == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if turn == 5:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="ACCEPTANCE_UNRESOLVED")

        settings = DurableRuntimeSettings(leases_enabled=True, lease_conflict_policy="wait", lease_duration_seconds=1)

        async def body():
            async with _DurableTestHarness(scripted, runtime_settings=settings) as h:
                handle = await h.start_job(scope_key=scope_key)

                async def wait_for_pending():
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                await wait_for_pending()
                # Simulated workflow-clock time far past the 1-second lease
                # duration — proves the wait does not depend on the old
                # lease still being valid.
                await h.env.sleep(timedelta(seconds=10))
                state = await handle.query(AxisJobWorkflow.get_job_state)
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state.pending_approval.approval_id, approved=True, decision_nonce="n1",
                ))
                result = await handle.result()
                upload_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.setInputFiles"]
                self.assertEqual(len(upload_calls), 1)
                self.assertNotEqual(result.error_code, "BROWSER_SCOPE_BUSY")

        run_async(body())


class TestFencedTokenNeverReachesBridge(unittest.TestCase):
    def test_stale_lease_proof_is_rejected_before_any_bridge_rpc(self):
        """A stale (wrong generation) fencing token must never reach the
        bridge: durable_browser_handler's own lease-currency check refuses
        to dispatch, so the mock bridge records zero calls."""
        from axis.durability.models import LeaseProof

        scope = "scope-stale-token"
        tools, client = _seed_mock_browser_tools(scope)
        deps = AxisDurableDeps(
            run_id="r", job_id="job-real", browser_scope_key=scope,
            lease_proof=LeaseProof(token="stale-token", generation=1, expires_at=time.time() + 3600),
        )
        ctx = MagicMock()
        ctx.deps = deps

        async def body():
            async with _DurableTestHarness(lambda messages, info: None, leases_enabled=False) as h:
                # Stand up a real BrowserLeaseWorkflow under a *different*,
                # current owner/generation than what `deps.lease_proof` claims.
                handle = await h.client.start_workflow(
                    BrowserLeaseWorkflow.run, LeaseWorkflowInput(),
                    id=scope, task_queue=h.task_queue,
                )
                await handle.signal(BrowserLeaseWorkflow.acquire, _AcquireSignal(
                    job_id="job-real", token="current-token", duration_seconds=120,
                ))
                with patch("axis.durability.runtime.activity.client", return_value=h.client):
                    result = await durable_agent_module.durable_browser_handler(
                        "browser_act", ctx, {
                            "browserSessionId": "tab-1", "action": "click", "locator": {"selector": "#x"},
                        },
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "LEASE_NOT_HELD")
                self.assertEqual(client.rpc.call_count, 0)

        run_async(body())


class TestWorkerRestartDurability(unittest.TestCase):
    def test_deferred_approval_workflow_replays_deterministically_after_restart(self):
        """A completed approval history replays with fresh agent/cache state."""
        scope_key = "scope-restart-replay"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(risk="external_effect", actions=["upload"]))])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if n == 5:
                handle = _active_tab_handle(messages)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": handle, "action": "upload", "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            return _final_result_call(info, status="failed", summary="stop", error_code="ACCEPTANCE_UNRESOLVED")

        async def body():
            from temporalio.worker import Replayer

            async with _DurableTestHarness(scripted, leases_enabled=False) as h:
                handle = await h.start_job(scope_key=scope_key)

                async def wait_for_pending():
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_approval is not None:
                            return state
                        await asyncio.sleep(0.1)
                    raise AssertionError("approval never became pending")

                state = await wait_for_pending()
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state.pending_approval.approval_id, approved=True, decision_nonce="n1",
                ))
                result = await handle.result()
                self.assertEqual(result.status, "failed")

                # Fetch the completed execution's real history — this is
                # what the Temporal server durably retains across any
                # worker crash/restart; nothing worker-local is used below.
                history = await handle.fetch_history()

                # Simulate the restart: an independently-built agent
                # instance, `set_durable_agent()` called again from
                # scratch, exactly as a fresh worker process would do.
                from axis.config import load_axis_config

                durable_agent_module.clear_plan_cache()
                durable_agent_module.clear_browser_cache()
                cfg = load_axis_config()
                agent2, _durability2 = build_durable_agent(cfg, FunctionModel(scripted))
                set_durable_agent(agent2)

                replayer = Replayer(workflows=[AxisJobWorkflow], plugins=[PydanticAIPlugin()])
                replay_result = await replayer.replay_workflow(history)
                self.assertIsNone(replay_result.replay_failure)

        run_async(body())


class TestRepairArchitecture(unittest.TestCase):
    def test_durable_build_calls_the_authoritative_root_builder(self):
        import axis.agent as shared_agent
        from axis.config import load_axis_config

        cfg = load_axis_config()
        with patch.object(
            durable_agent_module, "build_axis_agent", wraps=shared_agent.build_axis_agent,
        ) as builder:
            agent, durability = build_durable_agent(cfg, FunctionModel(lambda messages, info: None))
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(agent.name, "axis_agent")
        self.assertIsNotNone(durability)

    def test_no_duplicate_durable_agent_module_remains(self):
        package = Path(durable_agent_module.__file__).parent
        self.assertFalse((package / "agent.py").exists())
        self.assertFalse((package / "plan_cache.py").exists())

    def test_worker_registration_is_unique_and_event_is_once(self):
        from axis.config import load_axis_config

        _, durability = build_durable_agent(load_axis_config(), FunctionModel(lambda messages, info: None))
        workflows, activities = registered_components(durability)
        workflow_names = [getattr(w, "__name__", "") for w in workflows]
        activity_names = [
            getattr(getattr(a, "__temporal_activity_definition", None), "name", None) or a.__name__
            for a in activities
        ]
        self.assertEqual(workflow_names, ["AxisJobWorkflow", "BrowserLeaseWorkflow"])
        self.assertEqual(len(activity_names), len(set(activity_names)))
        self.assertEqual(activity_names.count("emit_durable_event"), 1)


class TestRestartSafePlanning(unittest.TestCase):
    def test_revision_mismatch_rehydrates_from_durable_projection(self):
        from axis.durability.runtime import _resolve_plan_store, clear_plan_cache
        from axis.models import AxisControlState
        from axis.planning import AxisPlanItem

        deps = AxisDurableDeps(
            run_id="r", job_id="plan-job", browser_scope_key="scope", leases_enabled=False,
            control_state=AxisControlState(
                plan=[AxisPlanItem(task_id="t1", content="one", status="in_progress")],
                plan_revision=4,
            ),
        )
        ctx = MagicMock()
        ctx.deps = deps
        first = _resolve_plan_store(ctx)
        self.assertEqual(run_async(first.get_items())[0].id, "t1")
        run_async(first.set_items([]))

        deps.control_state.plan = [AxisPlanItem(task_id="t2", content="newer", status="pending")]
        deps.control_state.plan_revision = 5
        second = _resolve_plan_store(ctx)
        self.assertEqual([item.id for item in run_async(second.get_items())], ["t2"])

        clear_plan_cache("plan-job")
        third = _resolve_plan_store(ctx)
        self.assertEqual([item.id for item in run_async(third.get_items())], ["t2"])

    def test_stale_or_empty_projection_cannot_overwrite_newer_state(self):
        from axis.durability.runtime import apply_plan_projection
        from axis.models import AxisControlState
        from axis.planning import AxisPlanItem

        control = AxisControlState(
            plan=[AxisPlanItem(task_id="new", content="new", status="pending")], plan_revision=8,
        )
        self.assertFalse(apply_plan_projection(control, {
            "_axis_plan": [], "_axis_plan_base_revision": 7, "_axis_plan_revision": 8,
        }))
        self.assertEqual(control.plan[0].task_id, "new")
        self.assertEqual(control.plan_revision, 8)


class TestMutationOutcomeClassifier(unittest.TestCase):
    def test_audited_pre_dispatch_rejection_is_known_not_applied(self):
        result = {"ok": False, "error": {"code": "STALE_OBSERVATION"}}
        self.assertEqual(
            classify_mutation_outcome(execution_stage="authorized", result=result),
            "known_not_applied",
        )

    def test_successful_response_is_applied_unverified(self):
        self.assertEqual(
            classify_mutation_outcome(execution_stage="response_received", result={"ok": True}),
            "applied_unverified",
        )

    def test_every_post_dispatch_failure_kind_defaults_unknown(self):
        for failure in ("timeout", "disconnect", "worker_crash", "lost_response", "transport", "cancelled", "other"):
            with self.subTest(failure=failure):
                self.assertEqual(
                    classify_mutation_outcome(
                        execution_stage="dispatch_started", result={"ok": False, "error": {"code": "ANY"}},
                        failure_kind=failure,
                    ),
                    "unknown",
                )


class TestBrowserRecoveryProjection(unittest.TestCase):
    def _tools(self, client: MagicMock, *, duplicate: bool = False) -> BrowserAgentTools:
        tabs = [{"id": 10, "windowId": 1, "active": True, "url": "https://a", "title": "t"}]
        if duplicate:
            tabs.append({"id": 11, "windowId": 1, "active": False, "url": "https://a", "title": "t"})
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": tabs} if method == "tabs.list" else _rpc_success(method, params)
        )
        return BrowserAgentTools(client)

    def test_cache_loss_invalidates_old_refs_and_recovers_exact_tab(self):
        scope = "projection-cache-loss"
        old_client = MagicMock(spec=BrowserBridgeClient)
        old_tools = self._tools(old_client)
        seed_browser_tools(scope, old_tools)
        deps = AxisDurableDeps(run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False)
        ctx = MagicMock()
        ctx.deps = deps

        listed = run_async(durable_agent_module.durable_browser_handler("browser_tabs", ctx, {"operation": "list"}))
        deps.browser_projection = BrowserRecoveryProjection.model_validate(listed["_axis_internal"]["projection"])
        handle = listed["data"]["tabs"][0]["browserSessionId"]
        observed = run_async(durable_agent_module.durable_browser_handler(
            "browser_observe", ctx, {"browserSessionId": handle},
        ))
        deps.browser_projection = BrowserRecoveryProjection.model_validate(observed["_axis_internal"]["projection"])
        prior_generation = deps.browser_projection.observation_generation

        new_client = MagicMock(spec=BrowserBridgeClient)
        new_tools = self._tools(new_client)
        durable_agent_module.clear_browser_cache(scope)
        with patch.object(durable_agent_module, "_new_browser_tools", return_value=new_tools):
            acted = run_async(durable_agent_module.durable_browser_handler(
                "browser_act", ctx, {
                    "browserSessionId": handle, "action": "click", "locator": {"ref": "f0:e1"},
                },
            ))
        self.assertFalse(acted["ok"])
        self.assertIn(acted["error"]["code"], {"STALE_OBSERVATION", "REF_NOT_FOUND", "INVALID_ARGUMENT"})
        self.assertGreater(acted["_axis_internal"]["projection"]["observation_generation"], prior_generation)
        self.assertNotIn("locator.click", [call.args[0] for call in new_client.rpc.call_args_list])

    def test_ambiguous_recovery_never_selects_another_tab(self):
        scope = "projection-ambiguous"
        deps = AxisDurableDeps(
            run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False,
            browser_projection=BrowserRecoveryProjection(
                durable_tab_key="tab-stable", last_known_sanitized_url="https://a",
                last_known_title="t", active_tab_intent=False,
            ),
        )
        client = MagicMock(spec=BrowserBridgeClient)
        tools = self._tools(client, duplicate=True)
        ctx = MagicMock()
        ctx.deps = deps
        durable_agent_module.clear_browser_cache(scope)
        with patch.object(durable_agent_module, "_new_browser_tools", return_value=tools):
            result = run_async(durable_agent_module.durable_browser_handler(
                "browser_act", ctx, {
                    "browserSessionId": "tab-stable", "action": "click", "locator": {"selector": "#save"},
                },
            ))
        self.assertEqual(result["error"]["code"], "BROWSER_REBIND_REQUIRED")
        self.assertNotIn("locator.click", [call.args[0] for call in client.rpc.call_args_list])

    def test_recovery_without_trusted_identity_does_not_choose_the_only_tab(self):
        scope = "projection-no-identity"
        deps = AxisDurableDeps(
            run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False,
            browser_projection=BrowserRecoveryProjection(durable_tab_key="tab-stable"),
        )
        client = MagicMock(spec=BrowserBridgeClient)
        tools = self._tools(client)
        ctx = MagicMock()
        ctx.deps = deps
        durable_agent_module.clear_browser_cache(scope)
        with patch.object(durable_agent_module, "_new_browser_tools", return_value=tools):
            result = run_async(durable_agent_module.durable_browser_handler(
                "browser_act", ctx, {
                    "browserSessionId": "tab-stable", "action": "click", "locator": {"selector": "#save"},
                },
            ))
        self.assertEqual(result["error"]["code"], "BROWSER_REBIND_REQUIRED")
        self.assertNotIn("locator.click", [call.args[0] for call in client.rpc.call_args_list])


class TestUserInputAndVersionFlow(unittest.TestCase):
    def test_needs_user_input_waits_rejects_stale_and_resumes(self):
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return _final_result_call(
                    info, status="needs_user_input", summary="need a choice", question="Proceed?",
                )
            return _final_result_call(info, status="failed", summary="answered", error_code="USER_ANSWERED")

        async def body():
            async with _DurableTestHarness(scripted, leases_enabled=False) as h:
                handle = await h.start_job(scope_key="input-flow")
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.status == "waiting_for_user":
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(state.status, "waiting_for_user")
                pending = state.pending_question
                await handle.signal(AxisJobWorkflow.pause_job, PauseSignal())
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.status == "paused":
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(state.status, "paused")
                await handle.signal(AxisJobWorkflow.resume_job, ResumeSignal())
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.status == "waiting_for_user":
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(state.status, "waiting_for_user")
                await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
                    request_id="stale", answer_nonce="n0", text="wrong",
                ))
                await asyncio.sleep(0.1)
                self.assertEqual((await handle.query(AxisJobWorkflow.get_job_state)).status, "waiting_for_user")
                signal = SubmitUserInputSignal(
                    request_id=pending.request_id, answer_nonce="n1", text="yes",
                )
                await handle.signal(AxisJobWorkflow.submit_user_input, signal)
                await handle.signal(AxisJobWorkflow.submit_user_input, signal)
                result = await handle.result()
                self.assertEqual(result.error_code, "USER_ANSWERED")
                self.assertEqual(step["n"], 2)

        run_async(body())

    def test_rebind_invalidates_recovers_and_requires_fresh_observation(self):
        scope = "rebind-flow"
        old_tools, _old_client = _seed_mock_browser_tools(scope, tab_id=10)
        new_client = MagicMock(spec=BrowserBridgeClient)
        new_client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{
                "id": 99, "windowId": 7, "active": True,
                "url": "https://a", "title": "t",
            }]} if method == "tabs.list" else _rpc_success(method, params)
        )
        new_tools = BrowserAgentTools(new_client)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] in (1, 4):
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if step["n"] in (2, 5):
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="browser_observe", args={"browserSessionId": _active_tab_handle(messages)},
                )])
            if step["n"] == 3:
                return _final_result_call(
                    info, status="needs_user_input", summary="waiting", question="Continue?",
                )
            return _final_result_call(
                info, status="completed", summary="rebound", verification_summary="freshly observed",
            )

        async def body():
            with patch.object(durable_agent_module, "_new_browser_tools", return_value=new_tools):
                async with _DurableTestHarness(scripted, leases_enabled=True) as h:
                    handle = await h.start_job(scope_key=scope)
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.pending_question:
                            break
                        await asyncio.sleep(0.1)
                    old_observation_generation = state.observation_generation
                    await handle.signal(AxisJobWorkflow.rebind_browser, RebindBrowserSignal())
                    for _ in range(50):
                        state = await handle.query(AxisJobWorkflow.get_job_state)
                        if state.binding_generation >= 1 and not state.rebind_required:
                            break
                        await asyncio.sleep(0.1)
                    self.assertEqual(state.status, "waiting_for_user")
                    self.assertEqual(state.binding_generation, 1)
                    self.assertGreater(state.observation_generation, old_observation_generation)
                    self.assertFalse(state.rebind_required)
                    await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
                        request_id=state.pending_question.request_id, answer_nonce="after-rebind", text="yes",
                    ))
                    result = await handle.result()
                    self.assertEqual(result.status, "completed")
                    self.assertIn("page.accessibilityTree", [call.args[0] for call in new_client.rpc.call_args_list])

        run_async(body())

    def test_manifest_incompatibility_prevents_next_model_execution(self):
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            return _final_result_call(
                info, status="needs_user_input", summary="pause", question="Continue?",
            )

        async def body():
            async with _DurableTestHarness(scripted, leases_enabled=False) as h:
                handle = await h.start_job(scope_key="version-flow")
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.pending_question:
                        break
                    await asyncio.sleep(0.1)
                set_worker_runtime_info(WorkerRuntimeInfo(
                    manifest=_manifest(browser_contract_hash="incompatible"),
                    settings=DurableRuntimeSettings(leases_enabled=False),
                ))
                await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
                    request_id=state.pending_question.request_id, answer_nonce="next", text="yes",
                ))
                result = await handle.result()
                self.assertEqual(result.error_code, "VERSION_INCOMPATIBLE")
                self.assertEqual(step["n"], 1)

        run_async(body())


class TestFencedLeaseBehavior(unittest.TestCase):
    def test_fenced_operations_reject_expired_or_stale_owners(self):
        owner = LeaseOwner(job_id="job", token="token", generation=3, expires_at=10)
        self.assertTrue(fenced_operation_result(
            owner, job_id="job", token="token", generation=3, now=9,
        ).accepted)
        self.assertFalse(fenced_operation_result(
            owner, job_id="job", token="token", generation=2, now=9,
        ).accepted)
        self.assertFalse(fenced_operation_result(
            owner, job_id="job", token="token", generation=3, now=11,
        ).accepted)

    def test_lease_expires_reacquires_with_new_generation_and_fences_old_owner(self):
        async def body():
            async with _DurableTestHarness(lambda messages, info: None, leases_enabled=False) as h:
                handle = await h.client.start_workflow(
                    BrowserLeaseWorkflow.run, LeaseWorkflowInput(),
                    id=f"lease-{uuid.uuid4().hex[:8]}", task_queue=h.task_queue,
                )
                await handle.signal(BrowserLeaseWorkflow.acquire, _AcquireSignal(
                    job_id="old", token="old-token", duration_seconds=1,
                ))
                first = await handle.query(BrowserLeaseWorkflow.get_owner)
                self.assertEqual(first.generation, 1)
                await h.env.sleep(timedelta(seconds=2))
                self.assertIsNone(await handle.query(BrowserLeaseWorkflow.get_owner))
                await handle.signal(BrowserLeaseWorkflow.acquire, _AcquireSignal(
                    job_id="new", token="new-token", duration_seconds=10,
                ))
                second = await handle.query(BrowserLeaseWorkflow.get_owner)
                self.assertEqual(second.generation, 2)
                await handle.signal(BrowserLeaseWorkflow.release, _ReleaseSignal(
                    job_id="old", token="old-token", generation=1,
                ))
                self.assertEqual((await handle.query(BrowserLeaseWorkflow.get_owner)).job_id, "new")
                before = second.expires_at
                await handle.signal(BrowserLeaseWorkflow.renew, _RenewSignal(
                    job_id="new", token="new-token", generation=2, duration_seconds=20,
                ))
                self.assertGreater((await handle.query(BrowserLeaseWorkflow.get_owner)).expires_at, before)

        run_async(body())


class TestHistoryLimit(unittest.TestCase):
    def test_safe_boundary_continues_as_new_without_losing_state(self):
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return _final_result_call(info, status="needs_user_input", summary="ask", question="Continue?")
            return _final_result_call(info, status="failed", summary="continued", error_code="AFTER_CONTINUE")

        settings = DurableRuntimeSettings(leases_enabled=False, max_run_segments=1)

        async def body():
            async with _DurableTestHarness(scripted, runtime_settings=settings) as h:
                handle = await h.start_job(scope_key="history-safe")
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.pending_question:
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
                    request_id=state.pending_question.request_id, answer_nonce="go", text="yes",
                ))
                result = await handle.result()
                self.assertEqual(result.error_code, "AFTER_CONTINUE")
                latest = await handle.query(AxisJobWorkflow.get_job_state)
                self.assertEqual(latest.run_segments, 2)

        run_async(body())

    def test_unresolved_approved_effect_stops_at_history_limit_without_execution(self):
        client = MagicMock(spec=BrowserBridgeClient)
        settings = DurableRuntimeSettings(leases_enabled=False, max_run_segments=1)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="axis_set_task_intent",
                    args=_intent_args(risk="external_effect", actions=["upload"]),
                )])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="write_plan",
                    args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]},
                )])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"},
                )])
            return ModelResponse(parts=[ToolCallPart(
                tool_name="browser_act", tool_call_id="limited-upload",
                args={
                    "browserSessionId": "opaque-tab", "action": "upload",
                    "locator": {"selector": "#file"}, "files": ["a.txt"],
                },
            )])

        async def body():
            async with _DurableTestHarness(scripted, runtime_settings=settings) as h:
                handle = await h.start_job(scope_key="history-unresolved")
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.pending_approval:
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(
                    request_id=state.pending_approval.approval_id, approved=True, decision_nonce="approve",
                ))
                result = await handle.result()
                self.assertEqual(result.error_code, "HISTORY_LIMIT_REACHED")
                self.assertEqual(client.rpc.call_count, 0)

        run_async(body())


class TestNonIdempotentRetryClassification(unittest.TestCase):
    def test_navigation_tabs_and_capture_evidence_are_never_retryable_tools(self):
        from axis.effects import is_non_idempotent_operation

        self.assertTrue(is_non_idempotent_operation("browser_navigate"))
        self.assertTrue(is_non_idempotent_operation("browser_capture_evidence"))
        self.assertTrue(is_non_idempotent_operation("browser_tabs", operation="create"))
        self.assertTrue(is_non_idempotent_operation("browser_tabs", operation="close"))
        self.assertFalse(is_non_idempotent_operation("browser_tabs", operation="list"))
        self.assertTrue(is_non_idempotent_operation("browser_act", action="click"))
        self.assertFalse(is_non_idempotent_operation("browser_act", action="hover"))
        self.assertFalse(is_non_idempotent_operation("browser_observe"))
        self.assertFalse(is_non_idempotent_operation("browser_assert"))

    def test_activity_metadata_gives_exactly_one_attempt_to_every_non_read_tool(self):
        from axis.config import load_axis_config
        from axis.durability.runtime import _activity_metadata

        cfg = load_axis_config()
        for tool in ("browser_navigate", "browser_capture_evidence", "browser_act", "browser_tabs"):
            attempts = _activity_metadata(cfg, tool)["temporal"]["retry_policy"].maximum_attempts
            self.assertEqual(attempts, 1, tool)
        for tool in ("browser_observe", "browser_wait", "browser_assert", "browser_diagnose"):
            attempts = _activity_metadata(cfg, tool)["temporal"]["retry_policy"].maximum_attempts
            self.assertGreaterEqual(attempts, 1, tool)

    def test_a_second_dispatch_attempt_of_a_non_idempotent_call_is_refused_before_the_bridge(self):
        """Defense in depth beyond the (necessarily tool-level) Temporal
        retry policy: even if a non-idempotent call's activity were somehow
        attempted a second time, durable_browser_handler itself refuses to
        dispatch it once it can see (from the actual action/operation) that
        the call is non-idempotent and this is not the first attempt."""
        scope = "scope-non-idempotent-guard"
        tools, client = _seed_mock_browser_tools(scope)
        deps = AxisDurableDeps(run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False)
        ctx = MagicMock()
        ctx.deps = deps

        with patch.object(durable_agent_module, "_current_activity_attempt", return_value=2):
            result = run_async(durable_agent_module.durable_browser_handler(
                "browser_navigate", ctx, {"browserSessionId": "tab-1", "url": "https://a"},
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NON_IDEMPOTENT_RETRY_BLOCKED")
        self.assertEqual(client.rpc.call_count, 0)

    def test_first_attempt_of_a_non_idempotent_call_is_not_blocked_by_the_guard(self):
        scope = "scope-non-idempotent-first-attempt"
        tools, client = _seed_mock_browser_tools(scope)
        deps = AxisDurableDeps(run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False)
        ctx = MagicMock()
        ctx.deps = deps
        result = run_async(durable_agent_module.durable_browser_handler(
            "browser_tabs", ctx, {"operation": "list"},
        ))
        self.assertTrue(result["ok"])


class TestVersionIncompatibleActivityGuard(unittest.TestCase):
    def test_incompatible_worker_manifest_blocks_the_browser_handler_before_dispatch(self):
        """Fix #5: an activity stamped with the manifest the workflow last
        confirmed must refuse to execute the browser handler if the worker
        process actually running it now reports a different (incompatible)
        manifest — a controlled VERSION_INCOMPATIBLE result, never a bridge
        call under mismatched assumptions."""
        scope = "scope-version-incompatible"
        tools, client = _seed_mock_browser_tools(scope)
        set_worker_runtime_info(WorkerRuntimeInfo(
            manifest=_manifest(browser_contract_hash="worker-is-actually-this"),
            settings=DurableRuntimeSettings(leases_enabled=False),
        ))
        deps = AxisDurableDeps(
            run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False,
            expected_manifest=_manifest(browser_contract_hash="workflow-expected-this"),
        )
        ctx = MagicMock()
        ctx.deps = deps
        result = run_async(durable_agent_module.durable_browser_handler(
            "browser_tabs", ctx, {"operation": "list"},
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "VERSION_INCOMPATIBLE")
        self.assertEqual(client.rpc.call_count, 0)

    def test_matching_manifest_does_not_block_execution(self):
        scope = "scope-version-compatible"
        tools, client = _seed_mock_browser_tools(scope)
        set_worker_runtime_info(WorkerRuntimeInfo(manifest=_manifest(), settings=DurableRuntimeSettings(leases_enabled=False)))
        deps = AxisDurableDeps(run_id="r", job_id="j", browser_scope_key=scope, leases_enabled=False, expected_manifest=_manifest())
        ctx = MagicMock()
        ctx.deps = deps
        result = run_async(durable_agent_module.durable_browser_handler("browser_tabs", ctx, {"operation": "list"}))
        self.assertTrue(result["ok"])


class TestPlanConflictNeverSilentlyDiscarded(unittest.TestCase):
    def test_stale_base_revision_returns_a_retryable_conflict_not_silent_success(self):
        from axis.config import load_axis_config
        from axis.durability.runtime import build_durable_tool_guardrail
        from axis.models import AxisControlState
        from axis.planning import AxisPlanItem

        cfg = load_axis_config()
        guardrail = build_durable_tool_guardrail(cfg, MagicMock())
        deps = AxisDurableDeps(
            run_id="r", job_id="j", browser_scope_key="s",
            control_state=AxisControlState(
                plan=[AxisPlanItem(task_id="t1", content="one", status="pending")], plan_revision=5,
            ),
        )
        ctx = MagicMock()
        ctx.deps = deps
        call = MagicMock()
        call.name = "add_task"
        call.args = {"content": "two"}
        call.tool_call_id = "call-1"
        # A stale write computed against an older base (3) than the current
        # control_state.plan_revision (5).
        call.result = {
            "message": "added", "_axis_plan": [{"task_id": "t1", "content": "one", "status": "pending"}],
            "_axis_plan_base_revision": 3, "_axis_plan_revision": 4,
        }
        verdict = run_async(guardrail.result_guard(ctx, call))
        self.assertEqual(verdict.action, "retry")
        self.assertIn("PLAN_CONFLICT", verdict.message)
        # The stale write must not have silently overwritten the newer plan.
        self.assertEqual(deps.control_state.plan_revision, 5)


class TestAmbiguousRebindCandidateSelection(unittest.TestCase):
    def test_ambiguous_rebind_offers_opaque_candidates_and_accepts_a_validated_selection(self):
        scope = "scope-ambiguous-rebind-selection"
        client = MagicMock(spec=BrowserBridgeClient)
        tabs = [
            {"id": 10, "windowId": 1, "active": True, "url": "https://a", "title": "t"},
            {"id": 11, "windowId": 1, "active": False, "url": "https://a", "title": "t"},
        ]
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": tabs} if method == "tabs.list" else _rpc_success(method, params)
        )
        tools = BrowserAgentTools(client)
        durable_agent_module.clear_browser_cache(scope)
        deps = AxisDurableDeps(
            run_id="r", job_id="job-ambig", browser_scope_key=scope, leases_enabled=False,
            browser_projection=BrowserRecoveryProjection(
                durable_tab_key="tab-stable", last_known_sanitized_url="https://a", last_known_title="t",
            ),
        )

        from axis.durability.activities import (
            BrowserRebindRequest, BrowserRebindSelectionRequest, refresh_browser_binding,
            select_browser_rebind_candidate,
        )

        async def body():
            with patch.object(durable_agent_module, "_new_browser_tools", return_value=tools):
                refreshed = await refresh_browser_binding(BrowserRebindRequest(deps=deps, previous_projection=deps.browser_projection))
            self.assertTrue(refreshed.projection.rebind_required)
            self.assertGreaterEqual(len(refreshed.candidates), 2)
            candidate_ids = {c.candidate_id for c in refreshed.candidates}
            # Never the raw Chrome/bridge tab id itself (int or its str form).
            self.assertNotIn(10, candidate_ids)
            self.assertNotIn(11, candidate_ids)
            self.assertNotIn("10", candidate_ids)
            self.assertNotIn("11", candidate_ids)

            # A stale candidate id (not from this refresh) must not resolve.
            stale_result = await select_browser_rebind_candidate(
                BrowserRebindSelectionRequest(deps=deps, candidate_id="not-a-real-candidate")
            )
            self.assertTrue(stale_result.projection.rebind_required)

            # A validated selection from the latest refresh resolves and
            # bumps binding_generation / invalidates observation / clears
            # rebind_required.
            chosen = refreshed.candidates[0]
            selected = await select_browser_rebind_candidate(
                BrowserRebindSelectionRequest(deps=deps, candidate_id=chosen.candidate_id)
            )
            self.assertFalse(selected.projection.rebind_required)
            self.assertGreater(selected.projection.binding_generation, deps.browser_projection.binding_generation)
            self.assertFalse(selected.projection.fresh_observation)
            self.assertIsNotNone(selected.projection.durable_tab_key)

        run_async(body())

    def test_workflow_signal_rejects_an_unknown_candidate_id(self):
        workflow_obj = AxisJobWorkflow()
        workflow_obj._rebind_candidates = [RebindCandidate(candidate_id="real-one")]
        workflow_obj.select_rebind_candidate(SelectRebindCandidateSignal(candidate_id="forged-id"))
        self.assertFalse(workflow_obj._rebind_requested)
        self.assertIsNone(workflow_obj._rebind_selected_candidate_id)
        workflow_obj.select_rebind_candidate(SelectRebindCandidateSignal(candidate_id="real-one"))
        self.assertTrue(workflow_obj._rebind_requested)
        self.assertEqual(workflow_obj._rebind_selected_candidate_id, "real-one")


class TestContinueAsNewRetainsContext(unittest.TestCase):
    def test_resumed_prompt_carries_original_task_objective_and_answers_not_bare_continue_text(self):
        from axis.durability.models import AxisContinuationState
        from axis.effects import AcceptanceCriterion, ProposedEffect, TaskIntent
        from axis.models import AxisControlState

        workflow_obj = AxisJobWorkflow()
        workflow_obj._deps = AxisDurableDeps(
            run_id="r", job_id="j", browser_scope_key="s",
            control_state=AxisControlState(task_intent=TaskIntent(goal="Update the display name.")),
        )
        job_input = AxisJobInput(
            job_id="j", task_summary="Update the profile display name to Axis POC Demo.",
            browser_scope_key="s",
            continuation=AxisContinuationState(
                control_state=AxisControlState(), browser_projection=BrowserRecoveryProjection(),
                original_task_summary="Update the profile display name to Axis POC Demo.",
                user_answers=["Use the marketing account."],
            ),
        )
        prompt = workflow_obj._build_resumed_prompt(job_input)
        self.assertIn("Update the profile display name to Axis POC Demo.", prompt)
        self.assertIn("Update the display name.", prompt)
        self.assertIn("Use the marketing account.", prompt)
        self.assertNotEqual(prompt.strip(), "Continue the task from the carried durable state.")

    def test_history_limit_continuation_preserves_original_summary_and_recent_answers(self):
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return _final_result_call(info, status="needs_user_input", summary="ask", question="Continue?")
            return _final_result_call(info, status="failed", summary="continued", error_code="AFTER_CONTINUE")

        settings = DurableRuntimeSettings(leases_enabled=False, max_run_segments=1)

        async def body():
            async with _DurableTestHarness(scripted, runtime_settings=settings) as h:
                handle = await h.start_job(scope_key="history-context")
                for _ in range(50):
                    state = await handle.query(AxisJobWorkflow.get_job_state)
                    if state.pending_question:
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
                    request_id=state.pending_question.request_id, answer_nonce="go", text="Proceed with option B.",
                ))
                result = await handle.result()
                self.assertEqual(result.error_code, "AFTER_CONTINUE")

        run_async(body())


class TestBulkPlanStatusUpdateThroughDurableGuardrail(unittest.TestCase):
    def test_update_task_statuses_does_not_crash_the_workflow(self):
        """Regression test: `_DurablePlanningToolset.update_task_statuses`
        previously overrode the tool with a looser `updates: list[Any]`
        signature than the real `list[PlanStatusUpdate]`, which is what the
        framework introspects to build THIS tool's own argument validator
        (Harness registers the bound, overridden method). That left each
        item as a raw unvalidated dict, and axis.agent's arg-stage guard
        (`u.task_id`/`u.status`) crashed the workflow with an unhandled
        AttributeError the first time a durable job actually called
        `update_task_statuses` with a real (non-empty) list — which no
        prior test exercised."""
        scope_key = "scope-bulk-status-update"
        tools, client = _seed_mock_browser_tools(scope_key)
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            n = step["n"]
            if n == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_tabs", args={"operation": "list"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="browser_observe", args={"browserSessionId": _active_tab_handle(messages)},
                )])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [
                    {"id": "t1", "content": "Step one", "status": "in_progress"},
                    {"id": "t2", "content": "Step two", "status": "pending"},
                ]})])
            if n == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_statuses", args={"updates": [
                    {"task_id": "t1", "status": "completed"},
                    {"task_id": "t2", "status": "in_progress"},
                ]})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="observed")

        async def body():
            async with _DurableTestHarness(scripted, leases_enabled=False) as h:
                handle = await h.start_job(scope_key=scope_key)
                result = await handle.result()
                self.assertEqual(result.status, "completed")
                self.assertNotEqual(result.error_code, "UNEXPECTED_ERROR")

        run_async(body())


if __name__ == "__main__":
    unittest.main()
