"""Deterministic multi-tab runtime tests (Phase 1.2): A/B/A tab switching,
popup registration, firewall precedence, YAML-driven limits end to end, and
CLI live-trace/redaction/Ctrl+C behavior.

No Chrome, no OCI credentials, no live bridge. Does not duplicate
tests/test_browser_agent_tools.py, tests/test_phase0_baseline.py, or
tests/test_phase1_agent.py — this file only covers the additional
whole-browser/multi-tab/YAML/CLI-observability scenarios from the Phase 1.2
brief.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import pydantic_ai.models as pai_models  # noqa: E402

pai_models.ALLOW_MODEL_REQUESTS = False

import axis.agent as aa  # noqa: E402
import browser_agent_tools as bat  # noqa: E402
from axis.cli import _make_printer, _redact_args_for_print  # noqa: E402
from axis.config import parse_axis_config  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402
from browser_bridge_client import BrowserBridgeClient  # noqa: E402
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402


def _bind_two_tabs():
    client = MagicMock(spec=BrowserBridgeClient)
    client.rpc.side_effect = lambda method, params=None, **_: (
        {"tabs": [
            {"id": 1, "windowId": 1, "active": True, "url": "https://a.example/", "title": "A"},
            {"id": 2, "windowId": 1, "active": False, "url": "https://b.example/", "title": "B"},
        ]} if method == "tabs.list" else (_ for _ in ()).throw(AssertionError(method))
    )
    tools = bat.BrowserAgentTools(client)
    deps, error = aa.bind_axis_run_deps(tools)
    assert deps is not None, error
    refreshed = tools.refresh_tabs()
    handles = {t["url"]: t["browserSessionId"] for t in refreshed["data"]["tabs"]}
    return deps, client, tools, handles["https://a.example/"], handles["https://b.example/"]


def _last_tool_result(messages, tool_name: str):
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                return part.content
    return None


def _final_result_call(info: AgentInfo, **kwargs: Any) -> ModelResponse:
    name = info.output_tools[0].name
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=kwargs)])


# ---------------------------------------------------------------------------
# A -> B -> A switching, ref isolation, using two tabs in one run
# ---------------------------------------------------------------------------

class TestMultiTabSwitching(unittest.TestCase):
    def _rpc(self, method, params=None, **_):
        params = params or {}
        if method == "page.accessibilityTree":
            tab_id = params["tabId"]
            return {"snapshot": f'[f0:e1] button "btn{tab_id}"', "url": f"https://tab{tab_id}/", "title": f"t{tab_id}", "snapshotId": f"snap{tab_id}"}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    def test_model_switches_a_to_b_to_a_within_one_run(self):
        deps, client, tools, handle_a, handle_b = _bind_two_tabs()
        client.rpc.side_effect = self._rpc
        step = {"n": 0}
        seen_handles = []

        def scripted(messages, info):
            step["n"] += 1
            if step["n"] == 1:
                seen_handles.append(handle_a)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": handle_a})])
            if step["n"] == 2:
                seen_handles.append(handle_b)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": handle_b})])
            if step["n"] == 3:
                seen_handles.append(handle_a)
                return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": handle_a})])
            return _final_result_call(info, status="completed", summary="Checked both tabs.", verification_summary="Observed A, then B, then A again.")

        agent = aa.build_axis_agent(FunctionModel(scripted))
        result, _history = asyncio.run(aa.run_axis_task(agent, deps, "Check tab A, then B, then A again."))

        self.assertEqual(result.status, "completed")
        self.assertEqual(seen_handles, [handle_a, handle_b, handle_a])

    def test_ref_from_tab_a_is_rejected_on_tab_b_but_a_and_b_stay_independent(self):
        deps, client, tools, handle_a, handle_b = _bind_two_tabs()
        client.rpc.side_effect = self._rpc

        obs_a = tools.browser_observe({"browserSessionId": handle_a})
        obs_b = tools.browser_observe({"browserSessionId": handle_b})
        self.assertTrue(obs_a["ok"] and obs_b["ok"])

        # A's ref used against B is rejected...
        cross = tools.browser_act({
            "browserSessionId": handle_b, "action": "click",
            "observationId": obs_a["data"]["observationId"], "ref": "ref_1",
        })
        self.assertFalse(cross["ok"])
        self.assertEqual(cross["error"]["code"], bat.STALE_OBSERVATION)

        # ...but each tab's own observation remains independently valid —
        # acting on B never invalidated A's.
        self.assertFalse(tools.observations[obs_a["data"]["observationId"]]["stale"])
        self.assertFalse(tools.observations[obs_b["data"]["observationId"]]["stale"])

    def test_navigating_tab_a_does_not_invalidate_tab_b(self):
        deps, client, tools, handle_a, handle_b = _bind_two_tabs()

        def rpc(method, params=None, **_):
            if method == "page.accessibilityTree":
                return self._rpc(method, params)
            if method == "page.navigate":
                return {"tab": {"url": "https://tab1/next", "title": "Next"}, "whatChanged": {"urlChanged": True, "toUrl": "https://tab1/next"}}
            raise AssertionError(method)

        client.rpc.side_effect = rpc
        obs_a = tools.browser_observe({"browserSessionId": handle_a})
        obs_b = tools.browser_observe({"browserSessionId": handle_b})

        nav = tools.browser_navigate({"browserSessionId": handle_a, "operation": "open", "url": "https://tab1/next"})
        self.assertTrue(nav["ok"], nav)

        self.assertTrue(tools.observations[obs_a["data"]["observationId"]]["stale"])
        self.assertFalse(tools.observations[obs_b["data"]["observationId"]]["stale"])

    def test_closing_tab_a_removes_only_tab_as_observations(self):
        deps, client, tools, handle_a, handle_b = _bind_two_tabs()
        client.rpc.side_effect = lambda method, params=None, **_: (
            self._rpc(method, params) if method == "page.accessibilityTree" else
            {"closed": [params["tabId"]]} if method == "tabs.close" else
            {"tabs": [{"id": 2, "windowId": 1, "active": True, "url": "https://b.example/", "title": "B"}]} if method == "tabs.list" else
            (_ for _ in ()).throw(AssertionError(method))
        )
        obs_a = tools.browser_observe({"browserSessionId": handle_a})
        obs_b = tools.browser_observe({"browserSessionId": handle_b})

        closed = tools.browser_tabs({"operation": "close", "browserSessionId": handle_a})
        self.assertTrue(closed["ok"], closed)

        self.assertNotIn(obs_a["data"]["observationId"], tools.observations)
        self.assertIn(obs_b["data"]["observationId"], tools.observations)
        # And the closed tab's handle now reports TAB_CLOSED, not silently gone.
        rejected = tools.browser_observe({"browserSessionId": handle_a})
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], bat.TAB_CLOSED)


# ---------------------------------------------------------------------------
# Popup registration
# ---------------------------------------------------------------------------

class TestPopupRegistration(unittest.TestCase):
    def test_popup_returned_by_browser_wait_can_be_registered_via_browser_tabs(self):
        deps, client, tools, handle_a, _handle_b = _bind_two_tabs()

        def rpc(method, params=None, **_):
            if method == "page.waitForPopup":
                return {"ok": True, "tab": {"id": 99, "windowId": 1, "url": "https://popup.example/", "title": "Popup"}, "elapsedMs": 12}
            if method == "tabs.list":
                return {"tabs": [
                    {"id": 1, "windowId": 1, "active": True, "url": "https://a.example/", "title": "A"},
                    {"id": 2, "windowId": 1, "active": False, "url": "https://b.example/", "title": "B"},
                    {"id": 99, "windowId": 1, "active": False, "url": "https://popup.example/", "title": "Popup"},
                ]}
            raise AssertionError(method)

        client.rpc.side_effect = rpc
        waited = tools.browser_wait({"browserSessionId": handle_a, "condition": "popup"})
        self.assertTrue(waited["ok"], waited)
        # The raw popup tab isn't auto-registered by browser_wait itself
        # (browser_wait's job is waiting, not tab bookkeeping) — the model's
        # documented next step is browser_tabs(list), which discovers it.
        listed = tools.browser_tabs({"operation": "list"})
        self.assertTrue(listed["ok"])
        popup_entries = [t for t in listed["data"]["tabs"] if t["url"] == "https://popup.example/"]
        self.assertEqual(len(popup_entries), 1)
        self.assertTrue(popup_entries[0]["browserSessionId"].startswith("tab-"))


# ---------------------------------------------------------------------------
# Firewall: checked on every operation, deny overrides allow
# ---------------------------------------------------------------------------

class TestFirewallPrecedence(unittest.TestCase):
    def test_deny_url_overrides_allow_all_default(self):
        client = MagicMock(spec=BrowserBridgeClient)
        client.rpc.side_effect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reach the bridge"))
        fw = bat.FirewallConfig(default="allow", allow_urls=("*",), deny_urls=("*://blocked.example/*",))
        tools = bat.BrowserAgentTools(client, firewall_config=fw)

        result = tools.browser_tabs({"operation": "create", "url": "https://blocked.example/x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.FIREWALL_DENIED)

    def test_deny_method_overrides_allow_all_default(self):
        client = MagicMock(spec=BrowserBridgeClient)
        fw = bat.FirewallConfig(default="allow", allow_methods=("*",), deny_methods=("tabs.create",))
        tools = bat.BrowserAgentTools(client, firewall_config=fw)

        result = tools.browser_tabs({"operation": "create", "url": "https://ok.example/"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.FIREWALL_DENIED)
        client.rpc.assert_not_called()

    def test_firewall_is_checked_for_ordinary_tab_operations_too(self):
        # Not just tabs.create — an ordinary browser_navigate on an
        # already-registered tab is checked against the tab's current URL.
        client = MagicMock(spec=BrowserBridgeClient)
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{"id": 1, "windowId": 1, "active": True, "url": "https://blocked.example/", "title": "B"}]}
            if method == "tabs.list" else (_ for _ in ()).throw(AssertionError(method))
        )
        fw = bat.FirewallConfig(deny_urls=("*://blocked.example/*",))
        tools = bat.BrowserAgentTools(client, firewall_config=fw)
        refreshed = tools.refresh_tabs()
        handle = refreshed["data"]["tabs"][0]["browserSessionId"]

        result = tools.browser_observe({"browserSessionId": handle})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.FIREWALL_DENIED)

    def test_explicit_scheme_exception_opens_only_the_named_url_not_the_whole_scheme(self):
        # axis.yaml carves out chrome://newtab/ specifically (the blank new
        # tab page is inert) while every other chrome:// page stays denied —
        # proving a blanket allow_urls "*" never overrides deny_schemes, but
        # a narrow same-scheme allow_urls pattern does, and only for the URL
        # it names.
        client = MagicMock(spec=BrowserBridgeClient)
        fw = bat.FirewallConfig(allow_urls=("*", "chrome://newtab/*"))
        tools = bat.BrowserAgentTools(client, firewall_config=fw)

        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{"id": 1, "windowId": 1, "active": True, "url": "chrome://newtab/", "title": "New Tab"}]}
            if method == "tabs.list" else
            {"snapshot": "", "url": "chrome://newtab/", "title": "New Tab", "snapshotId": "s"}
            if method == "page.accessibilityTree" else (_ for _ in ()).throw(AssertionError(method))
        )
        newtab_handle = tools.refresh_tabs()["data"]["tabs"][0]["browserSessionId"]
        allowed = tools.browser_observe({"browserSessionId": newtab_handle})
        self.assertTrue(allowed["ok"], allowed)

        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{"id": 2, "windowId": 1, "active": True, "url": "chrome://settings/", "title": "Settings"}]}
            if method == "tabs.list" else (_ for _ in ()).throw(AssertionError(method))
        )
        settings_handle = tools.refresh_tabs()["data"]["tabs"][0]["browserSessionId"]
        denied = tools.browser_observe({"browserSessionId": settings_handle})
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], bat.FIREWALL_DENIED)


# ---------------------------------------------------------------------------
# YAML limits control the real run limits end to end
# ---------------------------------------------------------------------------

class TestYamlLimitsControlRealRun(unittest.TestCase):
    def test_yaml_tool_call_limit_stops_the_run(self):
        client = MagicMock(spec=BrowserBridgeClient)
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{"id": 1, "windowId": 1, "active": True, "url": "https://a/", "title": "A"}]} if method == "tabs.list"
            else {"snapshot": "", "url": "https://a/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )
        tools = bat.BrowserAgentTools(client)
        deps, error = aa.bind_axis_run_deps(tools)
        assert deps is not None, error

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])

        config = parse_axis_config({"browser": {}, "limits": {"max_tool_calls": 1, "max_requests": None, "max_total_tokens": None, "max_wall_clock_seconds": None}})
        agent = aa.build_axis_agent(FunctionModel(always_observe))
        result, _h = asyncio.run(aa.run_axis_task(agent, deps, "loop", limits=config.limits))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, aa.TOOL_CALL_LIMIT_EXCEEDED)

    def test_changing_yaml_value_changes_runtime_behavior(self):
        # Same scripted model/mock; only the configured limit differs, and
        # that alone flips the outcome from failed to (eventually) allowed
        # to keep going far enough to hit the *next* configured limit.
        client = MagicMock(spec=BrowserBridgeClient)
        client.rpc.side_effect = lambda method, params=None, **_: (
            {"tabs": [{"id": 1, "windowId": 1, "active": True, "url": "https://a/", "title": "A"}]} if method == "tabs.list"
            else {"snapshot": "", "url": "https://a/", "title": "t", "snapshotId": "s"} if method == "page.accessibilityTree"
            else (_ for _ in ()).throw(AssertionError(method))
        )
        tools = bat.BrowserAgentTools(client)
        deps, error = aa.bind_axis_run_deps(tools)
        assert deps is not None, error

        def always_observe(messages, info):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_observe", args={"browserSessionId": deps.initial_tab_handle})])

        tight = parse_axis_config({"browser": {}, "limits": {"max_tool_calls": 1, "max_requests": None}}).limits
        loose = parse_axis_config({"browser": {}, "limits": {"max_tool_calls": None, "max_requests": 3}}).limits

        agent1 = aa.build_axis_agent(FunctionModel(always_observe))
        result1, _h1 = asyncio.run(aa.run_axis_task(agent1, deps, "loop", limits=tight))
        self.assertEqual(result1.error_code, aa.TOOL_CALL_LIMIT_EXCEEDED)

        agent2 = aa.build_axis_agent(FunctionModel(always_observe))
        deps2 = aa.next_run(deps)
        result2, _h2 = asyncio.run(aa.run_axis_task(agent2, deps2, "loop", limits=loose))
        # Different YAML value -> a different limit (the tool-call ceiling is
        # now disabled, so the run instead hits the request ceiling) stops
        # the run — proving the YAML value, not a hidden fallback, controls
        # which limit governs.
        self.assertEqual(result2.error_code, aa.REQUEST_LIMIT_EXCEEDED)


# ---------------------------------------------------------------------------
# CLI live trace: sanitized printer output, redaction, Ctrl+C
# ---------------------------------------------------------------------------

class TestCliObservability(unittest.TestCase):
    def test_printer_prints_tool_and_tab_events_live(self):
        cli_config = parse_axis_config({"browser": {}, "limits": {}, "cli": {"trace": "verbose"}}).cli
        printer = _make_printer(cli_config)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            printer({"eventType": "tool_started", "toolName": "browser_observe", "tabHandle": "tab-abc123", "runId": "r", "timestamp": 0, "durationMs": None, "success": None, "detail": None, "data": None})
            printer({"eventType": "tool_completed", "toolName": "browser_observe", "tabHandle": "tab-abc123", "success": True, "durationMs": 12.3, "runId": "r", "timestamp": 0, "detail": None, "data": None})
            printer({"eventType": "tab_created", "tabHandle": "tab-xyz789", "runId": "r", "timestamp": 0, "toolName": None, "durationMs": None, "success": None, "detail": None, "data": None})
        output = buffer.getvalue()
        self.assertIn("browser_observe", output)
        self.assertIn("tab-abc123", output)
        self.assertIn("tab-xyz789", output)

    def test_quiet_trace_mode_suppresses_the_printer(self):
        cli_config = parse_axis_config({"browser": {}, "limits": {}, "cli": {"trace": "quiet"}}).cli
        self.assertIsNone(_make_printer(cli_config))

    def test_sensitive_argument_values_are_redacted_before_printing(self):
        redacted = _redact_args_for_print({"password": "hunter2", "url": "https://example.com/", "token": "sk-abc"})
        self.assertEqual(redacted["password"], "<redacted>")
        self.assertEqual(redacted["token"], "<redacted>")
        self.assertEqual(redacted["url"], "https://example.com/")

    def test_ctrl_c_during_chat_loop_exits_without_a_traceback(self):
        import axis.cli as cli

        async def raise_keyboard_interrupt(*_a, **_k):
            raise KeyboardInterrupt

        fake_axis_config = parse_axis_config({"browser": {}, "limits": {}})
        with patch.object(cli, "load_provider_config", return_value=object()), \
             patch.object(cli, "load_axis_config", return_value=fake_axis_config), \
             patch.object(cli, "build_model", return_value=(object(), MagicMock())), \
             patch.object(cli, "_run_chat_loop", side_effect=raise_keyboard_interrupt):
            # main() must catch this and return normally — no traceback, no
            # unhandled exception propagating out of asyncio.run.
            asyncio.run(cli.main())


if __name__ == "__main__":
    unittest.main()
