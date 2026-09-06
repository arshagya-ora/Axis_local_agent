"""Deterministic tests for the Axis Phase 1 root agent.

Everything here uses Pydantic AI's `FunctionModel`/`TestModel` and a mocked
`BrowserBridgeClient` — no OCI credentials, no Chrome, no live Browser
Agent Bridge. Does not duplicate `tests/test_browser_agent_tools.py`'s 114
tests or `tests/test_phase0_baseline.py`'s 30 tests; this file only proves
the Phase 1 agent layer built on top of them.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = AGENT_DIR / "scripts"
for path in (AGENT_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from pydantic_ai import UsageLimits  # noqa: E402
from pydantic_ai.exceptions import UnexpectedModelBehavior  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

import axis_agent as aa  # noqa: E402
import browser_agent_tools as bat  # noqa: E402
import browser_contract  # noqa: E402
from axis_browser_capability import AxisBrowserCapability  # noqa: E402
from axis_events import RunEventLogger  # noqa: E402
from axis_models import (  # noqa: E402
    AxisRunDeps,
    AxisRunLimits,
    NeedsUserInput,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)
from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError  # noqa: E402

EXPECTED_TOOL_NAMES = (
    "browser_observe",
    "browser_act",
    "browser_navigate",
    "browser_wait",
    "browser_assert",
    "browser_capture_evidence",
    "browser_diagnose",
)

FORBIDDEN_INPUT_PROPERTY_NAMES = frozenset({
    "tabId", "windowId", "groupId", "frameId", "bridgeSessionId",
    "bridgeMethod", "method", "rpcMethod", "snapshotId",
})

MANAGED_SESSION_LIST = {
    "sessions": [{"id": "bridge-session-1", "name": "🤖 Agent", "groupId": 5, "mainTabId": 456, "tabIds": [456]}],
}
MANAGED_SESSION_GET = {
    "session": {"id": "bridge-session-1", "groupId": 5, "mainTabId": 456, "tabIds": [456]},
    "tabs": [{"id": 456, "windowId": 12, "groupId": 5, "active": True, "url": "https://application.example.com/profile", "title": "Profile"}],
}
FOCUSED_TAB_IN_GROUP_5 = {
    "tabs": [{"id": 456, "windowId": 12, "groupId": 5, "url": "https://application.example.com/profile", "title": "Profile"}],
}

# A second, independent managed session/tab (different groupId/tabId) used
# by the isolation and session-substitution tests.
MANAGED_SESSION_LIST_2 = {
    "sessions": [{"id": "bridge-session-2", "name": "🤖 Agent", "groupId": 9, "mainTabId": 789, "tabIds": [789]}],
}
MANAGED_SESSION_GET_2 = {
    "session": {"id": "bridge-session-2", "groupId": 9, "mainTabId": 789, "tabIds": [789]},
    "tabs": [{"id": 789, "windowId": 34, "groupId": 9, "active": True, "url": "https://application.example.com/other", "title": "Other"}],
}
FOCUSED_TAB_IN_GROUP_9 = {
    "tabs": [{"id": 789, "windowId": 34, "groupId": 9, "url": "https://application.example.com/other", "title": "Other"}],
}


def _bind_query(method: str, params: Optional[Dict[str, Any]], group_id: int, focused_tabs: Dict[str, Any]) -> Optional[Any]:
    params = params or {}
    if method == "tabs.list":
        query = params.get("query", {})
        if query.get("groupId") == group_id and query.get("active") is True and query.get("lastFocusedWindow") is True:
            return focused_tabs
        return {"tabs": []}
    return None


def bind_tools(session_list=MANAGED_SESSION_LIST, session_get=MANAGED_SESSION_GET, group_id=5, focused_tabs=FOCUSED_TAB_IN_GROUP_5):
    """Create a fresh BrowserAgentTools + trusted AxisRunDeps against a
    mocked, already-bound managed tab. Each call returns its own
    BrowserAgentTools instance (its own observations dict) — callers get
    isolation for free by calling this once per simulated run."""
    client = MagicMock(spec=BrowserBridgeClient)

    def bind_rpc(method, params=None, **_):
        if method == "session.list":
            return session_list
        if method == "session.get":
            return session_get
        tabs_result = _bind_query(method, params, group_id, focused_tabs)
        if tabs_result is not None:
            return tabs_result
        raise AssertionError(f"unexpected rpc call during bind: {method}")

    client.rpc.side_effect = bind_rpc
    tools = bat.BrowserAgentTools(client)
    bound = tools.bind_active_managed_tab()
    assert bound["ok"], bound
    deps = AxisRunDeps(run_id="run-test", browser_session_id=bound["data"]["browserSessionId"], browser_tools=tools)
    return deps, client, tools


def _last_tool_result(messages: List[ModelMessage], tool_name: str) -> Optional[Dict[str, Any]]:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                return part.content
    return None


def _output_tool_name(info: AgentInfo, variant: str) -> str:
    return next(t.name for t in info.output_tools if variant in t.name)


# ---------------------------------------------------------------------------
# 1-3: the seven tools, unchanged, no raw identifiers
# ---------------------------------------------------------------------------

class TestSevenToolsUnchanged(unittest.TestCase):
    def test_exactly_seven_tools(self):
        capability = AxisBrowserCapability()
        self.assertEqual(len(capability.build_tools()), 7)
        self.assertEqual(tuple(capability.tool_names), EXPECTED_TOOL_NAMES)

    def test_capability_definitions_match_committed_phase0_snapshot(self):
        capability = AxisBrowserCapability()
        committed = browser_contract.load_committed_snapshot()
        self.assertIsNotNone(committed, "Phase 0 contract snapshot must exist")
        self.assertEqual(capability._definitions, committed["tools"])
        self.assertTrue(browser_contract.verify())

    def test_no_raw_rpc_method_or_chrome_identifiers_in_tool_schemas(self):
        def iter_props(schema):
            if not isinstance(schema, dict):
                return
            for name, sub in (schema.get("properties") or {}).items():
                yield name
                yield from iter_props(sub)
            items = schema.get("items")
            if isinstance(items, dict):
                yield from iter_props(items)

        capability = AxisBrowserCapability()
        for entry in capability._definitions:
            props = set(iter_props(entry["function"]["parameters"]))
            leaked = props & (FORBIDDEN_INPUT_PROPERTY_NAMES | {"method", "bridgeMethod", "rpcMethod"})
            self.assertFalse(leaked, f"{entry['function']['name']} leaks: {leaked}")


# ---------------------------------------------------------------------------
# 4-5: trusted deps and session substitution
# ---------------------------------------------------------------------------

class TestTrustedRunDeps(unittest.TestCase):
    def test_deps_carry_the_bound_session_and_an_isolated_tools_instance(self):
        deps, _client, tools = bind_tools()
        self.assertTrue(deps.browser_session_id.startswith("bs-"))
        self.assertIs(deps.browser_tools, tools)

    def test_model_cannot_switch_to_a_different_browser_session_id(self):
        deps, client, tools = bind_tools()
        # A second, genuinely valid session bound in the SAME tools instance
        # — proves the override isn't just "reject anything unrecognized",
        # it forces the trusted id even over another real session's id.
        client.rpc.side_effect = lambda method, params=None, **_: (
            MANAGED_SESSION_LIST_2 if method == "session.list" else
            MANAGED_SESSION_GET_2 if method == "session.get" else
            (_bind_query(method, params, 9, FOCUSED_TAB_IN_GROUP_9) or {"tabs": []})
        )
        other = tools.bind_active_managed_tab()
        self.assertTrue(other["ok"], other)
        other_session_id = other["data"]["browserSessionId"]
        self.assertNotEqual(other_session_id, deps.browser_session_id)

        capability = AxisBrowserCapability()
        observe_tool = next(t for t in capability.build_tools() if t.name == "browser_observe")

        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(f"unexpected rpc: {method}"))
        )

        from pydantic_ai import RunContext

        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps  # trusted deps bound to the FIRST session

        result = observe_tool.function(ctx, browserSessionId=other_session_id)
        # The call must have run against the trusted (first) session's tab,
        # never the model-supplied second one.
        self.assertEqual(result["browserSessionId"], deps.browser_session_id)
        tab_id_used = client.rpc.call_args[0][1]["tabId"]
        self.assertEqual(tab_id_used, tools.browser_sessions[deps.browser_session_id]["tabId"])


# ---------------------------------------------------------------------------
# 6-7: observe -> act -> assert -> completed, and unverified completion
# ---------------------------------------------------------------------------

class TestObserveActAssertTrajectory(unittest.TestCase):
    def _rpc(self, method, params=None, **_):
        params = params or {}
        if method == "page.accessibilityTree":
            return {"snapshot": '[f0:e1] textbox "Display name"', "url": "https://application.example.com/profile", "title": "Profile", "snapshotId": "snap-1"}
        if method == "locator.fillRef":
            return {"whatChanged": None}
        if method == "expect.locator.toHaveValue":
            return {}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    def test_full_success_trajectory_returns_task_completed(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        session_id = deps.browser_session_id

        step = {"n": 0}

        def scripted(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
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
            name = _output_tool_name(info, "TaskCompleted")
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args={
                "status": "completed", "summary": "Display name updated.",
                "verification_summary": "browser_assert confirmed the new value.", "warnings": [],
            })])

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Change the display name and confirm it."))

        self.assertIsInstance(result, TaskCompleted)
        self.assertEqual(result.summary, "Display name updated.")
        self.assertEqual(step["n"], 4)

    def test_unverified_completion_is_rejected_and_retried(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = self._rpc
        session_id = deps.browser_session_id

        calls = {"n": 0}

        def scripted(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                # Only a browser_act, no assert/observe afterward — must not
                # be accepted as proof of completion.
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": session_id, "action": "click", "locator": {"selector": "#submit"},
                })])
            name = _output_tool_name(info, "TaskCompleted")
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args={
                "status": "completed", "summary": "done", "verification_summary": "trust me", "warnings": [],
            })])

        # locator.click must succeed for this scenario (act succeeds; only
        # the *completion claim* is what's supposed to be rejected).
        def rpc_with_click(method, params=None, **_):
            if method == "locator.click":
                return {"whatChanged": None}
            return self._rpc(method, params)

        client.rpc.side_effect = rpc_with_click

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Click submit and report done."))

        # Exhausts the output-validator retry budget and surfaces as a
        # controlled, typed failure — never TaskCompleted. Total scripted
        # calls = 1 real tool call + (1 initial output attempt + DEFAULT_RETRIES
        # retries), all rejected by the completion-verification output validator.
        self.assertIsInstance(result, TaskFailed)
        self.assertEqual(result.error_code, aa.MODEL_ERROR)
        self.assertEqual(calls["n"], 1 + 1 + aa.DEFAULT_RETRIES)


# ---------------------------------------------------------------------------
# 8-9: stale ref -> re-observe, never blindly retried
# ---------------------------------------------------------------------------

class TestStaleRefHandling(unittest.TestCase):
    def test_stale_ref_forces_reobservation_and_is_never_retried(self):
        deps, client, _tools = bind_tools()
        session_id = deps.browser_session_id
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
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": session_id})])
            if step["n"] == 2:
                obs = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": session_id, "action": "fill", "value": "x",
                    "observationId": obs["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 3:
                act_result = _last_tool_result(messages, "browser_act")
                self.assertFalse(act_result["ok"])
                self.assertEqual(act_result["error"]["code"], bat.STALE_OBSERVATION)
                # Correct behavior: observe again, do not retry the stale ref.
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": session_id})])
            if step["n"] == 4:
                obs2 = _last_tool_result(messages, "browser_observe")
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_act", args={
                    "browserSessionId": session_id, "action": "fill", "value": "x",
                    "observationId": obs2["data"]["observationId"], "ref": "ref_1",
                })])
            if step["n"] == 5:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_assert", args={
                    "browserSessionId": session_id, "assertion": "value",
                    "locator": {"selector": "#display-name"}, "expected": "x",
                })])
            name = _output_tool_name(info, "TaskCompleted")
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args={
                "status": "completed", "summary": "ok", "verification_summary": "confirmed", "warnings": [],
            })])

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Fill and confirm."))

        self.assertIsInstance(result, TaskCompleted)
        self.assertEqual(observe_calls["n"], 2)
        # The stale snapshot was attempted exactly once and never retried;
        # the fresh one succeeded exactly once.
        self.assertEqual(fill_calls_by_snapshot, {"snap-1": 1, "snap-2": 1})


# ---------------------------------------------------------------------------
# 10-11: invalid arguments and handler exceptions never crash the run
# ---------------------------------------------------------------------------

class TestToolFailureSafety(unittest.TestCase):
    def test_invalid_tool_arguments_return_a_controlled_typed_failure(self):
        deps, _client, _tools = bind_tools()
        capability = AxisBrowserCapability()
        act_tool = next(t for t in capability.build_tools() if t.name == "browser_act")

        from pydantic_ai import RunContext
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        # 'fill' requires 'value'; omitted here.
        result = act_tool.function(ctx, browserSessionId=deps.browser_session_id, action="fill", locator={"selector": "#x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_handler_exception_does_not_crash_and_returns_typed_failure(self):
        deps, _client, tools = bind_tools()
        tools.browser_observe = MagicMock(side_effect=RuntimeError("boom"))
        capability = AxisBrowserCapability()
        observe_tool = next(t for t in capability.build_tools() if t.name == "browser_observe")

        from pydantic_ai import RunContext
        ctx = MagicMock(spec=RunContext)
        ctx.deps = deps

        result = observe_tool.function(ctx, browserSessionId=deps.browser_session_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TOOL_EXECUTION_ERROR")
        self.assertIn("boom", result["error"]["message"])


# ---------------------------------------------------------------------------
# 12-13: retry budget and execution limits
# ---------------------------------------------------------------------------

class TestRunLimits(unittest.TestCase):
    def test_tool_calls_limit_stops_the_run_cleanly(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.browser_session_id})])

        agent = aa.build_axis_agent(FunctionModel(always_observe))
        limits = AxisRunLimits(max_tool_calls=1, max_requests=10, max_total_tokens=None, max_wall_clock_seconds=5)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "loop forever", limits=limits))

        self.assertIsInstance(result, TaskFailed)
        self.assertEqual(result.error_code, aa.RUN_LIMIT_EXCEEDED)
        self.assertTrue(result.retryable)

    def test_request_limit_stops_the_run_cleanly(self):
        # Shares the exact same UsageLimitExceeded handling path as
        # max_tool_calls/max_total_tokens — see run_axis_task's single
        # `except UsageLimitExceeded` branch — so this also stands in for
        # the total-tokens-limit case.
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.browser_session_id})])

        agent = aa.build_axis_agent(FunctionModel(always_observe))
        limits = AxisRunLimits(max_tool_calls=50, max_requests=1, max_total_tokens=None, max_wall_clock_seconds=5)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "loop forever", limits=limits))

        self.assertIsInstance(result, TaskFailed)
        self.assertEqual(result.error_code, aa.RUN_LIMIT_EXCEEDED)

    def test_wall_clock_timeout_stops_the_run_cleanly(self):
        deps, _client, _tools = bind_tools()

        async def slow(messages, info):
            await asyncio.sleep(2)
            name = _output_tool_name(info, "TaskCompleted")
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args={
                "status": "completed", "summary": "x", "verification_summary": "x", "warnings": [],
            })])

        agent = aa.build_axis_agent(FunctionModel(slow))
        limits = AxisRunLimits(max_wall_clock_seconds=0.05)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "be slow", limits=limits))

        self.assertIsInstance(result, TaskFailed)
        self.assertEqual(result.error_code, aa.RUN_TIMEOUT)
        self.assertTrue(result.retryable)

    def test_limits_are_read_from_the_trusted_object_not_model_output(self):
        # No tool/output schema exposes any limit-related field at all.
        capability = AxisBrowserCapability()
        for entry in capability._definitions:
            props = set((entry["function"]["parameters"].get("properties") or {}).keys())
            self.assertFalse(props & {"maxRequests", "maxToolCalls", "maxTotalTokens", "maxWallClockSeconds"})
        for result_cls in (TaskCompleted, NeedsUserInput, TaskFailed, TaskCancelled):
            self.assertFalse(set(result_cls.model_fields) & {"maxRequests", "maxToolCalls", "maxTotalTokens", "maxWallClockSeconds"})

    def test_limits_are_configurable_via_environment(self):
        backup = os.environ.get("AXIS_MAX_TOOL_CALLS")
        try:
            os.environ["AXIS_MAX_TOOL_CALLS"] = "5"
            self.assertEqual(AxisRunLimits.from_env().max_tool_calls, 5)
        finally:
            if backup is None:
                os.environ.pop("AXIS_MAX_TOOL_CALLS", None)
            else:
                os.environ["AXIS_MAX_TOOL_CALLS"] = backup


# ---------------------------------------------------------------------------
# 14: no bound browser session
# ---------------------------------------------------------------------------

class TestNoBoundSession(unittest.TestCase):
    def test_no_managed_tab_produces_a_clear_typed_failure(self):
        client = MagicMock(spec=BrowserBridgeClient)
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"sessions": []} if method == "session.list" else (_ for _ in ()).throw(AssertionError(method))
        )
        tools = bat.BrowserAgentTools(client)
        deps, error = aa.bind_axis_run_deps(tools)
        self.assertIsNone(deps)
        self.assertIsNotNone(error)

        result = aa.no_managed_tab_result(error)
        self.assertIsInstance(result, TaskFailed)
        self.assertEqual(result.error_code, aa.NO_MANAGED_TAB)
        self.assertTrue(result.retryable)


# ---------------------------------------------------------------------------
# 15: structured events carry correlation ids, never sensitive payloads
# ---------------------------------------------------------------------------

class TestStructuredEvents(unittest.TestCase):
    def test_events_carry_run_id_and_no_sensitive_or_raw_identifier_fields(self):
        deps, client, _tools = bind_tools()
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://x/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )

        def scripted(messages, info):
            if _last_tool_result(messages, "browser_observe") is None:
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.browser_session_id})])
            name = _output_tool_name(info, "TaskCompleted")
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args={
                "status": "completed", "summary": "x", "verification_summary": "x", "warnings": [],
            })])

        sink: List[Dict[str, Any]] = []
        event_logger = RunEventLogger(sink=sink)
        agent = aa.build_axis_agent(FunctionModel(scripted), event_logger=event_logger)
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "observe then finish", event_logger=event_logger))

        self.assertIsInstance(result, TaskCompleted)
        self.assertGreater(len(sink), 0)
        allowed_keys = {"runId", "eventType", "timestamp", "toolName", "durationMs", "success"}
        forbidden_substrings = ("password", "token", "cookie", "secret", "authorization")
        for event in sink:
            self.assertEqual(set(event.keys()), allowed_keys)
            self.assertEqual(event["runId"], deps.run_id)
            dumped = str(event).lower()
            for bad in forbidden_substrings:
                self.assertNotIn(bad, dumped)
        event_types = {event["eventType"] for event in sink}
        self.assertIn("run_started", event_types)
        self.assertIn("run_completed", event_types)
        self.assertIn("tool_started", event_types)
        self.assertIn("tool_completed", event_types)


# ---------------------------------------------------------------------------
# 16: no shared state across concurrent/separate runs
# ---------------------------------------------------------------------------

class TestRunIsolation(unittest.TestCase):
    def test_separate_runs_have_independent_tools_and_observations(self):
        deps1, client1, tools1 = bind_tools()
        deps2, client2, tools2 = bind_tools(session_list=MANAGED_SESSION_LIST_2, session_get=MANAGED_SESSION_GET_2, group_id=9, focused_tabs=FOCUSED_TAB_IN_GROUP_9)

        self.assertIsNot(tools1, tools2)
        self.assertIsNot(tools1.observations, tools2.observations)
        self.assertNotEqual(deps1.browser_session_id, deps2.browser_session_id)

        client1.rpc.side_effect = lambda method, params=None, **_: (
            {"snapshot": "", "url": "https://one/", "title": "one", "snapshotId": "s1"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )
        obs1 = tools1.browser_observe({"browserSessionId": deps1.browser_session_id})
        self.assertTrue(obs1["ok"], obs1)

        # tools2 must not see tools1's observation at all.
        self.assertEqual(tools2.observations, {})
        self.assertIn(obs1["data"]["observationId"], tools1.observations)


if __name__ == "__main__":
    unittest.main()
