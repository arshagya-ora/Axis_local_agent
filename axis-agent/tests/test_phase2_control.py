"""Deterministic tests for AXIS Phase 2: Planning, Task Intent, Effects,
Guardrails, and Approvals.

Everything here uses Pydantic AI's `FunctionModel` and a mocked
`BrowserBridgeClient` — no OCI credentials, no Chrome, no live Browser
Agent Bridge (`conftest.py` forces `ALLOW_MODEL_REQUESTS = False` at import
time). Tests are grouped by concern rather than by a numbered checklist.
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict

from pydantic import ValidationError
from pydantic_ai import DeferredToolRequests, ToolApproved, ToolDenied
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.guardrails import ToolGuardrail
from pydantic_ai_harness.planning import InMemoryPlanStore, Planning
from pydantic_ai_harness.system_reminders import SystemReminders

import axis.agent as aa
from axis.approvals import resolve_approval
from axis.capabilities.task_control import build_task_control_capability
from axis.effects import (
    AcceptanceCriterion,
    EffectError,
    EffectLedger,
    ProposedEffect,
    TaskIntent,
    criterion_matches_assertion,
    deterministic_risk_floor,
    max_risk,
    requires_effect,
)
from axis.events import RunEventLogger
from axis.planning import create_task_plan_store

from conftest import bind_tools, final_result_call

_DISPLAY_NAME_CRITERION: Dict[str, Any] = {
    "criterion_id": "c1", "description": "name updated",
    "assertion": {"assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo"},
}


def _intent_args(*, risk: str = "reversible_local", tool: str = "browser_act", actions=("fill",), max_mutations: int = 3, ambiguities=None) -> Dict[str, Any]:
    return {
        "goal": "Update the display name.",
        "constraints": [],
        "ambiguities": ambiguities or [],
        "proposed_effects": [{
            "effect_key": "e1", "summary": "Fill the display name field.", "risk": risk,
            "allowed_tools": [tool], "allowed_actions": list(actions),
            "max_browser_mutations": max_mutations, "acceptance_criterion_ids": ["c1"],
        }],
        "acceptance_criteria": [_DISPLAY_NAME_CRITERION],
    }


def _rpc_success(method, params=None, **_):
    if method == "page.accessibilityTree":
        return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://a", "title": "t", "snapshotId": "snap-1"}
    if method in ("locator.fill", "locator.fillRef", "locator.click", "locator.clickRef", "locator.setInputFiles"):
        return {"whatChanged": None}
    if method == "expect.locator.toHaveValue":
        return {}
    raise AssertionError(f"unexpected rpc: {method} {params}")


class TestContractPreservation(unittest.TestCase):
    """The frozen Phase 1.2 browser contract is untouched by Phase 2."""

    def test_eight_browser_tools_unchanged_and_sequential(self):
        from axis.capabilities.browser import build_browser_capability
        import browser_contract

        cap = build_browser_capability()
        names = [t.name for t in cap.tools]
        self.assertEqual(len(names), 8)
        self.assertEqual(len(set(names)), 8)
        for tool in cap.tools:
            self.assertTrue(tool.sequential)
        committed = browser_contract.load_committed_snapshot("v2")
        by_name = {t.name: t for t in cap.tools}
        for entry in committed["tools"]:
            spec = entry["function"]
            self.assertEqual(by_name[spec["name"]].function_schema.json_schema, spec["parameters"])

    def test_task_control_tools_exactly_once_with_additional_properties_false(self):
        cap = build_task_control_capability(RunEventLogger())
        names = [t.name for t in cap.tools]
        self.assertEqual(names, ["axis_set_task_intent", "axis_prepare_effect"])
        for tool in cap.tools:
            self.assertTrue(tool.sequential)
            schema = tool.function_schema.json_schema
            self.assertFalse(schema.get("additionalProperties", True))
            for prop in schema.get("properties", {}).values():
                if isinstance(prop, dict) and prop.get("type") == "object":
                    self.assertFalse(prop.get("additionalProperties", True))

    def test_agent_capability_composition(self):
        from pydantic_ai.models.test import TestModel

        agent = aa.build_axis_agent(TestModel())
        caps = agent.root_capability.capabilities
        types_by_id = {getattr(c, "id", None): type(c).__name__ for c in caps}
        self.assertEqual(types_by_id.get("axis.browser"), "Capability")
        self.assertEqual(types_by_id.get("axis.task_control"), "Capability")
        self.assertTrue(any(isinstance(c, ToolGuardrail) for c in caps))
        self.assertTrue(any(isinstance(c, SystemReminders) for c in caps))
        # Planning is deliberately NOT agent-level (see axis.agent's module
        # docstring) — it is injected per run segment by run_axis_task.
        self.assertFalse(any(isinstance(c, Planning) for c in caps))

    def test_system_reminders_registered_and_not_llm_backed(self):
        from pydantic_ai_harness.system_reminders import LLMReminder

        reminders = aa._build_system_reminders(5, 4)
        self.assertIsInstance(reminders, SystemReminders)
        self.assertTrue(reminders.reminders)
        self.assertFalse(reminders.dynamic_reminders)
        for reminder in reminders.reminders:
            self.assertNotIsInstance(reminder, LLMReminder)


class TestTaskIntentValidation(unittest.TestCase):
    def test_duplicate_effect_key_rejected(self):
        effect = ProposedEffect(effect_key="e1", summary="s", risk="read", allowed_tools=["browser_observe"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        criterion = AcceptanceCriterion(criterion_id="c1", description="d", assertion={"assertion": "title", "expected": "x"})
        with self.assertRaises(ValidationError):
            TaskIntent(goal="g", proposed_effects=[effect, effect], acceptance_criteria=[criterion])

    def test_duplicate_criterion_id_rejected(self):
        criterion = AcceptanceCriterion(criterion_id="c1", description="d", assertion={"assertion": "title", "expected": "x"})
        with self.assertRaises(ValidationError):
            TaskIntent(goal="g", acceptance_criteria=[criterion, criterion])

    def test_unknown_criterion_reference_rejected(self):
        effect = ProposedEffect(effect_key="e1", summary="s", risk="read", allowed_tools=["browser_observe"], max_browser_mutations=1, acceptance_criterion_ids=["missing"])
        with self.assertRaises(ValidationError):
            TaskIntent(goal="g", proposed_effects=[effect])

    def test_effect_without_required_criterion_rejected(self):
        criterion = AcceptanceCriterion(criterion_id="c1", description="d", required=False, assertion={"assertion": "title", "expected": "x"})
        effect = ProposedEffect(effect_key="e1", summary="s", risk="read", allowed_tools=["browser_observe"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        with self.assertRaises(ValidationError):
            TaskIntent(goal="g", proposed_effects=[effect], acceptance_criteria=[criterion])

    def test_empty_goal_rejected(self):
        with self.assertRaises(ValidationError):
            TaskIntent(goal="")

    def test_unsupported_tool_rejected(self):
        with self.assertRaises(ValidationError):
            ProposedEffect(effect_key="e1", summary="s", risk="read", allowed_tools=["not_a_real_tool"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])

    def test_assertion_missing_locator_rejected(self):
        with self.assertRaises(ValidationError):
            AcceptanceCriterion(criterion_id="c1", description="d", assertion={"assertion": "value", "expected": "x"})

    def test_assertion_with_raw_identifier_rejected(self):
        with self.assertRaises(ValidationError):
            AcceptanceCriterion(criterion_id="c1", description="d", assertion={"assertion": "title", "expected": "x", "tabId": 5})

    def test_revision_blocked_once_effect_started(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="reversible_local", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        self.assertFalse(ledger.has_started_or_awaiting())  # merely "prepared" does not block revision
        ledger.mark_started(record.effect_id)
        self.assertTrue(ledger.has_started_or_awaiting())


class TestEffectPreparation(unittest.TestCase):
    def test_cannot_prepare_second_effect_while_one_active(self):
        ledger = EffectLedger()
        ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        with self.assertRaises(EffectError):
            ledger.prepare(effect_key="e2", plan_task_id="t2", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])

    def test_cannot_bind_two_different_effect_keys_to_same_task(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        ledger.mark_executed_unverified(record.effect_id, "tab-1")
        ledger.mark_succeeded(record.effect_id)
        with self.assertRaises(EffectError):
            ledger.prepare(effect_key="e2", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])

    def test_denied_effect_cannot_be_reprepared(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_denied(record.effect_id)
        with self.assertRaises(EffectError):
            ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])

    def test_failed_effect_within_budget_can_be_retried_without_reprepare(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=3, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        updated = ledger.mark_failed(record.effect_id)
        self.assertEqual(updated.status, "prepared")  # retryable — budget not exhausted

    def test_failed_effect_at_budget_becomes_terminal(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        updated = ledger.mark_failed(record.effect_id)
        self.assertEqual(updated.status, "failed")


class TestRiskClassification(unittest.TestCase):
    def test_read_and_control_are_not_effect_gated(self):
        self.assertFalse(requires_effect("browser_observe"))
        self.assertFalse(requires_effect("browser_navigate"))
        self.assertFalse(requires_effect("browser_act", action="hover"))
        self.assertFalse(requires_effect("browser_act", action="scroll"))
        self.assertFalse(requires_effect("browser_tabs", operation="list"))
        self.assertFalse(requires_effect("browser_tabs", operation="activate"))

    def test_mutations_and_tab_close_are_effect_gated(self):
        for action in ("click", "fill", "press", "select", "check", "uncheck", "upload", "drag"):
            self.assertTrue(requires_effect("browser_act", action=action), action)
        self.assertTrue(requires_effect("browser_tabs", operation="close"))

    def test_model_cannot_lower_deterministic_floor(self):
        floor = deterministic_risk_floor("browser_act", action="upload")
        self.assertEqual(floor, "external_effect")
        self.assertEqual(max_risk(floor, "read"), "external_effect")  # a lower model-proposed risk never wins

    def test_runtime_can_raise_model_proposed_risk(self):
        self.assertEqual(max_risk("reversible_local", "destructive_high_impact"), "destructive_high_impact")


class TestGuardrailGating(unittest.TestCase):
    def test_mutation_without_task_intent_is_blocked_before_handler(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x", "locator": {"selector": "#x"},
                })])
            return final_result_call(info, status="failed", summary="blocked", error_code="TASK_INTENT_REQUIRED")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        self.assertNotIn("locator.fill", [c.args[0] for c in client.rpc.call_args_list])

    def test_mutation_with_intent_but_no_plan_is_blocked(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x", "locator": {"selector": "#x"},
                })])
            return final_result_call(info, status="failed", summary="blocked", error_code="PLAN_REQUIRED")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        self.assertNotIn("locator.fill", [c.args[0] for c in client.rpc.call_args_list])

    def test_mutation_without_prepared_effect_is_blocked(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x", "locator": {"selector": "#x"},
                })])
            return final_result_call(info, status="failed", summary="blocked", error_code="EFFECT_REQUIRED")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        self.assertNotIn("locator.fill", [c.args[0] for c in client.rpc.call_args_list])

    def test_prepared_effect_only_permits_declared_action(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(actions=["fill"]))])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if step["n"] == 4:
                # click is not in the prepared effect's allowed_actions (["fill"])
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#x"},
                })])
            return final_result_call(info, status="failed", summary="blocked", error_code="EFFECT_MISMATCH")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        self.assertNotIn("locator.click", [c.args[0] for c in client.rpc.call_args_list])
        rejected = deps.effect_ledger.get_for_task("t1")
        self.assertIsNotNone(rejected)
        self.assertEqual(rejected.outcome, "known_not_applied")
        self.assertEqual(rejected.execution_stage, "validated")

    def test_mutation_budget_enforced(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args(max_mutations=1))])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if step["n"] in (4, 5):
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x", "locator": {"selector": "#display-name"},
                })])
            return final_result_call(info, status="failed", summary="budget", error_code="EFFECT_LIMIT_EXCEEDED")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        fill_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.fill"]
        self.assertEqual(len(fill_calls), 1)  # the second attempt never reached the handler

    def test_read_calls_pass_without_any_task_intent(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            return final_result_call(info, status="completed", summary="ok", verification_summary="observed")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _h = asyncio.run(aa.run_axis_task(agent, deps, "Look."))
        self.assertEqual(result.status, "completed")


class TestApprovalFlow(unittest.TestCase):
    def _upload_intent(self):
        return _intent_args(risk="external_effect", tool="browser_act", actions=["upload"])

    def test_external_effect_defers_then_denial_prevents_execution(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=self._upload_intent())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if step["n"] == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "upload",
                    "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            self.fail("model should not be re-invoked before the deferred call is resolved")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        output, history = asyncio.run(aa.run_axis_task(agent, deps, "Upload the file."))

        self.assertIsInstance(output, DeferredToolRequests)
        self.assertEqual(len(output.approvals), 1)
        self.assertNotIn("locator.setInputFiles", [c.args[0] for c in client.rpc.call_args_list])
        self.assertEqual(deps.effect_ledger.get_active().status, "awaiting_approval")

        results = resolve_approval(output, {"upload-1": False}, deps.approval_state, deps.effect_ledger, RunEventLogger(), deps.run_id)
        self.assertIsInstance(results.approvals["upload-1"], ToolDenied)
        self.assertEqual(deps.effect_ledger.get_active(), None)
        self.assertEqual(deps.effect_ledger.list_effects()[-1].status, "denied")

        def after_denial(messages, info):
            return final_result_call(info, status="failed", summary="denied", error_code="APPROVAL_DENIED")

        agent2 = aa.build_axis_agent(FunctionModel(after_denial))
        final_output, _h = asyncio.run(aa.run_axis_task(
            agent2, deps, None, message_history=history, deferred_tool_results=results,
        ))
        self.assertEqual(final_output.status, "failed")
        self.assertNotIn("locator.setInputFiles", [c.args[0] for c in client.rpc.call_args_list])

    def test_approved_call_executes_exactly_once(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=self._upload_intent())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Upload", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if step["n"] == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "upload",
                    "locator": {"selector": "#file"}, "files": ["a.txt"],
                }, tool_call_id="upload-1")])
            self.fail("unexpected extra model turn before resume")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        output, history = asyncio.run(aa.run_axis_task(agent, deps, "Upload the file."))
        self.assertIsInstance(output, DeferredToolRequests)

        results = resolve_approval(output, {"upload-1": True}, deps.approval_state, deps.effect_ledger, RunEventLogger(), deps.run_id)
        self.assertIsInstance(results.approvals["upload-1"], ToolApproved)

        def after_approval(messages, info):
            return final_result_call(info, status="failed", summary="stop", error_code="ACCEPTANCE_UNRESOLVED")

        agent2 = aa.build_axis_agent(FunctionModel(after_approval))
        asyncio.run(aa.run_axis_task(agent2, deps, None, message_history=history, deferred_tool_results=results))

        upload_calls = [c for c in client.rpc.call_args_list if c.args[0] == "locator.setInputFiles"]
        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(deps.effect_ledger.get_active().status, "executed_unverified")


class TestAcceptanceAndCompletion(unittest.TestCase):
    def test_matching_assertion_satisfies_criterion_and_full_trajectory_completes(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="write_plan", args={"items": [{"id": "t1", "content": "Fill name", "status": "in_progress"}]})])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_prepare_effect", args={"effectKey": "e1", "planTaskId": "t1"})])
            if step["n"] == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "Axis POC Demo",
                    "locator": {"selector": "#display-name"},
                })])
            if step["n"] == 5:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
                })])
            if step["n"] == 6:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return final_result_call(info, status="completed", summary="done", verification_summary="asserted")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _h = asyncio.run(aa.run_axis_task(agent, deps, "Fill and confirm."))
        self.assertEqual(result.status, "completed")
        self.assertEqual(deps.effect_ledger.list_effects()[-1].status, "succeeded")

    def test_nonmatching_assertion_does_not_satisfy(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="reversible_local", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        ledger.mark_executed_unverified(record.effect_id, "tab-a")
        criterion = AcceptanceCriterion(**_DISPLAY_NAME_CRITERION)
        wrong_call = {"assertion": "value", "locator": {"selector": "#other-field"}, "expected": "Axis POC Demo", "browserSessionId": "tab-a"}
        self.assertFalse(criterion_matches_assertion(criterion, wrong_call, effect=ledger.get(record.effect_id), trusted_session_id="tab-a"))

    def test_wrong_tab_does_not_satisfy(self):
        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="reversible_local", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        ledger.mark_executed_unverified(record.effect_id, "tab-a")
        criterion = AcceptanceCriterion(**_DISPLAY_NAME_CRITERION)
        call_args = {"assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo"}
        self.assertTrue(criterion_matches_assertion(criterion, call_args, effect=ledger.get(record.effect_id), trusted_session_id="tab-a"))
        self.assertFalse(criterion_matches_assertion(criterion, call_args, effect=ledger.get(record.effect_id), trusted_session_id="tab-b"))

    def test_unresolved_proposed_effect_blocks_completion_even_with_no_ledger_record(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = _rpc_success
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="axis_set_task_intent", args=_intent_args())])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            # Never prepares/executes the proposed effect at all.
            return final_result_call(info, status="completed", summary="done", verification_summary="observed")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _h = asyncio.run(aa.run_axis_task(agent, deps, "Fill it."))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "MODEL_ERROR")

    def test_wait_and_observe_do_not_satisfy_acceptance(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {} if method in ("page.waitForCondition",) else _rpc_success(method, params)
        )
        # Direct ledger check: neither browser_observe nor browser_wait is
        # ever routed through _check_acceptance (only browser_assert is).
        from axis.capabilities.browser import _check_acceptance

        ledger = EffectLedger()
        record = ledger.prepare(effect_key="e1", plan_task_id="t1", risk="reversible_local", summary="s", allowed_tools=["browser_act"], allowed_actions=["fill"], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        ledger.mark_started(record.effect_id)
        ledger.mark_executed_unverified(record.effect_id, "tab-a")
        deps.effect_ledger = ledger
        deps.task_intent = TaskIntent(goal="g", proposed_effects=[ProposedEffect(
            effect_key="e1", summary="s", risk="reversible_local", allowed_tools=["browser_act"], allowed_actions=["fill"],
            max_browser_mutations=1, acceptance_criterion_ids=["c1"],
        )], acceptance_criteria=[AcceptanceCriterion(**_DISPLAY_NAME_CRITERION)])
        _check_acceptance({"browserSessionId": "tab-a"}, {"ok": True, "browserSessionId": "tab-a", "data": {}}, deps, RunEventLogger())
        self.assertEqual(ledger.get(record.effect_id).status, "executed_unverified")  # unchanged — no criterion matched


class TestIsolationAcrossTasks(unittest.TestCase):
    def test_next_run_gives_fresh_intent_ledger_and_plan_store(self):
        deps, _client, _tools = bind_tools()
        deps.task_intent = TaskIntent(goal="g")
        deps.effect_ledger.prepare(effect_key="e1", plan_task_id="t1", risk="read", summary="s", allowed_tools=["browser_observe"], allowed_actions=[], max_browser_mutations=1, acceptance_criterion_ids=["c1"])
        old_store = deps.plan_store

        fresh = aa.next_run(deps)
        self.assertIsNone(fresh.task_intent)
        self.assertEqual(fresh.effect_ledger.list_effects(), [])
        self.assertIsNot(fresh.plan_store, old_store)
        self.assertIsNot(fresh.effect_ledger, deps.effect_ledger)
        self.assertIsNot(fresh.approval_state, deps.approval_state)

    def test_each_task_plan_store_is_independent(self):
        logger = RunEventLogger()
        store1 = create_task_plan_store(logger, "run-1")
        store2 = create_task_plan_store(logger, "run-2")
        self.assertIsNot(store1, store2)
        self.assertIsInstance(store1, InMemoryPlanStore)


if __name__ == "__main__":
    unittest.main()
