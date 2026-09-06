"""Tests for the Axis Agent browser tool layer.

BrowserBridgeClient.rpc is mocked throughout — nothing here talks to a real
Chrome instance or the Browser Agent Bridge's native host. Sections mirror
browser_agent_tools.py's own section banners.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_agent_tools as bat  # noqa: E402
from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# bind_active_managed_tab()'s sequence is: session.list -> session.get (to
# read each managed session's groupId) -> tabs.list({query: {groupId,
# active:true, lastFocusedWindow:true}}) to confirm that group's active tab
# is also Chrome's actually-focused tab. See browser_agent_tools.py's
# docstring for why this replaces an unscoped "list every tab" call, which
# the bridge's tab-isolation gate does not allow.
MANAGED_SESSION_LIST = {
    "sessions": [{"id": "bridge-session-1", "name": "🤖 Agent", "groupId": 5, "mainTabId": 456, "tabIds": [456]}],
}
MANAGED_SESSION_GET = {
    "session": {"id": "bridge-session-1", "groupId": 5, "mainTabId": 456, "tabIds": [456]},
    "tabs": [{"id": 456, "windowId": 12, "groupId": 5, "active": True, "url": "https://application.example.com", "title": "App"}],
}
FOCUSED_TAB_IN_GROUP_5 = {
    "tabs": [{"id": 456, "windowId": 12, "groupId": 5, "url": "https://application.example.com", "title": "App"}],
}


def make_client(rpc_side_effect=None):
    client = MagicMock(spec=BrowserBridgeClient)
    if rpc_side_effect is not None:
        client.rpc.side_effect = rpc_side_effect
    return client


def default_bind_rpc(method, params=None, **_):
    if method == "session.list":
        return MANAGED_SESSION_LIST
    if method == "session.get":
        return MANAGED_SESSION_GET
    if method == "tabs.list":
        query = (params or {}).get("query", {})
        if query.get("groupId") == 5 and query.get("active") is True and query.get("lastFocusedWindow") is True:
            return FOCUSED_TAB_IN_GROUP_5
        return {"tabs": []}
    raise AssertionError(f"unexpected rpc call in bind: {method}")


def bind_tools(rpc_side_effect=None, **kwargs):
    """Create a BrowserAgentTools with a mocked, already-bound managed tab."""
    client = make_client()
    client.rpc.side_effect = default_bind_rpc
    tools = bat.BrowserAgentTools(client, **kwargs)
    result = tools.bind_active_managed_tab()
    assert result["ok"], result
    session_id = result["browserSessionId"]
    if rpc_side_effect is not None:
        client.rpc.side_effect = rpc_side_effect
    else:
        client.rpc.reset_mock(side_effect=True)
    return tools, client, session_id


def compact_rpc_result(snapshot="[f0:e1] textbox \"Username\"\n[f0:e2] button \"Submit\"", **overrides):
    result = {
        "snapshot": snapshot,
        "url": "https://application.example.com/",
        "title": "Application",
        "snapshotId": "snap-1",
        "nodeCount": 2,
        "truncated": False,
    }
    result.update(overrides)
    return result


def observe_compact(tools, client, session_id, **overrides):
    client.rpc.side_effect = lambda method, params=None, **_: compact_rpc_result(**overrides)
    result = tools.browser_observe({"browserSessionId": session_id, "mode": "compact"})
    assert result["ok"], result
    return result["data"]


# ---------------------------------------------------------------------------
# Tool registry: definitions, schemas, dispatcher
# ---------------------------------------------------------------------------

class TestToolRegistry(unittest.TestCase):
    def test_exactly_eight_tools_exported(self):
        # v2 contract (Phase 1.2): the original seven semantic tools plus
        # browser_tabs. v1's frozen 7-tool snapshot is checked separately
        # (see tests/test_phase0_baseline.py) as a historical artifact.
        names = [t["function"]["name"] for t in bat.get_tool_definitions()]
        self.assertEqual(len(names), 8)
        self.assertEqual(sorted(names), sorted([
            "browser_observe", "browser_act", "browser_navigate", "browser_wait",
            "browser_assert", "browser_capture_evidence", "browser_diagnose", "browser_tabs",
        ]))

    def test_definitions_are_function_tool_shaped_with_no_duplicates(self):
        seen = set()
        for definition in bat.get_tool_definitions():
            self.assertEqual(definition["type"], "function")
            fn = definition["function"]
            self.assertNotIn(fn["name"], seen)
            seen.add(fn["name"])
            self.assertIsInstance(fn["description"], str)
            self.assertGreater(len(fn["description"]), 200)
            self.assertIn("parameters", fn)

    def test_schemas_use_additional_properties_false_and_required_session_id(self):
        # browser_tabs is the one exception: 'list'/'create' don't need an
        # existing handle, so browserSessionId is optional (required only
        # for 'activate'/'close', checked at the argument level instead).
        for definition in bat.get_tool_definitions():
            params = definition["function"]["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIs(params["additionalProperties"], False)
            self.assertIn("browserSessionId", params["properties"])
            if definition["function"]["name"] != "browser_tabs":
                self.assertIn("browserSessionId", params["required"])

    def test_descriptions_cover_all_required_sections(self):
        required_sections = [
            "Purpose:", "When to use:", "When not to use:", "Important capabilities:",
            "Required sequencing:", "Important limitations:", "Expected result:",
            "Recommended next step:",
        ]
        # browser_tabs uses its own section labels, per its design brief.
        browser_tabs_sections = [
            "Purpose:", "When to use:", "When not to use:", "Capabilities:",
            "Required workflow:", "Limitations:", "Expected result:",
            "Common errors:", "Recommended next step:",
        ]
        for definition in bat.get_tool_definitions():
            description = definition["function"]["description"]
            sections = browser_tabs_sections if definition["function"]["name"] == "browser_tabs" else required_sections
            for section in sections:
                self.assertIn(section, description, f"{definition['function']['name']} missing {section!r}")

    def test_property_descriptions_present(self):
        for definition in bat.get_tool_definitions():
            for name, subschema in definition["function"]["parameters"]["properties"].items():
                self.assertIn("description", subschema, f"{definition['function']['name']}.{name} missing description")
                self.assertGreater(len(subschema["description"]), 10)

    def test_no_forbidden_or_internal_fields_are_freely_selectable(self):
        forbidden_field_names = {"tabId", "windowId", "groupId", "snapshotId", "frameId", "method", "bridgeMethod", "targetRef"}
        for definition in bat.get_tool_definitions():
            props = set(definition["function"]["parameters"]["properties"])
            self.assertEqual(props & forbidden_field_names, set())

    def test_target_ref_is_absent_from_the_act_schema(self):
        # The bridge has no locator.dragToRef, so an argument that would
        # always fail must not be shown to the LLM at all.
        act = next(d for d in bat.get_tool_definitions() if d["function"]["name"] == "browser_act")
        self.assertNotIn("targetRef", act["function"]["parameters"]["properties"])
        self.assertNotIn("targetRef", act["function"]["description"])

    def test_serialized_tool_definitions_stay_within_size_budget(self):
        import json as _json
        serialized = _json.dumps(bat.get_tool_definitions(), separators=(",", ":"))
        # Was ~38.5KB before the size-reduction pass; keep it materially
        # smaller without making the schemas vague.
        self.assertLessEqual(len(serialized), 30_000, f"tool definitions are {len(serialized)} bytes")

    def test_preserved_cross_tool_guidance_survives_shortening(self):
        # These behavioral facts must remain discoverable from the tool
        # descriptions themselves even after the size-reduction pass (some
        # now live only in BROWSER_AGENT_INSTRUCTIONS per the division of
        # responsibility described in the task brief).
        descriptions = " ".join(d["function"]["description"] for d in bat.get_tool_definitions()).lower()
        self.assertIn("one interaction", descriptions)
        self.assertIn("invalidat", descriptions)  # navigate/act invalidation language
        self.assertIn("not an assertion", descriptions)
        self.assertIn("passed=false", descriptions)

        import re as _re
        instructions = _re.sub(r"\s+", " ", bat.BROWSER_AGENT_INSTRUCTIONS.lower())
        for phrase in [
            "first browser tool used on a page",       # browser_observe normally comes first
            "use only refs from the latest",
            "performs exactly one interaction",
            "navigation invalidates observations",
            "synchronizes with browser activity",       # browser_wait
            "browser_assert verifies",
            "not ordinary observation",                 # browser_capture_evidence
            "for failures",                              # browser_diagnose
            "never invent refs, browsersessionid values, urls",
        ]:
            self.assertIn(phrase, instructions, f"missing guidance: {phrase!r}")

    def test_get_tool_handlers_maps_all_eight(self):
        tools, _client, _sid = bind_tools()
        handlers = bat.get_tool_handlers(tools)
        self.assertEqual(sorted(handlers), sorted([
            "browser_observe", "browser_act", "browser_navigate", "browser_wait",
            "browser_assert", "browser_capture_evidence", "browser_diagnose", "browser_tabs",
        ]))
        for name, fn in handlers.items():
            self.assertEqual(fn.__name__, name)

    def test_dispatch_rejects_unknown_tool(self):
        tools, _client, session_id = bind_tools()
        result = tools.dispatch("browser_teleport", {"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.UNKNOWN_TOOL)

    def test_dispatch_rejects_raw_rpc_method_as_tool_name(self):
        tools, _client, session_id = bind_tools()
        result = tools.dispatch("locator.clickRef", {"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.UNKNOWN_TOOL)

    def test_dispatch_routes_to_correct_handler(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: compact_rpc_result())
        result = tools.dispatch("browser_observe", {"browserSessionId": session_id})
        self.assertTrue(result["ok"])
        self.assertIn("observationId", result["data"])

    def test_get_tool_definitions_returns_an_independent_copy(self):
        # Regression test: a caller (e.g. an SDK that annotates tool dicts
        # before sending them to a model) must not be able to corrupt the
        # schema for every other caller/session by mutating what it got back.
        first = bat.get_tool_definitions()
        first[0]["function"]["name"] = "hijacked"
        first[0]["function"]["parameters"]["properties"]["browserSessionId"]["description"] = "corrupted"
        second = bat.get_tool_definitions()
        names = [d["function"]["name"] for d in second]
        self.assertIn("browser_observe", names)
        self.assertNotIn("hijacked", names)
        self.assertNotEqual(
            second[0]["function"]["parameters"]["properties"]["browserSessionId"]["description"],
            "corrupted",
        )


# ---------------------------------------------------------------------------
# Forbidden-method / positive-allowlist protection
# ---------------------------------------------------------------------------

class TestForbiddenMethodProtection(unittest.TestCase):
    def test_forbidden_methods_are_never_in_allowed_method_set(self):
        self.assertEqual(bat.FORBIDDEN_METHODS & bat._ALLOWED_METHODS, set())

    def test_assert_method_allowed_blocks_forbidden_methods(self):
        tools, _client, _sid = bind_tools()
        for method in ("cookies.get", "page.executeJavaScript", "page.waitForFunction",
                       "policy.set", "extension.reload", "network.setInterceptors"):
            with self.assertRaises(bat.BridgeMethodNotAllowedError):
                tools._assert_method_allowed(method)

    def test_computer_methods_are_always_blocked(self):
        # There is no coordinate-fallback opt-in at all: computer.* is
        # simply absent from _ALLOWED_METHODS, so it is unreachable
        # regardless of configuration — no public schema accepts
        # coordinates and no internal mapping needs computer.*.
        tools, _client, _sid = bind_tools()
        for method in ("computer.click", "computer.scroll", "computer.type", "computer.drag"):
            with self.assertRaises(bat.BridgeMethodNotAllowedError):
                tools._assert_method_allowed(method)
        self.assertFalse(any(m.startswith("computer.") for m in bat._ALLOWED_METHODS))

    def test_browser_agent_tools_constructor_has_no_coordinate_fallback_flag(self):
        # Regression test: the dormant allow_coordinate_fallback configuration
        # path has been removed entirely, not just defaulted off.
        import inspect
        params = inspect.signature(bat.BrowserAgentTools.__init__).parameters
        self.assertNotIn("allow_coordinate_fallback", params)
        self.assertFalse(hasattr(bind_tools()[0], "allow_coordinate_fallback"))

    def test_assert_method_allowed_rejects_methods_absent_from_the_allowlist(self):
        # Regression test: _ALLOWED_METHODS must be a real positive
        # allowlist. A method that is neither mapped by any tool nor
        # explicitly forbidden (e.g. a typo, or a method added to the bridge
        # after this module was written) must still be rejected rather than
        # passing through by default.
        tools, _client, _sid = bind_tools()
        for method in ("totally.unknown", "locator.select", "page.someFutureMethod"):
            with self.assertRaises(bat.BridgeMethodNotAllowedError):
                tools._assert_method_allowed(method)

    def test_locator_select_is_not_a_real_bridge_method(self):
        # locator.select does not exist in the bridge (only
        # locator.selectOption/selectOptionRef do) and must not appear in
        # the allowlist.
        self.assertNotIn("locator.select", bat._ALLOWED_METHODS)

    def test_all_seven_tool_mapping_tables_are_subset_of_allowed_methods(self):
        mapped = set(bat.OBSERVE_METHOD_MAP.values()) | set(bat.NAV_METHOD_MAP.values()) \
            | set(bat.WAIT_METHOD_MAP.values()) | set(bat.ASSERT_METHOD_MAP.values()) \
            | set(bat.CAPTURE_METHOD_MAP.values()) | set(bat.DIAGNOSTIC_METHOD_MAP.values()) \
            | set(bat.ACT_REF_METHODS.values()) | set(bat.ACT_LOCATOR_METHODS.values()) | {bat.ACT_SCROLL_METHOD}
        self.assertEqual(mapped - bat._ALLOWED_METHODS, set())
        self.assertEqual(mapped & bat.FORBIDDEN_METHODS, set())


# ---------------------------------------------------------------------------
# Managed-session binding
# ---------------------------------------------------------------------------

class TestSessionBinding(unittest.TestCase):
    def test_bind_active_managed_tab_success_hides_raw_tab_id_from_tools(self):
        tools, _client, session_id = bind_tools()
        # Opaque handle minting is now unified with the tab registry
        # (browser_tabs uses the same _register_tab path), so every handle
        # uses the "tab-" prefix regardless of which entry point minted it.
        self.assertTrue(session_id.startswith("tab-"))
        record = tools.tab_registry[session_id]
        self.assertEqual(record["tabId"], 456)
        self.assertEqual(record["bridgeSessionId"], "bridge-session-1")
        # Tool schemas never accept tabId/bridgeSessionId as input.
        for definition in bat.get_tool_definitions():
            self.assertNotIn("tabId", definition["function"]["parameters"]["properties"])
            self.assertNotIn("bridgeSessionId", definition["function"]["parameters"]["properties"])

    def test_bind_active_managed_tab_calls_tabs_list_with_focus_query(self):
        # Regression test (strict/tab-group-isolation mode — the fast-path
        # unscoped tabs.list is rejected here, as it would be by a bridge
        # with unscopedTabAccess explicitly turned off): binding must ask
        # the bridge which tab is actually focused (active tab of the
        # last-focused window), not just proxy "most recently updated
        # managed session".
        def rpc(method, params=None, **_):
            if method == "tabs.list" and "groupId" not in (params or {}).get("query", {}):
                raise BrowserBridgeError("Access denied: tabs.list requires query.groupId for an Agent-managed tab group")
            return default_bind_rpc(method, params)

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertTrue(result["ok"], result)

        tabs_list_calls = [c for c in client.rpc.call_args_list if c[0][0] == "tabs.list"]
        self.assertEqual(len(tabs_list_calls), 2)  # rejected unscoped attempt + the groupId-scoped one
        query = tabs_list_calls[-1][0][1]["query"]
        self.assertEqual(query["groupId"], 5)
        self.assertIs(query["active"], True)
        self.assertIs(query["lastFocusedWindow"], True)

    def test_bind_active_managed_tab_uses_the_whole_browser_fast_path_by_default(self):
        # The bridge's default (unscopedTabAccess: true) lets tabs.list be
        # queried directly for the focused tab with no groupId — binding
        # should use that in one round trip and never even call
        # session.list/session.get.
        def rpc(method, params=None, **_):
            if method == "tabs.list":
                query = (params or {}).get("query", {})
                if query.get("active") is True and query.get("lastFocusedWindow") is True and "groupId" not in query:
                    return {"tabs": [{"id": 42, "windowId": 1, "url": "https://unmanaged.example/"}]}
                raise AssertionError(f"unexpected tabs.list query: {query}")
            raise AssertionError(f"unexpected rpc call in fast-path bind: {method}")

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["url"], "https://unmanaged.example/")
        record = tools.tab_registry[result["browserSessionId"]]
        self.assertEqual(record["tabId"], 42)
        self.assertEqual(client.rpc.call_count, 1)

    def test_bind_active_managed_tab_returns_only_public_fields(self):
        # bind_active_managed_tab() itself must not hand tabId/windowId/
        # groupId/bridgeSessionId back to the caller — only browserSessionId
        # and the public url. The full record still lives in
        # self.tab_registry for internal use.
        client = make_client(rpc_side_effect=default_bind_rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["data"]), {"browserSessionId", "url"})
        self.assertNotIn("tabId", result["data"])
        self.assertNotIn("windowId", result["data"])
        self.assertNotIn("groupId", result["data"])
        self.assertNotIn("bridgeSessionId", result["data"])

    def test_binds_the_focused_tab_only_when_it_belongs_to_a_managed_session(self):
        # Two managed sessions/groups exist, but Chrome's actually-focused
        # tab (per tabs.list active+lastFocusedWindow) is in group 20, not
        # group 5 or 10 — only that one may be bound.
        sessions_payload = {
            "sessions": [
                {"id": "session-a", "groupId": 5, "mainTabId": 456},
                {"id": "session-b", "groupId": 20, "mainTabId": 900},
            ],
        }
        session_details = {
            "session-a": {"session": {"id": "session-a", "groupId": 5}, "tabs": []},
            "session-b": {"session": {"id": "session-b", "groupId": 20}, "tabs": []},
        }

        def rpc(method, params=None, **_):
            if method == "session.list":
                return sessions_payload
            if method == "session.get":
                return session_details[params["sessionId"]]
            if method == "tabs.list":
                query = params["query"]
                if "groupId" not in query:
                    # Strict mode: the fast-path unscoped query is rejected.
                    raise BrowserBridgeError("Access denied: tabs.list requires query.groupId for an Agent-managed tab group")
                if query["groupId"] == 20:
                    return {"tabs": [{"id": 900, "windowId": 2, "groupId": 20, "url": "https://focused.example/"}]}
                return {"tabs": []}
            raise AssertionError(method)

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertTrue(result["ok"], result)
        record = tools.tab_registry[result["browserSessionId"]]
        self.assertEqual(record["tabId"], 900)
        self.assertEqual(record["bridgeSessionId"], "session-b")
        self.assertEqual(result["data"]["url"], "https://focused.example/")

    def test_focused_unmanaged_tab_produces_no_managed_tab(self):
        # A managed session/group exists, but tabs.list confirms the
        # currently focused tab is not that group's active tab — it must
        # never fall back to the session's mainTabId or "most recently
        # updated" as a substitute for genuine focus confirmation.
        def rpc(method, params=None, **_):
            if method == "session.list":
                return MANAGED_SESSION_LIST
            if method == "session.get":
                return MANAGED_SESSION_GET
            if method == "tabs.list":
                return {"tabs": []}  # focused tab is not in this managed group
            raise AssertionError(method)

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.NO_MANAGED_TAB)
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(tools.tab_registry, {})

    def test_bind_active_managed_tab_never_falls_back_to_main_tab_id(self):
        # Even though session.get reports a mainTabId, if tabs.list does not
        # confirm that tab as the focused one, binding must not silently use
        # mainTabId as a substitute.
        def rpc(method, params=None, **_):
            if method == "session.list":
                return MANAGED_SESSION_LIST
            if method == "session.get":
                return MANAGED_SESSION_GET  # mainTabId=456
            if method == "tabs.list":
                return {"tabs": []}
            raise AssertionError(method)

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.NO_MANAGED_TAB)

    def test_bind_active_managed_tab_no_managed_session(self):
        client = make_client(rpc_side_effect=lambda method, params=None, **_: {"sessions": []})
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.NO_MANAGED_TAB)
        self.assertTrue(result["error"]["retryable"])

    def test_bind_active_managed_tab_skips_session_with_no_group_id(self):
        # A tab-based (groupless) managed session cannot be checked against
        # tabs.list's groupId-scoped focus query, so it is skipped rather
        # than guessed at.
        def rpc(method, params=None, **_):
            if method == "tabs.list" and "groupId" not in (params or {}).get("query", {}):
                raise BrowserBridgeError("Access denied: tabs.list requires query.groupId for an Agent-managed tab group")
            if method == "session.list":
                return {"sessions": [{"id": "tabbed-session", "tabIds": [1]}]}
            if method == "session.get":
                return {"session": {"id": "tabbed-session", "groupId": None, "tabIds": [1]}, "tabs": []}
            raise AssertionError(method)

        client = make_client(rpc_side_effect=rpc)
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.NO_MANAGED_TAB)

    def test_bind_active_managed_tab_bridge_unavailable(self):
        client = make_client(rpc_side_effect=BrowserBridgeError("Connection refused"))
        tools = bat.BrowserAgentTools(client)
        result = tools.bind_active_managed_tab()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_UNAVAILABLE)

    def test_tool_call_without_binding_returns_tab_not_found(self):
        client = make_client()
        tools = bat.BrowserAgentTools(client)
        result = tools.browser_observe({"browserSessionId": "bs-does-not-exist"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.TAB_NOT_FOUND)


# ---------------------------------------------------------------------------
# browser_observe: observation/ref state
# ---------------------------------------------------------------------------

class TestBrowserObserve(unittest.TestCase):
    def test_compact_observe_mints_refs_and_hides_bridge_ref_format(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        self.assertIn("ref_1", data["snapshot"])
        self.assertIn("ref_2", data["snapshot"])
        self.assertNotIn("f0:e1", data["snapshot"])
        self.assertEqual(data["snapshotId"], "snap-1")
        self.assertEqual(data["url"], "https://application.example.com/")
        self.assertFalse(data["truncated"])

        obs = tools.observations[data["observationId"]]
        # Ref shape is deliberately minimal: just enough to resolve a bridge
        # RPC. No per-ref checked-state tracking (removed along with
        # ref-based check/uncheck — see TestBrowserAct.test_check_and_uncheck_*).
        self.assertEqual(obs["refs"]["ref_1"], {"frameId": 0, "bridgeRef": "e1"})
        self.assertEqual(obs["refs"]["ref_2"], {"frameId": 0, "bridgeRef": "e2"})
        self.assertEqual(obs["snapshotId"], "snap-1")
        self.assertFalse(obs["stale"])
        client.rpc.assert_called_once()
        method = client.rpc.call_args[0][0]
        self.assertEqual(method, "page.accessibilityTree")

    def test_newer_observation_invalidates_the_previous_one(self):
        # Regression test: an older observation must stop being usable the
        # moment a newer browser_observe call is made for the same session,
        # even though the underlying page/tab did not navigate in between.
        tools, client, session_id = bind_tools()
        first = observe_compact(tools, client, session_id)
        self.assertFalse(tools.observations[first["observationId"]]["stale"])

        second = observe_compact(tools, client, session_id)
        self.assertNotEqual(first["observationId"], second["observationId"])
        self.assertTrue(tools.observations[first["observationId"]]["stale"])
        self.assertFalse(tools.observations[second["observationId"]]["stale"])

        stale_act = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": first["observationId"], "ref": "ref_1",
        })
        self.assertFalse(stale_act["ok"])
        self.assertEqual(stale_act["error"]["code"], bat.STALE_OBSERVATION)

        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        fresh_act = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": second["observationId"], "ref": "ref_1",
        })
        self.assertTrue(fresh_act["ok"], fresh_act)

    def test_aria_and_text_modes_produce_no_refs(self):
        tools, client, session_id = bind_tools(
            rpc_side_effect=lambda method, params=None, **_: {"snapshot": "- button \"Submit\""})
        result = tools.browser_observe({"browserSessionId": session_id, "mode": "aria"})
        self.assertTrue(result["ok"])
        obs_id = result["data"]["observationId"]
        self.assertEqual(tools.observations[obs_id]["refs"], {})

    def test_observe_rejects_unknown_mode(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_observe({"browserSessionId": session_id, "mode": "video"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_observe_rejects_additional_properties(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_observe({"browserSessionId": session_id, "bogus": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_stale_ref_after_navigation_is_rejected(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"tab": {"url": "https://application.example.com/next", "title": "Next"}}
        nav_result = tools.browser_navigate({"browserSessionId": session_id, "operation": "reload"})
        self.assertTrue(nav_result["ok"])

        act_result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertFalse(act_result["ok"])
        self.assertEqual(act_result["error"]["code"], bat.STALE_OBSERVATION)

    def test_unknown_ref_is_ref_not_found(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_99",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.REF_NOT_FOUND)

    def test_missing_observation_id_with_ref_is_stale_observation(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_act({"browserSessionId": session_id, "action": "click", "ref": "ref_1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.STALE_OBSERVATION)


# ---------------------------------------------------------------------------
# browser_act: action mappings, one-action enforcement, fallbacks
# ---------------------------------------------------------------------------

class TestBrowserAct(unittest.TestCase):
    def _observed(self, tools, client, session_id, **overrides):
        return observe_compact(tools, client, session_id, **overrides)

    def test_click_uses_ref_based_method_when_ref_supplied(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True, "whatChanged": {"focusChanged": False}}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        self.assertTrue(result["ok"])
        method, params = client.rpc.call_args[0][0], client.rpc.call_args[0][1]
        self.assertEqual(method, "locator.clickRef")
        self.assertEqual(params["ref"], "e2")
        self.assertEqual(params["frameId"], 0)
        self.assertEqual(params["snapshotId"], "snap-1")
        # Regression test: a11yDiff must never be requested on a ref action.
        # wrapWithActionObserver (extension/sw/action-observer.js) only
        # captures a pre-action accessibility tree when a11yDiff is
        # truthy, and doing so calls page.accessibilityTree, which mints
        # and installs a *new* snapshotId server-side (content/
        # accessibility-tree.js's refState.snapshotId) before this ref
        # action's own (now-superseded) snapshotId ever arrives — the
        # content script then rejects it as stale
        # ("Stale accessibility ref snapshot: ..."). Sending a11yDiff:True
        # would make every ref-based action fail against this bridge.
        self.assertNotIn("a11yDiff", params)
        self.assertNotIn("tabId", bat.BROWSER_ACT_PARAMS["properties"])  # tabId not LLM-selectable

    def test_no_ref_action_ever_sends_a11y_diff(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        cases = [
            {"action": "click", "ref": "ref_2"},
            {"action": "fill", "ref": "ref_1", "value": "hi"},
            {"action": "press", "ref": "ref_1", "key": "Enter"},
            {"action": "hover", "ref": "ref_1"},
            {"action": "select", "ref": "ref_1", "options": ["a"]},
        ]
        for extra in cases:
            args = {"browserSessionId": session_id, "observationId": data["observationId"], **extra}
            result = tools.browser_act(args)
            self.assertTrue(result["ok"], result)
            params = client.rpc.call_args[0][1]
            self.assertNotIn("a11yDiff", params, f"a11yDiff leaked into {extra['action']} params")
            self.assertEqual(params["frameId"], 0)
            self.assertEqual(params["snapshotId"], "snap-1")

    def test_click_falls_back_to_locator_when_no_ref(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: {"ok": True})
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "locator": {"role": "button", "name": "Submit"},
        })
        self.assertTrue(result["ok"])
        method, params = client.rpc.call_args[0][0], client.rpc.call_args[0][1]
        self.assertEqual(method, "locator.click")
        self.assertEqual(params["locator"], {"role": "button", "name": "Submit"})

    def test_click_requires_ref_or_locator(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_act({"browserSessionId": session_id, "action": "click"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_fill_requires_string_value(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "fill",
            "locator": {"selector": "#u"},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_fill_ref_mapping(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "fill", "value": "hello",
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertTrue(result["ok"])
        method, params = client.rpc.call_args[0][0], client.rpc.call_args[0][1]
        self.assertEqual(method, "locator.fillRef")
        self.assertEqual(params["text"], "hello")

    def test_press_requires_key(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "press",
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_hover_ref_mapping(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "hover",
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "locator.hoverRef")

    def test_hover_without_ref_requires_css_selector(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "hover",
            "locator": {"role": "button", "name": "Menu"},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_hover_without_ref_uses_dom_hover_with_selector(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: {"ok": True})
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "hover",
            "locator": {"selector": "#menu"},
        })
        self.assertTrue(result["ok"])
        method, params = client.rpc.call_args[0][0], client.rpc.call_args[0][1]
        self.assertEqual(method, "dom.hover")
        self.assertEqual(params["selector"], "#menu")

    def test_select_requires_non_empty_options(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "select",
            "locator": {"selector": "#choice"}, "options": [],
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_select_ref_mapping(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "select", "options": ["a"],
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "locator.selectOptionRef")

    def test_check_and_uncheck_reject_ref(self):
        # Regression test: the bridge has no deterministic checkRef/
        # uncheckRef. locator.clickRef unconditionally toggles, which is
        # nondeterministic against a possibly-stale observation or a live
        # application change, so ref-based check/uncheck must be refused
        # outright rather than attempted — never a click, never a no-op.
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id, snapshot='[f0:e1] checkbox "Accept" [unchecked]')
        client.rpc.reset_mock()
        for action in ("check", "uncheck"):
            result = tools.browser_act({
                "browserSessionId": session_id, "action": action,
                "observationId": data["observationId"], "ref": "ref_1",
            })
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)
            self.assertIn("locator", result["error"]["message"])
        client.rpc.assert_not_called()

    def test_check_and_uncheck_locator_use_dedicated_methods(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: {"ok": True})
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "check",
            "locator": {"selector": "#agree"},
        })
        self.assertEqual(client.rpc.call_args[0][0], "locator.check")
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "uncheck",
            "locator": {"selector": "#agree"},
        })
        self.assertEqual(client.rpc.call_args[0][0], "locator.uncheck")

    def test_upload_requires_locator_and_files_and_rejects_ref(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        # ref is not usable for upload
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "upload", "files": ["/tmp/a.png"],
            "observationId": data["observationId"], "ref": "ref_1",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)
        # missing locator
        result = tools.browser_act({"browserSessionId": session_id, "action": "upload", "files": ["/tmp/a.png"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)
        # missing files
        result = tools.browser_act({"browserSessionId": session_id, "action": "upload", "locator": {"selector": "input"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)
        # success path
        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "upload",
            "files": ["/tmp/a.png"], "locator": {"selector": "input[type=file]"},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "locator.setInputFiles")

    def test_drag_requires_locator_and_target_locator(self):
        tools, client, session_id = bind_tools()
        result = tools.browser_act({"browserSessionId": session_id, "action": "drag", "locator": {"selector": "#src"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

        client.rpc.side_effect = lambda method, params=None, **_: {"ok": True}
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "drag",
            "locator": {"selector": "#src"}, "targetLocator": {"selector": "#dst"},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "locator.dragTo")

    def test_scroll_uses_dom_scroll_with_deltas(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: {"ok": True})
        result = tools.browser_act({"browserSessionId": session_id, "action": "scroll", "deltaX": 0, "deltaY": 400})
        self.assertTrue(result["ok"])
        method, params = client.rpc.call_args[0][0], client.rpc.call_args[0][1]
        self.assertEqual(method, "dom.scroll")
        self.assertEqual(params["y"], 400)
        self.assertEqual(params["mode"], "scrollBy")

    def test_coordinate_fallback_never_selected_by_default(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda method, params=None, **_: {"ok": True})
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "locator": {"role": "button", "name": "X"},
        })
        self.assertTrue(result["ok"])
        self.assertFalse(client.rpc.call_args[0][0].startswith("computer."))

    def test_whatchanged_url_change_invalidates_observations(self):
        # Regression test: the bridge reports a navigation via
        # whatChanged.urlChanged/.toUrl (extension/sw/action-observer.js),
        # not whatChanged.url — an action-triggered navigation (e.g.
        # clicking a link) must still be detected and invalidate refs.
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {
            "ok": True,
            "whatChanged": {
                "urlChanged": True,
                "fromUrl": "https://application.example.com/",
                "toUrl": "https://application.example.com/after",
            },
        }
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["observationInvalidated"])
        self.assertTrue(tools.observations[data["observationId"]]["stale"])
        self.assertEqual(tools.tab_registry[session_id]["url"], "https://application.example.com/after")

    def test_whatchanged_without_url_change_does_not_invalidate(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {
            "ok": True, "whatChanged": {"focusChanged": True, "focusedElement": {"ref": "ref_2"}},
        }
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["observationInvalidated"])
        self.assertFalse(tools.observations[data["observationId"]]["stale"])

    def test_stale_accessibility_snapshot_error_becomes_stale_observation(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = BrowserBridgeError("Stale accessibility ref snapshot: snap_abc123")
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.STALE_OBSERVATION)
        self.assertTrue(result["error"]["retryable"])
        self.assertIn("browser_observe", result["error"]["message"])

    def test_ref_not_found_or_stale_error_becomes_stale_observation(self):
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.side_effect = BrowserBridgeError("Accessibility ref not found or stale: e2")
        result = tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.STALE_OBSERVATION)
        self.assertTrue(result["error"]["retryable"])

    def test_stale_ref_error_does_not_retry_the_old_ref(self):
        # The tool must surface STALE_OBSERVATION and stop — it must never
        # automatically retry the RPC with the same (rejected) ref.
        tools, client, session_id = bind_tools()
        data = self._observed(tools, client, session_id)
        client.rpc.reset_mock()
        client.rpc.side_effect = BrowserBridgeError("Stale accessibility ref snapshot: snap_abc123")
        tools.browser_act({
            "browserSessionId": session_id, "action": "click",
            "observationId": data["observationId"], "ref": "ref_2",
        })
        client.rpc.assert_called_once()


# ---------------------------------------------------------------------------
# browser_navigate
# ---------------------------------------------------------------------------

class TestBrowserNavigate(unittest.TestCase):
    def test_open_requires_url(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_navigate({"browserSessionId": session_id, "operation": "open"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_all_navigation_mappings(self):
        expected = {
            "open": "page.navigate", "reload": "page.reload",
            "back": "page.goBack", "forward": "page.goForward",
        }
        for operation, method in expected.items():
            tools, client, session_id = bind_tools(
                rpc_side_effect=lambda m, params=None, **_: {"tab": {"url": "https://x/", "title": "X"}})
            args = {"browserSessionId": session_id, "operation": operation}
            if operation == "open":
                args["url"] = "https://x/"
            result = tools.browser_navigate(args)
            self.assertTrue(result["ok"], result)
            self.assertEqual(client.rpc.call_args[0][0], method)

    def test_navigate_invalidates_prior_observations(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        client.rpc.side_effect = lambda method, params=None, **_: {"tab": {"url": "https://x/", "title": "X"}}
        result = tools.browser_navigate({"browserSessionId": session_id, "operation": "reload"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["observationsInvalidated"])
        self.assertTrue(tools.observations[data["observationId"]]["stale"])


# ---------------------------------------------------------------------------
# browser_wait
# ---------------------------------------------------------------------------

class TestBrowserWait(unittest.TestCase):
    def test_all_condition_mappings(self):
        expected = {
            "element_state": "locator.waitFor",
            "url": "page.waitForURL",
            "navigation": "page.waitForNavigation",
            "page_load": "page.waitForLoad",
            "network_idle": "page.waitForNetworkIdle",
            "text": "page.waitForText",
            "popup": "page.waitForPopup",
            "dialog": "page.waitForDialog",
            "request": "page.waitForRequest",
            "response": "page.waitForResponse",
        }
        for condition, method in expected.items():
            tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"ok": True})
            args = {"browserSessionId": session_id, "condition": condition}
            if condition == "element_state":
                args["locator"] = {"selector": "#x"}
            if condition in ("url", "request", "response", "text"):
                args["expected"] = "value"
            result = tools.browser_wait(args)
            self.assertTrue(result["ok"], result)
            self.assertEqual(client.rpc.call_args[0][0], method)

    def test_element_state_requires_locator(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "element_state"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_url_condition_requires_expected(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "url"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_wait_for_function_is_never_used(self):
        for condition in ["element_state", "url", "navigation", "page_load", "network_idle",
                           "text", "popup", "dialog", "request", "response"]:
            self.assertNotEqual(bat.WAIT_METHOD_MAP[condition], "page.waitForFunction")
        self.assertNotIn("page.waitForFunction", bat._ALLOWED_METHODS)

    def test_wait_timeout_is_classified_as_timeout_error(self):
        tools, client, session_id = bind_tools(
            rpc_side_effect=BrowserBridgeError("Timed out waiting for locator to be visible"))
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "navigation"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.TIMEOUT)
        self.assertTrue(result["error"]["retryable"])

    def test_confirmed_navigation_wait_invalidates_observations(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        client.rpc.side_effect = lambda m, params=None, **_: {"ok": True, "url": "https://application.example.com/next"}
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "navigation"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["observationsInvalidated"])
        self.assertTrue(tools.observations[data["observationId"]]["stale"])
        self.assertEqual(tools.tab_registry[session_id]["url"], "https://application.example.com/next")

    def test_url_condition_wait_invalidates_observations(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        client.rpc.side_effect = lambda m, params=None, **_: {"ok": True, "url": "https://application.example.com/next"}
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "url", "expected": "/next"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["observationsInvalidated"])
        self.assertTrue(tools.observations[data["observationId"]]["stale"])

    def test_page_load_wait_does_not_invalidate_observations(self):
        tools, client, session_id = bind_tools()
        data = observe_compact(tools, client, session_id)
        client.rpc.side_effect = lambda m, params=None, **_: {"ok": True}
        result = tools.browser_wait({"browserSessionId": session_id, "condition": "page_load"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["observationsInvalidated"])
        self.assertFalse(tools.observations[data["observationId"]]["stale"])


# ---------------------------------------------------------------------------
# browser_assert
# ---------------------------------------------------------------------------

class TestBrowserAssert(unittest.TestCase):
    def test_all_assertion_mappings(self):
        expected = {
            "visible": "expect.locator.toBeVisible",
            "hidden": "expect.locator.toBeHidden",
            "enabled": "expect.locator.toBeEnabled",
            "disabled": "expect.locator.toBeDisabled",
            "editable": "expect.locator.toBeEditable",
            "checked": "expect.locator.toBeChecked",
            "value": "expect.locator.toHaveValue",
            "text": "expect.locator.toHaveText",
            "count": "expect.locator.toHaveCount",
            "attribute": "expect.locator.toHaveAttribute",
            "title": "expect.page.toHaveTitle",
            "aria_snapshot": "expect.page.toMatchAriaSnapshot",
        }
        for assertion, method in expected.items():
            tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"ok": True})
            args = {"browserSessionId": session_id, "assertion": assertion}
            if assertion in bat.LOCATOR_ASSERTIONS:
                args["locator"] = {"selector": "#x"}
            if assertion in ("value", "text", "title", "aria_snapshot"):
                args["expected"] = "hello"
            if assertion == "attribute":
                args["attribute"] = "disabled"
                args["expected"] = "true"
            if assertion == "count":
                args["count"] = 3
            result = tools.browser_assert(args)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["data"]["passed"])
            self.assertEqual(client.rpc.call_args[0][0], method)

    def test_locator_assertion_requires_locator(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_assert({"browserSessionId": session_id, "assertion": "visible"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_failed_assertion_is_ok_true_with_passed_false(self):
        tools, client, session_id = bind_tools(
            rpc_side_effect=BrowserBridgeError("Timed out waiting for locator #x to be visible"))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "visible", "locator": {"selector": "#x"},
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["passed"])
        self.assertIn("diagnostic", result["data"])

    def test_transport_failure_during_assertion_is_ok_false(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("Connection refused"))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "visible", "locator": {"selector": "#x"},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_UNAVAILABLE)

    def test_count_assertion_requires_integer_count(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "count",
            "locator": {"selector": "#x"}, "count": "three",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_scope_or_permission_error_is_ok_false_not_passed_false(self):
        # Regression test: a security/infrastructure error surfaced by the
        # bridge as a coded failure (e.g. a tab-group scope rejection) must
        # never be reported as a successful, false assertion outcome.
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "Access denied: expect.locator.toBeVisible is limited to tabs in Agent-managed tab groups",
            data={"code": "TAB_NOT_ALLOWED"},
        ))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "visible", "locator": {"selector": "#x"},
        })
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["error"]["code"], None)
        self.assertIn(result["error"]["code"], (bat.BRIDGE_ERROR, bat.BRIDGE_UNAVAILABLE, bat.TIMEOUT))

    def test_actionability_and_strict_mode_errors_are_ok_false(self):
        # LOCATOR_STRICT_MODE_VIOLATION / LOCATOR_REF_NOT_ACTIONABLE-style
        # coded errors are real bridge/DOM problems, not "assertion is
        # false" — they must not be swallowed into passed:false either.
        for code in ("LOCATOR_STRICT_MODE_VIOLATION", "LOCATOR_ACTIONABILITY_TIMEOUT", "LOCATOR_REF_NOT_ACTIONABLE"):
            tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
                f"{code} while evaluating locator", data={"code": code}))
            result = tools.browser_assert({
                "browserSessionId": session_id, "assertion": "visible", "locator": {"selector": "#x"},
            })
            self.assertFalse(result["ok"], f"{code} should not be reported as ok=true")

    def test_permission_denied_message_with_no_code_is_ok_false(self):
        # assertTabAllowed-style rejections (extension/service-worker.js)
        # throw a plain Error with no .code at all.
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "Access denied: expect.locator.toBeVisible is limited to tabs in Agent-managed tab groups"))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "visible", "locator": {"selector": "#x"},
        })
        self.assertFalse(result["ok"])

    def test_invalid_parameter_error_is_ok_false(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "expect.locator.toHaveValue requires expectedValue, expected, or value"))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "value", "locator": {"selector": "#x"}, "expected": "x",
        })
        self.assertFalse(result["ok"])

    def test_locator_expect_timeout_code_is_the_only_mismatch_path(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "Timed out after 5000ms expecting locator #x toHaveValue expected: \"y\" actual: \"z\"",
            data={"code": "LOCATOR_EXPECT_TIMEOUT", "diagnostic": {"expected": "y", "actual": "z", "elapsedMs": 5000}},
        ))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "value", "locator": {"selector": "#x"}, "expected": "y",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["passed"])
        self.assertEqual(result["data"]["expected"], "y")

    def test_page_expect_title_timeout_is_ok_true_passed_false(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "expect.page.toHaveTitle timed out after 5000ms: title did not match (current=\"Home\")",
            data={"code": "PAGE_EXPECT_TITLE_TIMEOUT", "diagnostic": {"actual": "Home", "expected": "Dashboard", "elapsedMs": 5000}},
        ))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "title", "expected": "Dashboard",
        })
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["passed"])
        # The expectation echoed back is the title the caller supplied, not
        # something derived from the internal 'title'/'titleContains' bridge
        # param (which never has an 'expected' key at all).
        self.assertEqual(result["data"]["expected"], "Dashboard")
        self.assertEqual(result["data"]["diagnostic"]["actual"], "Home")

    def test_page_expect_aria_snapshot_timeout_is_ok_true_passed_false(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "expect.page.toMatchAriaSnapshot timed out after 5000ms: aria snapshot did not match",
            data={"code": "PAGE_EXPECT_ARIA_SNAPSHOT_TIMEOUT", "diagnostic": {"missing": ["- button \"Submit\""], "elapsedMs": 5000}},
        ))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "aria_snapshot", "expected": "- button \"Submit\"",
        })
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["passed"])
        self.assertEqual(result["data"]["expected"], "- button \"Submit\"")
        self.assertIn("missing", result["data"]["diagnostic"])

    def test_title_assertion_echoes_the_supplied_title_on_pass(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"ok": True, "title": "Dashboard"})
        result = tools.browser_assert({"browserSessionId": session_id, "assertion": "title", "expected": "Dashboard"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["passed"])
        self.assertEqual(result["data"]["expected"], "Dashboard")

    def test_count_assertion_echoes_the_requested_count_on_pass(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"ok": True, "count": 3})
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "count", "locator": {"selector": "#x"}, "count": 3,
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["passed"])
        self.assertEqual(result["data"]["expected"], 3)

    def test_mismatch_diagnostic_is_bounded_and_redacted(self):
        # Preserve useful bridge assertion diagnostics (actual/expected
        # value, elapsed time, ...) rather than discarding them, but never
        # leak a sensitive-keyed field or an unbounded payload.
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError(
            "Timed out expecting locator toHaveAttribute",
            data={
                "code": "LOCATOR_EXPECT_TIMEOUT",
                "diagnostic": {
                    "attribute": "data-token",
                    "expected": "abc",
                    "actual": "def",
                    "elapsedMs": 3000,
                    "sessionToken": "super-secret-value",
                    "candidates": ["x" * 100 for _ in range(500)],
                },
            },
        ))
        result = tools.browser_assert({
            "browserSessionId": session_id, "assertion": "attribute",
            "locator": {"selector": "#x"}, "attribute": "data-token", "expected": "abc",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["passed"])
        diagnostic = result["data"]["diagnostic"]
        self.assertEqual(diagnostic["expected"], "abc")
        self.assertEqual(diagnostic["actual"], "def")
        self.assertEqual(diagnostic["elapsedMs"], 3000)
        # 'sessionToken' is a sensitive-shaped key name and must be
        # redacted by value; the candidate list must be size-bounded.
        self.assertEqual(diagnostic["sessionToken"], bat.REDACTED)
        self.assertNotIn("super-secret-value", str(diagnostic))
        self.assertLessEqual(len(diagnostic["candidates"]), bat.MAX_LIST_ITEMS + 1)


# ---------------------------------------------------------------------------
# browser_capture_evidence
# ---------------------------------------------------------------------------

class TestBrowserCaptureEvidence(unittest.TestCase):
    def test_native_save_data_url_is_routed_through_an_explicit_allowlist(self):
        # native.saveDataUrl bypasses BrowserBridgeClient.rpc() entirely (it
        # goes through the client's dedicated save_data_url() helper), so it
        # never passes through _assert_method_allowed(). _save_data_url()
        # must therefore check its own allowlist and refuse anything that
        # isn't a genuine data: URL rather than trusting the caller.
        tools, client, session_id = bind_tools()
        saved, error = tools._save_data_url(session_id, "https://not-a-data-url.example/x", "out.png")
        self.assertIsNone(saved)
        self.assertIsNotNone(error)
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], bat.INVALID_ARGUMENT)
        client.save_data_url.assert_not_called()

        client.save_data_url.return_value = {"path": "/tmp/out.png", "bytes": 4, "mimeType": "image/png"}
        saved, error = tools._save_data_url(session_id, "data:image/png;base64,AAAA", "out.png")
        self.assertIsNone(error)
        self.assertEqual(saved["path"], "/tmp/out.png")
        client.save_data_url.assert_called_once()

    def test_save_text_data_url_matches_the_native_hosts_regex(self):
        # Regression test (found by manual testing against a live bridge):
        # native/host.py parses the data URL with
        # DATA_URL_RE = r'^data:([^;,]+)?(;base64)?,(.*)$' — the mime group
        # cannot contain a ';', and only one literal ';base64' segment is
        # allowed before the comma. _save_text() previously built
        # 'data:{mime};charset=utf-8;base64,{data}', which that regex does
        # not match at all, so the native host rejected every
        # dom_snapshot/accessibility_snapshot/trace_export save with
        # "Invalid data URL" even though _save_data_url()'s own data:
        # prefix check passed. Assert the actual regex accepts what we build.
        import re
        host_pattern = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.DOTALL)

        tools, client, session_id = bind_tools()
        client.save_data_url.return_value = {"path": "/tmp/out.json", "bytes": 2, "mimeType": "application/json"}
        tools._save_text(session_id, "{}", "out.json", "application/json")

        client.save_data_url.assert_called_once()
        data_url = client.save_data_url.call_args[0][0]
        match = host_pattern.match(data_url)
        self.assertIsNotNone(match, f"{data_url!r} does not match the native host's DATA_URL_RE")
        self.assertEqual(match.group(1), "application/json")
        self.assertEqual(match.group(2), ";base64")
        self.assertNotIn("charset", data_url)

    def test_all_capture_mappings_and_data_url_saved(self):
        expected = {
            "page_screenshot": "page.screenshot",
            "dom_snapshot": "page.domSnapshot",
            "accessibility_snapshot": "page.accessibilityTree",
            "pdf": "page.pdf",
            "trace_start": "trace.start",
        }
        for capture, method in expected.items():
            def rpc(m, params=None, **_):
                if capture == "pdf":
                    return {"dataUrl": "data:application/pdf;base64,AAAA", "mimeType": "application/pdf"}
                if capture == "page_screenshot":
                    return {"dataUrl": "data:image/png;base64,AAAA"}
                if capture == "trace_start":
                    return {"trace": {"id": "trace-1"}}
                return {"tree": {}, "snapshotId": "s1"} if capture == "dom_snapshot" else {"snapshot": "- root"}

            tools, client, session_id = bind_tools(rpc_side_effect=rpc)
            client.save_data_url.return_value = {"path": "/tmp/out.bin", "bytes": 4, "mimeType": "application/octet-stream"}
            result = tools.browser_capture_evidence({"browserSessionId": session_id, "capture": capture})
            self.assertTrue(result["ok"], result)
            self.assertEqual(client.rpc.call_args[0][0], method)
            self.assertNotIn("dataUrl", result["data"])
            self.assertNotIn("base64", str(result["data"]))

    def test_element_screenshot_requires_locator(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_capture_evidence({"browserSessionId": session_id, "capture": "element_screenshot"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_element_screenshot_uses_locator_screenshot(self):
        tools, client, session_id = bind_tools(
            rpc_side_effect=lambda m, params=None, **_: {"dataUrl": "data:image/png;base64,AAAA"})
        client.save_data_url.return_value = {"path": "/tmp/e.png", "bytes": 4, "mimeType": "image/png"}
        result = tools.browser_capture_evidence({
            "browserSessionId": session_id, "capture": "element_screenshot",
            "locator": {"selector": "#hero"},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "locator.screenshot")

    def test_trace_stop_and_export_require_trace_id(self):
        tools, _client, session_id = bind_tools()
        for capture in ("trace_stop", "trace_export"):
            result = tools.browser_capture_evidence({"browserSessionId": session_id, "capture": capture})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_save_to_disk_false_never_returns_base64(self):
        tools, client, session_id = bind_tools(
            rpc_side_effect=lambda m, params=None, **_: {"dataUrl": "data:image/png;base64,AAAABBBBCCCC"})
        result = tools.browser_capture_evidence({
            "browserSessionId": session_id, "capture": "page_screenshot", "saveToDisk": False,
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["saved"])
        self.assertNotIn("AAAABBBBCCCC", str(result["data"]))
        client.save_data_url.assert_not_called()

    def test_unsafe_filename_rejected(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_capture_evidence({
            "browserSessionId": session_id, "capture": "page_screenshot", "filename": "../../etc/passwd",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)


# ---------------------------------------------------------------------------
# browser_diagnose
# ---------------------------------------------------------------------------

class TestBrowserDiagnose(unittest.TestCase):
    def test_all_diagnostic_mappings(self):
        expected = {
            "console": "console.read", "network": "network.read", "dom": "page.domSnapshot",
            "trace_status": "trace.status",
        }
        for diagnostic, method in expected.items():
            tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"events": [], "tree": {}})
            result = tools.browser_diagnose({"browserSessionId": session_id, "diagnostic": diagnostic})
            self.assertTrue(result["ok"], result)
            self.assertEqual(client.rpc.call_args[0][0], method)

    def test_response_body_blocked_by_default(self):
        tools, _client, session_id = bind_tools()
        result = tools.browser_diagnose({
            "browserSessionId": session_id, "diagnostic": "response_body", "requestId": "req-1",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.SENSITIVE_OPERATION_BLOCKED)

    def test_response_body_requires_request_id_even_when_enabled(self):
        tools, _client, session_id = bind_tools(allow_response_body=True)
        result = tools.browser_diagnose({"browserSessionId": session_id, "diagnostic": "response_body"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.INVALID_ARGUMENT)

    def test_response_body_allowed_when_enabled(self):
        tools, client, session_id = bind_tools(
            allow_response_body=True,
            rpc_side_effect=lambda m, params=None, **_: {"requestId": "req-1", "base64Encoded": False, "body": "{}"})
        result = tools.browser_diagnose({
            "browserSessionId": session_id, "diagnostic": "response_body", "requestId": "req-1",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(client.rpc.call_args[0][0], "network.getResponseBody")

    def test_console_and_network_events_are_redacted(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {
            "events": [{"headers": {"Authorization": "Bearer super-secret-token"}, "message": "ok"}],
        })
        result = tools.browser_diagnose({"browserSessionId": session_id, "diagnostic": "network"})
        self.assertTrue(result["ok"])
        payload = str(result["data"])
        self.assertNotIn("super-secret-token", payload)
        self.assertIn(bat.REDACTED, payload)

    def test_console_events_are_bounded(self):
        many_events = [{"message": f"line {i}"} for i in range(bat.MAX_DIAGNOSTIC_LIMIT + 50)]
        tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: {"events": many_events})
        result = tools.browser_diagnose({"browserSessionId": session_id, "diagnostic": "console"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["truncated"])
        self.assertLessEqual(len(result["data"]["events"]), bat.MAX_DIAGNOSTIC_LIMIT)


# ---------------------------------------------------------------------------
# Scope provider
# ---------------------------------------------------------------------------

class TestScopeProvider(unittest.TestCase):
    def test_whole_browser_provider_allows_a_registered_tab(self):
        provider = bat.WholeBrowserScopeProvider()
        decision = provider.authorize({"browserSessionId": "tab-1", "tabId": 5})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policy_id, "whole-browser")

    def test_whole_browser_provider_denies_incomplete_context(self):
        provider = bat.WholeBrowserScopeProvider()
        decision = provider.authorize({"browserSessionId": None, "tabId": None})
        self.assertFalse(decision.allowed)

    def test_managed_tab_group_scope_provider_is_a_compatibility_alias(self):
        # Old imports referencing the tab-group-only name still work, and
        # behave exactly like WholeBrowserScopeProvider now.
        self.assertIs(bat.ManagedTabGroupScopeProvider, bat.WholeBrowserScopeProvider)

    def test_external_firewall_provider_is_unimplemented(self):
        provider = bat.ExternalFirewallScopeProvider()
        with self.assertRaises(NotImplementedError):
            provider.authorize({})

    def test_custom_scope_provider_can_deny_and_is_checked_before_rpc(self):
        class DenyEverything(bat.ScopeProvider):
            def authorize(self, context):
                return bat.ScopeDecision(False, "denied for test", "test-policy")

        tools, client, session_id = bind_tools(scope_provider=DenyEverything())
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.SCOPE_DENIED)
        client.rpc.assert_not_called()

    def test_scope_provider_receives_expected_context_fields(self):
        captured = {}

        class Capturing(bat.ScopeProvider):
            def authorize(self, context):
                captured.update(context)
                return bat.ScopeDecision(True, None, "capture")

        tools, client, session_id = bind_tools(
            scope_provider=Capturing(), rpc_side_effect=lambda m, params=None, **_: compact_rpc_result())
        tools.browser_observe({"browserSessionId": session_id})
        for key in ("semanticTool", "bridgeMethod", "browserSessionId", "bridgeSessionId", "tabId", "url", "action", "requestSummary"):
            self.assertIn(key, captured)
        self.assertEqual(captured["semanticTool"], "browser_observe")
        self.assertEqual(captured["bridgeMethod"], "page.accessibilityTree")


# ---------------------------------------------------------------------------
# Common bridge-error responses
# ---------------------------------------------------------------------------

class TestBridgeErrorClassification(unittest.TestCase):
    def test_transport_error_is_bridge_unavailable(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("[Errno 111] Connection refused"))
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_UNAVAILABLE)
        self.assertTrue(result["error"]["retryable"])

    def test_http_401_and_403_are_not_bridge_unavailable(self):
        for status in (401, 403):
            tools, client, session_id = bind_tools(
                rpc_side_effect=BrowserBridgeError(f"HTTP {status}: {{\"error\": \"unauthorized\"}}"))
            result = tools.browser_observe({"browserSessionId": session_id})
            self.assertFalse(result["ok"])
            self.assertNotEqual(result["error"]["code"], bat.BRIDGE_UNAVAILABLE)
            self.assertEqual(result["error"]["code"], bat.BRIDGE_ERROR)
            self.assertFalse(result["error"]["retryable"])

    def test_http_500_is_bridge_unavailable(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("HTTP 500: internal error"))
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_UNAVAILABLE)

    def test_http_400_is_bridge_error_not_unavailable(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("HTTP 400: bad request"))
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_ERROR)

    def test_timeout_message_is_timeout(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("Timed out waiting for tab to load"))
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.TIMEOUT)

    def test_generic_application_error_is_bridge_error(self):
        tools, client, session_id = bind_tools(rpc_side_effect=BrowserBridgeError("Element not actionable"))
        result = tools.browser_observe({"browserSessionId": session_id})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], bat.BRIDGE_ERROR)
        self.assertFalse(result["error"]["retryable"])

    def test_response_envelope_shape_on_success_and_failure(self):
        tools, client, session_id = bind_tools(rpc_side_effect=lambda m, params=None, **_: compact_rpc_result())
        ok_result = tools.browser_observe({"browserSessionId": session_id})
        self.assertEqual(set(ok_result), {"ok", "browserSessionId", "data", "error"})
        self.assertIsNone(ok_result["error"])

        bad_result = tools.browser_observe({"browserSessionId": "nope"})
        self.assertEqual(set(bad_result), {"ok", "browserSessionId", "data", "error"})
        self.assertIsNone(bad_result["data"])
        self.assertEqual(set(bad_result["error"]), {"code", "message", "retryable", "diagnostic"})


if __name__ == "__main__":
    unittest.main()
