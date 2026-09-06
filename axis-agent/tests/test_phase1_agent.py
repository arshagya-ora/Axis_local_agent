"""Deterministic tests for the Axis Phase 1 root agent (post-repair).

Everything here uses Pydantic AI's `FunctionModel`/`TestModel` and a mocked
`BrowserBridgeClient` — no OCI credentials, no Chrome, no live Browser
Agent Bridge. `pydantic_ai.models.ALLOW_MODEL_REQUESTS` is forced off at
import time so an accidentally-real model can never be reached from here.

Does not duplicate `tests/test_browser_agent_tools.py`'s 114 tests or
`tests/test_phase0_baseline.py`'s 30 tests; this file only proves the
axis/ agent layer built on top of them.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

AGENT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = AGENT_DIR / "scripts"
for path in (AGENT_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pydantic_ai.models as pai_models  # noqa: E402

pai_models.ALLOW_MODEL_REQUESTS = False  # block any accidental real provider call

from pydantic_ai import RunContext  # noqa: E402
from pydantic_ai.capabilities import Capability  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

import axis.agent as aa  # noqa: E402
import browser_agent_tools as bat  # noqa: E402
import browser_contract  # noqa: E402
from axis.capabilities.browser import build_browser_capability  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402
from axis.models import AxisRunDeps, AxisRunLimits, AxisRunLimitsError, AxisRunState, AxisTaskResult  # noqa: E402
from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError  # noqa: E402

EXPECTED_TOOL_NAMES = (
    "browser_observe",
    "browser_act",
    "browser_navigate",
    "browser_wait",
    "browser_assert",
    "browser_capture_evidence",
    "browser_diagnose",
    "browser_tabs",
)

FORBIDDEN_INPUT_PROPERTY_NAMES = frozenset({
    "tabId", "windowId", "groupId", "frameId", "bridgeSessionId",
    "bridgeMethod", "method", "rpcMethod", "snapshotId",
})

def bind_tools(tab_id: int = 456, url: str = "https://application.example.com/profile", title: str = "Profile"):
    """Create a fresh BrowserAgentTools + trusted AxisRunDeps with one
    registered tab (the whole-browser fast path — see
    ``browser_agent_tools.BrowserAgentTools.refresh_tabs``). Each call
    returns its own BrowserAgentTools instance (its own tab registry and
    observations dict). ``deps.initial_tab_handle`` is the one tab's handle."""
    client = MagicMock(spec=BrowserBridgeClient)
    client.rpc.side_effect = lambda method, params=None, **_: (
        {"tabs": [{"id": tab_id, "windowId": 1, "active": True, "url": url, "title": title}]} if method == "tabs.list"
        else (_ for _ in ()).throw(AssertionError(f"unexpected rpc call during bind: {method}"))
    )
    tools = bat.BrowserAgentTools(client)
    deps, error = aa.bind_axis_run_deps(tools)
    assert deps is not None, error
    return deps, client, tools


def _last_tool_result(messages: List[ModelMessage], tool_name: str) -> Optional[Dict[str, Any]]:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                return part.content
    return None


def _final_result_call(info: AgentInfo, **kwargs: Any) -> ModelResponse:
    name = info.output_tools[0].name
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=kwargs)])


def _grant_effect(
    deps: AxisRunDeps, *, tool: str, actions: Optional[List[str]] = None,
    assertion: Optional[Dict[str, Any]] = None, max_mutations: int = 10, plan_task_id: str = "t1",
) -> None:
    """Test helper (Phase 2): directly seed `deps` with a valid, already
    in_progress plan step and a prepared effect authorizing `tool`/`actions`
    — bypassing the model-facing `axis_set_task_intent`/`write_plan`/
    `axis_prepare_effect` tool calls. These Phase 1.2 tests exercise other
    concerns (stale refs, navigation invalidation, run limits, safe
    failures, session enforcement); Phase 2's own gating/approval/
    acceptance behavior gets its dedicated coverage in
    `test_phase2_control.py`. `assertion` — when the test's script goes on
    to call `browser_assert` and expects a legitimate "completed" — must
    match that call's args exactly (minus `browserSessionId`) so the
    effect's one acceptance criterion actually resolves."""
    from pydantic_ai_harness.planning import PlanItem, TaskStatus

    from axis.effects import AcceptanceCriterion, ProposedEffect, TaskIntent

    criteria = [AcceptanceCriterion(criterion_id="c1", description="d", assertion=assertion)] if assertion else [
        AcceptanceCriterion(criterion_id="c1", description="d", assertion={"assertion": "visible", "locator": {"selector": "#unused"}})
    ]
    proposed = ProposedEffect(
        effect_key="e1", summary="s", risk="reversible_local", allowed_tools=[tool],
        allowed_actions=actions or [], max_browser_mutations=max_mutations, acceptance_criterion_ids=["c1"],
    )
    deps.task_intent = TaskIntent(goal="g", proposed_effects=[proposed], acceptance_criteria=criteria)
    asyncio.run(deps.plan_store.add_item(PlanItem(id=plan_task_id, content="c", status=TaskStatus.in_progress)))
    deps.effect_ledger.prepare(
        effect_key="e1", plan_task_id=plan_task_id, risk="reversible_local", summary="s",
        allowed_tools=[tool], allowed_actions=actions or [], max_browser_mutations=max_mutations,
        acceptance_criterion_ids=["c1"],
    )


# ---------------------------------------------------------------------------
# 1-8: the browser Capability itself
# ---------------------------------------------------------------------------

class TestBrowserCapability(unittest.TestCase):
    def test_returned_object_is_a_real_pydantic_ai_capability(self):
        cap = build_browser_capability()
        self.assertIsInstance(cap, Capability)
        self.assertEqual(cap.id, "axis.browser")
        self.assertTrue(cap.description)
        self.assertEqual(cap.get_instructions(), [bat.BROWSER_AGENT_INSTRUCTIONS])

    def test_agent_receives_capability_via_capabilities_param(self):
        from pydantic_ai.models.test import TestModel
        agent = aa.build_axis_agent(TestModel())
        # root_capability.capabilities is the list of top-level capabilities.
        # Pydantic AI auto-injects two dormant infrastructure capabilities
        # into every agent regardless of what's passed to capabilities=
        # (ToolSearch — "zero overhead when no deferred tools exist", and
        # PendingMessageDrainCapability); neither contributes a model-facing
        # tool or instruction here since none of our tools use
        # defer_loading=True. The structural proof that the browser tools
        # (and Phase 2's task-control/guardrail/reminders capabilities)
        # arrived through capabilities=, not tools=/toolsets=/@agent.tool,
        # is that exactly this set of *user* capabilities is present — no
        # more, no fewer. (`Planning` is intentionally NOT agent-level; it
        # is passed per run segment by `run_axis_task` — see axis.agent's
        # module docstring.)
        registered = agent.root_capability.capabilities
        ids = [c.id for c in registered]
        self.assertIn("axis.browser", ids)
        self.assertIn("axis.task_control", ids)
        non_infrastructure = [c for c in registered if type(c).__name__ not in ("ToolSearch", "PendingMessageDrainCapability")]
        non_infrastructure_types = {type(c).__name__ for c in non_infrastructure}
        self.assertEqual(non_infrastructure_types, {"Capability", "ToolGuardrail", "SystemReminders"})
        self.assertEqual(len(non_infrastructure), 4)  # axis.browser, axis.task_control, ToolGuardrail, SystemReminders

    def test_exactly_eight_browser_tools_registered_once(self):
        cap = build_browser_capability()
        names = [t.name for t in cap.tools]
        self.assertEqual(len(names), 8)
        self.assertEqual(tuple(names), EXPECTED_TOOL_NAMES)
        self.assertEqual(len(set(names)), 8)  # no duplicates

    def test_all_eight_tools_are_sequential(self):
        cap = build_browser_capability()
        for tool in cap.tools:
            self.assertTrue(tool.sequential, f"{tool.name} is not sequential")

    def test_tool_schemas_match_committed_v2_snapshot(self):
        committed = browser_contract.load_committed_snapshot("v2")
        self.assertIsNotNone(committed, "Phase 0 contract snapshot must exist")
        self.assertEqual(bat.get_tool_definitions(), committed["tools"])
        self.assertTrue(browser_contract.verify())

        cap = build_browser_capability()
        by_name = {t.name: t for t in cap.tools}
        for entry in committed["tools"]:
            spec = entry["function"]
            tool = by_name[spec["name"]]
            self.assertEqual(tool.description, spec["description"])
            self.assertEqual(tool.function_schema.json_schema, spec["parameters"])

    def test_no_raw_rpc_method_or_chrome_identifiers_visible(self):
        def iter_props(schema):
            if not isinstance(schema, dict):
                return
            for name, sub in (schema.get("properties") or {}).items():
                yield name
                yield from iter_props(sub)
            items = schema.get("items")
            if isinstance(items, dict):
                yield from iter_props(items)

        for entry in bat.get_tool_definitions():
            props = set(iter_props(entry["function"]["parameters"]))
            leaked = props & (FORBIDDEN_INPUT_PROPERTY_NAMES | {"method", "bridgeMethod", "rpcMethod"})
            self.assertFalse(leaked, f"{entry['function']['name']} leaks: {leaked}")

    def test_output_mechanism_exposes_one_result_schema(self):
        from pydantic_ai.models.function import FunctionModel

        seen: Dict[str, Any] = {}

        def probe(messages, info):
            seen["output_tools"] = [t.name for t in info.output_tools]
            seen["function_tools"] = [t.name for t in info.function_tools]
            return _final_result_call(info, status="cancelled", summary="stop")

        deps, _client, _tools = bind_tools()
        agent = aa.build_axis_agent(FunctionModel(probe))
        asyncio.run(agent.run("hi", deps=deps))

        # Exactly one *model-facing* output tool ("final_result", built from
        # AxisTaskResult) — DeferredToolRequests is framework control output,
        # synthesized directly and never offered to the model as a tool, so
        # adding it to output_type does not create a second output tool.
        self.assertEqual(len(seen["output_tools"]), 1)
        # 8 browser tools + 2 Phase 2 task-control tools. Planning's tools
        # are absent here because Planning is deliberately not agent-level
        # (see axis.agent's module docstring) and this test calls
        # agent.run() directly without passing capabilities=[...].
        self.assertEqual(len(seen["function_tools"]), 10)


# ---------------------------------------------------------------------------
# 9-10: trusted session enforcement and sequential execution
# ---------------------------------------------------------------------------

class TestSessionEnforcementAndSequencing(unittest.TestCase):
    def test_model_can_operate_on_any_tab_registered_in_the_trusted_registry(self):
        # Phase 1.2 product decision: AXIS has whole-browser access, so a
        # model-supplied handle for a DIFFERENT, still-registered tab is
        # legitimate and must be honored — there is no single fixed session
        # to force every call onto anymore (see axis.capabilities.browser's
        # module docstring). Only fabricated/closed handles are rejected
        # (see TestMultiTabTrustedRegistry below).
        deps, client, tools = bind_tools(tab_id=456)
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [
                {"id": 456, "windowId": 1, "active": False, "url": "https://a.example/", "title": "A"},
                {"id": 789, "windowId": 1, "active": True, "url": "https://b.example/", "title": "B"},
            ]} if method == "tabs.list" else (_ for _ in ()).throw(AssertionError(method))
        )
        refreshed = tools.refresh_tabs()
        self.assertTrue(refreshed["ok"])
        other_handle = next(t["browserSessionId"] for t in refreshed["data"]["tabs"] if t["browserSessionId"] != deps.initial_tab_handle)

        cap = build_browser_capability()
        observe_tool = next(t for t in cap.tools if t.name == "browser_observe")
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://b.example/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(f"unexpected rpc: {method}"))
        )
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        result = observe_tool.function(ctx, browserSessionId=other_handle)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["browserSessionId"], other_handle)
        tab_id_used = client.rpc.call_args[0][1]["tabId"]
        self.assertEqual(tab_id_used, tools.tab_registry[other_handle]["tabId"])

    def test_two_browser_calls_in_one_turn_execute_sequentially_not_overlapping(self):
        deps, client, _tools = bind_tools()
        active = {"n": 0}
        overlap_detected = {"value": False}

        def rpc(method, params=None, **_):
            if method == "page.accessibilityTree":
                active["n"] += 1
                if active["n"] > 1:
                    overlap_detected["value"] = True
                try:
                    return {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"}
                finally:
                    active["n"] -= 1
            raise AssertionError(method)

        client.rpc.side_effect = rpc

        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                # Two browser_observe calls requested in the SAME model response.
                return ModelResponse(parts=[
                    ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle}, tool_call_id="call-1"),
                    ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle}, tool_call_id="call-2"),
                ])
            return _final_result_call(info, status="cancelled", summary="done")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        asyncio.run(agent.run("go", deps=deps))
        self.assertFalse(overlap_detected["value"], "sequential=True tools must not overlap")


# ---------------------------------------------------------------------------
# 11-14: trajectories and completion verification
# ---------------------------------------------------------------------------

class TestTrajectoriesAndVerification(unittest.TestCase):
    def _rpc(self, method, params=None, **_):
        params = params or {}
        if method == "page.accessibilityTree":
            return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://application.example.com/profile", "title": "Profile", "snapshotId": "snap-1"}
        if method == "locator.fillRef":
            return {"whatChanged": None}
        if method == "expect.locator.toHaveValue":
            return {}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    def test_full_success_trajectory_observe_act_assert_completed(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        session_id = deps.initial_tab_handle
        _grant_effect(deps, tool="browser_act", actions=["fill"], assertion={
            "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
        })
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": session_id})])
            if step["n"] == 2:
                obs = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": session_id, "action": "fill", "value": "Axis POC Demo",
                    "observationId": obs["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": session_id, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
                })])
            if step["n"] == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return _final_result_call(info, status="completed", summary="Display name updated.", verification_summary="browser_assert confirmed the new value.")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Change the display name and confirm it."))

        self.assertEqual(result.status, "completed")
        self.assertEqual(step["n"], 5)

    def test_state_changing_completion_without_assertion_is_rejected(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"whatChanged": None} if method == "locator.click" else self._rpc(method, params)
        )
        _grant_effect(deps, tool="browser_act", actions=["click"])
        calls = {"n": 0}

        def scripted(messages, info):
            calls["n"] += 1
            if calls["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            return _final_result_call(info, status="completed", summary="done", verification_summary="trust me")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Click submit and report done."))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.MODEL_ERROR)
        self.assertEqual(calls["n"], 1 + 1 + aa.DEFAULT_RETRIES)

    def test_state_changing_completion_with_failed_assertion_is_rejected(self):
        deps, client, _tools = bind_tools()

        def rpc(method, params=None, **_):
            if method == "locator.click":
                return {"whatChanged": None}
            if method == "expect.locator.toBeVisible":
                raise BrowserBridgeError("timed out waiting for locator to be visible")
            return self._rpc(method, params)

        client.rpc.side_effect = rpc
        _grant_effect(deps, tool="browser_act", actions=["click"])
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "visible", "locator": {"selector": "#success"},
                })])
            return _final_result_call(info, status="completed", summary="done", verification_summary="confirmed")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Click submit and confirm."))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.MODEL_ERROR)

    def test_state_changing_completion_with_passing_assertion_is_accepted(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"whatChanged": None} if method == "locator.click" else self._rpc(method, params)
        )
        _grant_effect(deps, tool="browser_act", actions=["click"], assertion={
            "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
        })
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "Axis POC Demo",
                })])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="confirmed")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Click submit and confirm."))

        self.assertEqual(result.status, "completed")

    def test_read_only_task_may_complete_after_observe_alone(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            return _final_result_call(info, status="completed", summary="Read the page.", verification_summary="Observed the current page state.")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "What does the page say?"))

        self.assertEqual(result.status, "completed")

    def test_observe_fill_observe_completed_is_rejected(self):
        # Re-observing after an unverified state-changing action must NOT
        # count as proof — verification_required stays set until a passing
        # browser_assert, regardless of any browser_observe in between.
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        _grant_effect(deps, tool="browser_act", actions=["fill"])
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            if step["n"] == 2:
                obs = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x",
                    "observationId": obs["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="I looked at it again")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Fill it in."))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.MODEL_ERROR)

    def test_verification_does_not_carry_across_tasks(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"whatChanged": None} if method == "locator.click" else self._rpc(method, params)
        )

        _grant_effect(deps, tool="browser_act", actions=["click"], assertion={
            "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "x",
        })

        # Task 1: act + passing assert -> legitimately verified and completed.
        step1 = {"n": 0}

        def scripted1(messages, info):
            step1["n"] += 1
            if step1["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            if step1["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "x",
                })])
            if step1["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="confirmed")

        agent1 = aa.build_axis_agent(FunctionModel(scripted1))
        result1, _h = asyncio.run(aa.run_axis_task(agent1, deps, "Do the first thing."))
        self.assertEqual(result1.status, "completed")

        # Task 2: a FRESH run (aa.next_run) that only acts, never asserts —
        # must NOT inherit task 1's assertion_passed_after_effect.
        run2_deps = aa.next_run(deps)
        self.assertFalse(run2_deps.run_state.assertion_passed_after_effect)
        self.assertIsNot(run2_deps.run_state, deps.run_state)
        self.assertIsNone(run2_deps.task_intent)
        _grant_effect(run2_deps, tool="browser_act", actions=["click"])

        step2 = {"n": 0}

        def scripted2(messages, info):
            step2["n"] += 1
            if step2["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            return _final_result_call(info, status="completed", summary="done", verification_summary="trust me")

        agent2 = aa.build_axis_agent(FunctionModel(scripted2))
        result2, _h2 = asyncio.run(aa.run_axis_task(agent2, run2_deps, "Do the second thing."))
        self.assertEqual(result2.status, "failed")
        self.assertEqual(result2.error_code, aa.MODEL_ERROR)


# ---------------------------------------------------------------------------
# 15: stale ref -> re-observe, never blindly retried
# ---------------------------------------------------------------------------

class TestStaleRefHandling(unittest.TestCase):
    def test_stale_ref_forces_reobservation_and_is_never_retried(self):
        deps, client, _tools = bind_tools()
        _grant_effect(deps, tool="browser_act", actions=["fill"], assertion={
            "assertion": "value", "locator": {"selector": "#display-name"}, "expected": "x",
        })
        observe_calls = {"n": 0}
        fill_calls_by_snapshot: Dict[str, int] = {}

        def rpc(method, params=None, **_):
            params = params or {}
            if method == "page.accessibilityTree":
                observe_calls["n"] += 1
                snapshot_id = "snap-1" if observe_calls["n"] == 1 else "snap-2"
                return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://application.example.com/profile", "title": "Profile", "snapshotId": snapshot_id}
            if method == "locator.fillRef":
                snap = params.get("snapshotId")
                fill_calls_by_snapshot[snap] = fill_calls_by_snapshot.get(snap, 0) + 1
                if snap == "snap-1":
                    raise BrowserBridgeError("Stale accessibility ref snapshot: snap-1")
                return {"whatChanged": None}
            if method == "expect.locator.toHaveValue":
                return {}
            raise AssertionError(f"unexpected rpc: {method} {params}")

        client.rpc.side_effect = rpc
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            if step["n"] == 2:
                obs = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x",
                    "observationId": obs["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 3:
                act_result = _last_tool_result(messages, "browser_act")
                self.assertFalse(act_result["ok"])
                self.assertEqual(act_result["error"]["code"], bat.STALE_OBSERVATION)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            if step["n"] == 4:
                obs2 = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "fill", "value": "x",
                    "observationId": obs2["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 5:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "x",
                })])
            if step["n"] == 6:
                return ModelResponse(parts=[ToolCallPart(tool_name="update_task_status", args={"task_id": "t1", "status": "completed"})])
            return _final_result_call(info, status="completed", summary="ok", verification_summary="confirmed")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Fill and confirm."))

        self.assertEqual(result.status, "completed")
        self.assertEqual(observe_calls["n"], 2)
        self.assertEqual(fill_calls_by_snapshot, {"snap-1": 1, "snap-2": 1})


# ---------------------------------------------------------------------------
# 16-17: safe, controlled failures
# ---------------------------------------------------------------------------

class TestSafeFailures(unittest.TestCase):
    def test_handler_exception_becomes_safe_structured_failure(self):
        deps, _client, tools = bind_tools()
        tools.browser_observe = MagicMock(side_effect=RuntimeError("boom with fake_api_key=sk-should-not-leak"))
        cap = build_browser_capability()
        observe_tool = next(t for t in cap.tools if t.name == "browser_observe")
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        result = observe_tool.function(ctx, browserSessionId=deps.initial_tab_handle)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TOOL_EXECUTION_ERROR")
        self.assertEqual(result["error"]["message"], "The browser tool failed unexpectedly.")
        self.assertNotIn("boom", result["error"]["message"])
        self.assertNotIn("sk-should-not-leak", result["error"]["message"])

    def test_exception_text_with_a_fake_secret_is_never_logged(self):
        deps, _client, tools = bind_tools()
        tools.browser_observe = MagicMock(side_effect=RuntimeError("leaked secret=sk-FAKESECRET123"))
        cap = build_browser_capability()
        observe_tool = next(t for t in cap.tools if t.name == "browser_observe")
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        with self.assertLogs("axis.errors", level="ERROR") as captured:
            result = observe_tool.function(ctx, browserSessionId=deps.initial_tab_handle)
        joined = "\n".join(captured.output)
        self.assertNotIn("sk-FAKESECRET123", joined)
        self.assertFalse(result["ok"])

    def test_invalid_tool_arguments_return_a_controlled_typed_failure(self):
        deps, _client, _tools = bind_tools()
        cap = build_browser_capability()
        act_tool = next(t for t in cap.tools if t.name == "browser_act")
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        result = act_tool.function(ctx, browserSessionId=deps.initial_tab_handle, action="fill", locator={"selector": "#x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)


# ---------------------------------------------------------------------------
# 18-20: execution limits
# ---------------------------------------------------------------------------

class TestRunLimits(unittest.TestCase):
    def test_tool_calls_limit_stops_the_run_cleanly(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])

        agent = aa.build_axis_agent(FunctionModel(always_observe))
        limits = AxisRunLimits(max_tool_calls=1, max_requests=10, max_total_tokens=None, max_wall_clock_seconds=5)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "loop forever", limits=limits))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.TOOL_CALL_LIMIT_EXCEEDED)
        self.assertTrue(result.retryable)

    def test_request_limit_reports_the_precise_exhausted_limit(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])

        agent = aa.build_axis_agent(FunctionModel(always_observe))
        limits = AxisRunLimits(max_tool_calls=50, max_requests=1, max_total_tokens=None, max_wall_clock_seconds=5)
        sink = []
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "loop forever", limits=limits, event_logger=RunEventLogger(sink=sink)))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.REQUEST_LIMIT_EXCEEDED)
        limit_events = [e for e in sink if e["eventType"] == "limit_reached"]
        self.assertEqual(len(limit_events), 1)
        self.assertEqual(limit_events[0]["data"]["kind"], "requests")
        self.assertEqual(limit_events[0]["data"]["limit"], 1)

    def test_wall_clock_timeout_returns_a_typed_failure(self):
        deps, _client, _tools = bind_tools()

        async def slow(messages, info):
            await asyncio.sleep(2)
            return _final_result_call(info, status="cancelled", summary="x")

        agent = aa.build_axis_agent(FunctionModel(slow))
        limits = AxisRunLimits(max_wall_clock_seconds=0.05)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "be slow", limits=limits))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.WALL_CLOCK_LIMIT_EXCEEDED)
        self.assertTrue(result.retryable)

    def test_invalid_limit_values_raise_a_controlled_configuration_error(self):
        with self.assertRaises(AxisRunLimitsError):
            AxisRunLimits(max_tool_calls=0)
        with self.assertRaises(AxisRunLimitsError):
            AxisRunLimits(max_requests=-1)
        with self.assertRaises(AxisRunLimitsError):
            AxisRunLimits(max_wall_clock_seconds=0)
        with self.assertRaises(AxisRunLimitsError):
            AxisRunLimits(max_total_tokens=0)

    def test_invalid_environment_limit_is_a_controlled_error_not_a_traceback(self):
        backup = os.environ.get("AXIS_MAX_TOOL_CALLS")
        try:
            os.environ["AXIS_MAX_TOOL_CALLS"] = "not-a-number"
            with self.assertRaises(AxisRunLimitsError):
                AxisRunLimits.from_env()
        finally:
            if backup is None:
                os.environ.pop("AXIS_MAX_TOOL_CALLS", None)
            else:
                os.environ["AXIS_MAX_TOOL_CALLS"] = backup


# ---------------------------------------------------------------------------
# 21: rebinding clears history (exercised at the deps/state level)
# ---------------------------------------------------------------------------

class TestRebinding(unittest.TestCase):
    def test_bind_axis_run_deps_produces_a_fresh_run_state_each_time(self):
        deps1, _client, tools = bind_tools()
        deps1.run_state.observed_current_run = True
        deps1.run_state.verification_required = True

        deps2, error = aa.bind_axis_run_deps(tools)
        self.assertIsNone(error)
        self.assertIsNot(deps2.run_state, deps1.run_state)
        self.assertFalse(deps2.run_state.observed_current_run)
        self.assertFalse(deps2.run_state.verification_required)


# ---------------------------------------------------------------------------
# 22: independent runs, independent state and tools
# ---------------------------------------------------------------------------

class TestRunIsolation(unittest.TestCase):
    def test_separate_runs_have_independent_state_and_tools(self):
        deps1, client1, tools1 = bind_tools(tab_id=456, url="https://one.example/")
        deps2, client2, tools2 = bind_tools(tab_id=789, url="https://two.example/")

        self.assertIsNot(tools1, tools2)
        self.assertIsNot(deps1.run_state, deps2.run_state)
        self.assertIsNot(tools1.observations, tools2.observations)
        self.assertNotEqual(deps1.initial_tab_handle, deps2.initial_tab_handle)

        deps1.run_state.verification_required = True
        self.assertFalse(deps2.run_state.verification_required)

        client1.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://one/", "title": "one", "snapshotId": "s1"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )
        obs1 = tools1.browser_observe({"browserSessionId": deps1.initial_tab_handle})
        self.assertTrue(obs1["ok"], obs1)
        self.assertEqual(tools2.observations, {})


# ---------------------------------------------------------------------------
# 23: one typed result schema (structural + validator checks)
# ---------------------------------------------------------------------------

class TestOneTypedResultModel(unittest.TestCase):
    def test_completed_requires_verification_summary(self):
        with self.assertRaises(Exception):
            AxisTaskResult(status="completed", summary="x")
        AxisTaskResult(status="completed", summary="x", verification_summary="confirmed")

    def test_needs_user_input_requires_question(self):
        with self.assertRaises(Exception):
            AxisTaskResult(status="needs_user_input", summary="x")
        AxisTaskResult(status="needs_user_input", summary="x", question="Which one?")

    def test_failed_requires_error_code(self):
        with self.assertRaises(Exception):
            AxisTaskResult(status="failed", summary="x")
        AxisTaskResult(status="failed", summary="x", error_code="SOMETHING")

    def test_cancelled_forbids_other_fields(self):
        AxisTaskResult(status="cancelled", summary="x")
        with self.assertRaises(Exception):
            AxisTaskResult(status="cancelled", summary="x", error_code="SOMETHING")


# ---------------------------------------------------------------------------
# Fix 1: navigation invalidates observations and previous assertion proof
# ---------------------------------------------------------------------------

class TestNavigationInvalidatesVerification(unittest.TestCase):
    def _rpc(self, method, params=None, **_):
        params = params or {}
        if method == "page.accessibilityTree":
            return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://application.example.com/next", "title": "Next", "snapshotId": "snap-1"}
        if method == "page.navigate":
            return {"tab": {"url": "https://application.example.com/next", "title": "Next"}, "whatChanged": {"urlChanged": True, "toUrl": "https://application.example.com/next"}}
        if method == "locator.click":
            return {"whatChanged": None}
        if method in ("expect.locator.toHaveValue", "expect.locator.toBeVisible"):
            return {}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    def test_assert_pass_then_navigate_then_completed_is_rejected(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "x",
                })])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_navigate", args={
                    "browserSessionId": deps.initial_tab_handle, "operation": "open", "url": "https://application.example.com/next",
                })])
            return _final_result_call(info, status="completed", summary="done", verification_summary="already confirmed earlier")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Assert then move on."))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.MODEL_ERROR)
        self.assertFalse(deps.run_state.assertion_passed_after_effect)
        self.assertFalse(deps.run_state.observed_current_run)

    def test_navigate_then_observe_then_completed_is_allowed(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_navigate", args={
                    "browserSessionId": deps.initial_tab_handle, "operation": "open", "url": "https://application.example.com/next",
                })])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            return _final_result_call(info, status="completed", summary="Navigated and read the new page.", verification_summary="Observed the destination page.")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Go to the next page and tell me what's there."))

        self.assertEqual(result.status, "completed")

    def test_act_assert_navigate_observe_completed_is_rejected_until_reasserted(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        _grant_effect(deps, tool="browser_act", actions=["click"])
        step = {"n": 0}

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": deps.initial_tab_handle, "action": "click", "locator": {"selector": "#submit"},
                })])
            if step["n"] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": deps.initial_tab_handle, "assertion": "visible", "locator": {"selector": "#success"},
                })])
            if step["n"] == 3:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_navigate", args={
                    "browserSessionId": deps.initial_tab_handle, "operation": "open", "url": "https://application.example.com/next",
                })])
            if step["n"] == 4:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])
            return _final_result_call(info, status="completed", summary="done", verification_summary="the earlier assert was enough")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Click submit, confirm, then move to the next page."))

        # verification_required is still set (the click was never re-verified
        # on the new page) — neither the stale pass nor the new observation
        # satisfies it.
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.MODEL_ERROR)
        self.assertTrue(deps.run_state.verification_required)
        self.assertFalse(deps.run_state.assertion_passed_after_effect)


# ---------------------------------------------------------------------------
# Fix 2: run_axis_task never raises on invalid execution-limit configuration
# ---------------------------------------------------------------------------

class TestConfigurationErrorIsControlled(unittest.TestCase):
    def test_invalid_axis_yaml_returns_configuration_error_without_raising(self):
        import tempfile
        deps, _client, _tools = bind_tools()
        agent = aa.build_axis_agent(FunctionModel(lambda messages, info: _final_result_call(info, status="cancelled", summary="x")))

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp.write("browser: {}\nlimits: {max_requests: FAKE_SECRET_VALUE}\n")
            bad_config_path = tmp.name

        backup = os.environ.get("AXIS_CONFIG_PATH")
        try:
            os.environ["AXIS_CONFIG_PATH"] = bad_config_path
            with self.assertLogs("axis.errors", level="ERROR") as captured:
                result, _history = asyncio.run(aa.run_axis_task(agent, deps, "go"))
        finally:
            if backup is None:
                os.environ.pop("AXIS_CONFIG_PATH", None)
            else:
                os.environ["AXIS_CONFIG_PATH"] = backup
            os.remove(bad_config_path)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "CONFIGURATION_ERROR")
        self.assertFalse(result.retryable)
        self.assertNotIn("FAKE_SECRET_VALUE", result.summary)
        joined_logs = "\n".join(captured.output)
        self.assertNotIn("FAKE_SECRET_VALUE", joined_logs)


# ---------------------------------------------------------------------------
# Fix 3: CLI provider bootstrap failures are controlled
# ---------------------------------------------------------------------------

class TestCliBootstrapIsControlled(unittest.TestCase):
    def test_build_model_failure_exits_cleanly_without_a_traceback(self):
        import axis.cli as cli
        from axis.config import load_axis_config

        fake_provider_config = object()
        real_axis_config = load_axis_config()  # the real, valid axis.yaml
        with patch.object(cli, "load_provider_config", return_value=fake_provider_config), \
             patch.object(cli, "load_axis_config", return_value=real_axis_config), \
             patch.object(cli, "build_model", side_effect=RuntimeError("secret auth detail: sk-FAKE")):
            with self.assertRaises(SystemExit) as captured:
                asyncio.run(cli.main())

        message = str(captured.exception)
        self.assertNotIn("secret auth detail", message)
        self.assertNotIn("sk-FAKE", message)
        self.assertIn("could not be initialized", message)


# ---------------------------------------------------------------------------
# Fix 4: root instructions do not duplicate the browser capability's
# ---------------------------------------------------------------------------

class TestInstructionSeparation(unittest.TestCase):
    def test_root_instructions_do_not_restate_browser_operating_rules(self):
        # Spot-check a handful of browser-specific phrases that must live
        # only in BROWSER_AGENT_INSTRUCTIONS, not duplicated at root.
        browser_specific_phrases = (
            "browser_wait only for",
            "one semantic interaction per browser_act",
            "browser_diagnose only after",
        )
        for phrase in browser_specific_phrases:
            self.assertNotIn(phrase, aa.AXIS_SAFETY_INSTRUCTIONS)
        # But BROWSER_AGENT_INSTRUCTIONS (from the capability) still carries them.
        self.assertIn("browser_diagnose", bat.BROWSER_AGENT_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
