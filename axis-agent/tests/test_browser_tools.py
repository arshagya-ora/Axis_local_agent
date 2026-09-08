import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_core import ValidationError
from pydantic_ai.tools import Tool


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import browser_tools as bt  # noqa: E402


LEGACY_SHA256 = "88781bc3ca2812b4e06c96672e489517a548c2283e8eb97faee8c1ca4e0a6c19"

TOOL_FUNCTIONS = (
    bt.browser_tabs,
    bt.browser_observe,
    bt.browser_act,
    bt.browser_navigate,
    bt.browser_wait,
    bt.browser_assert,
    bt.browser_capture_evidence,
    bt.browser_diagnose,
)


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.saved = []

    def rpc(self, method, params=None, **_):
        self.calls.append((method, params or {}))
        response = self.responses.get(method, {})
        return response() if callable(response) else response

    def save_data_url(self, data_url, filename=None, directory=None):
        self.saved.append((data_url, filename, directory))
        return {"path": f"artifacts/{filename or 'artifact'}", "bytes": len(data_url)}


def context(runtime):
    return SimpleNamespace(deps=runtime)


def runtime_with_tab(url="https://example.test/"):
    bridge = FakeBridge()
    runtime = bt.BrowserRuntime(bridge)
    alias = runtime._register({"id": 41, "url": url, "title": "Fixture", "active": True})
    return bridge, runtime, alias


def test_all_eight_functions_are_independently_selectable():
    names = [function.__name__ for function in TOOL_FUNCTIONS]
    assert names == [
        "browser_tabs", "browser_observe", "browser_act", "browser_navigate",
        "browser_wait", "browser_assert", "browser_capture_evidence", "browser_diagnose",
    ]
    selected = [bt.browser_tabs, bt.browser_observe, bt.browser_navigate]
    assert [Tool(function, takes_ctx=True).name for function in selected] == [
        "browser_tabs", "browser_observe", "browser_navigate",
    ]


def test_pydantic_generates_bounded_schemas_without_internal_identifiers():
    forbidden = {
        "browserSessionId", "observationId", "snapshotId", "frameId", "tabId",
        "windowId", "groupId", "sessionId", "bridgeMethod", "method",
    }
    schemas = []
    for function in TOOL_FUNCTIONS:
        schema = Tool(function, takes_ctx=True).tool_def.parameters_json_schema
        schemas.append(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        serialized = json.dumps(schema)
        assert not any(f'"{name}"' in serialized for name in forbidden)
        assert "page.executeJavaScript" not in serialized
        assert "tabs.list" not in serialized
    assert "oneOf" in json.dumps(Tool(bt.browser_act, takes_ctx=True).tool_def.parameters_json_schema)
    assert len(json.dumps(schemas)) < 25_000


def test_pydantic_rejects_an_incomplete_action_before_the_function_runs():
    bridge, runtime, alias = runtime_with_tab()
    tool = Tool(bt.browser_act, takes_ctx=True)
    with pytest.raises(ValidationError):
        tool.function_schema.validator.validate_python({
            "tab": alias,
            "command": {
                "action": "fill",
                "target": {"kind": "locator", "locator": {"label": "Search"}},
            },
        })
    assert bridge.calls == []


def test_stateful_validation_rejects_bad_hover_before_rpc():
    bridge, runtime, alias = runtime_with_tab()
    result = bt.browser_act(context(runtime), alias, bt.Hover(
        action="hover",
        target=bt.LocatorTarget(kind="locator", locator=bt.Locator(role="button", name="Menu")),
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert bridge.calls == []


def test_observation_hides_bridge_refs_and_navigation_makes_them_stale():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.accessibilityTree"] = {
        "snapshotId": "raw-snapshot-9",
        "snapshot": '[f7:bridge-node-3] textbox "Search"',
        "url": "https://example.test/",
        "title": "Fixture",
    }
    observed = bt.browser_observe(context(runtime), alias)
    assert observed["ok"] is True
    assert observed["data"]["snapshot"] == '[ref_1] textbox "Search"'
    assert "raw-snapshot" not in json.dumps(observed)
    assert "bridge-node" not in json.dumps(observed)

    bridge.responses["page.reload"] = {"tab": {"url": "https://example.test/"}}
    navigated = bt.browser_navigate(context(runtime), alias, bt.History(operation="reload"))
    assert navigated["ok"] is True
    calls_before_stale_action = len(bridge.calls)

    stale = bt.browser_act(context(runtime), alias, bt.Click(
        action="click", target=bt.RefTarget(kind="ref", ref="ref_1"),
    ))
    assert stale["ok"] is False
    assert stale["error"]["code"] == "STALE_REFERENCE"
    assert len(bridge.calls) == calls_before_stale_action


def test_current_ref_resolves_only_inside_runtime():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.accessibilityTree"] = {
        "snapshotId": "snap-secret",
        "snapshot": '[f2:node-secret] button "Go"',
    }
    bt.browser_observe(context(runtime), alias)
    bt.browser_act(context(runtime), alias, bt.Click(
        action="click", target=bt.RefTarget(kind="ref", ref="ref_1"),
    ))
    method, params = bridge.calls[-1]
    assert method == "locator.clickRef"
    assert params["tabId"] == 41
    assert params["frameId"] == 2
    assert params["snapshotId"] == "snap-secret"
    assert params["ref"] == "node-secret"


def test_firewall_blocks_urls_and_methods_before_rpc():
    bridge, runtime, alias = runtime_with_tab()
    denied = bt.browser_navigate(
        context(runtime), alias, bt.Open(operation="open", url="javascript:alert(1)"),
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "FIREWALL_DENIED"
    assert bridge.calls == []

    with pytest.raises(bt.ToolFault, match="not allowed"):
        runtime._rpc("page.executeJavaScript", {"tabId": 41}, tab=alias)
    assert bridge.calls == []


def test_diagnostic_output_is_redacted_and_bounded():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["console.read"] = {
        "events": [
            {"message": "x" * (bt.MAX_TEXT + 50), "authorization": "Bearer secret"},
            *({"message": f"event-{i}"} for i in range(249)),
        ]
    }
    result = bt.browser_diagnose(
        context(runtime), alias, bt.BufferedDiagnostic(diagnostic="console", limit=500),
    )
    events = result["data"]["events"]
    assert result["ok"] is True
    assert events[0]["authorization"] == "<redacted>"
    assert len(events[0]["message"]) < bt.MAX_TEXT + 100
    assert len(events) == bt.MAX_ITEMS
    assert result["data"]["truncated"] is True


def test_sensitive_diagnostics_and_unsafe_artifacts_never_reach_bridge():
    bridge, runtime, alias = runtime_with_tab()
    blocked = bt.browser_diagnose(
        context(runtime), alias,
        bt.ResponseBodyDiagnostic(diagnostic="response_body", request="request_1"),
    )
    assert blocked["error"]["code"] == "SENSITIVE_OPERATION_BLOCKED"

    unsafe = bt.browser_capture_evidence(
        context(runtime), alias,
        bt.PageCapture(capture="page_screenshot", filename="../escape.png"),
    )
    assert unsafe["error"]["code"] == "INVALID_ARGUMENT"
    assert bridge.calls == []
    assert bridge.saved == []


def test_new_module_is_substantially_smaller_and_legacy_file_is_unchanged():
    legacy = AGENT_DIR / "browser_agent_tools.py"
    new = AGENT_DIR / "browser_tools.py"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == LEGACY_SHA256
    assert len(new.read_text(encoding="utf-8").splitlines()) < len(legacy.read_text(encoding="utf-8").splitlines()) // 2
