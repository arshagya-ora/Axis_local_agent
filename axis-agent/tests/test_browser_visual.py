"""Visual recovery uses bounded private images and single-use bridge leases."""

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.tools import Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_tools as bt
from browser_bridge_client import BrowserBridgeError


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def rpc(self, method, params=None):
        self.calls.append((method, params))
        value = self.responses.get(method, {})
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def visual():
    bridge = FakeBridge()
    runtime = bt.BrowserRuntime(bridge, allow_coordinate_fallback=True)
    runtime.visual_enabled = True
    tab = runtime._register({"id": 41, "url": "https://example.test/", "active": True})
    bridge.responses["page.screenshot"] = {
        "tabId": 41, "screenshotId": "raw-bridge-shot", "url": "https://example.test/",
        "viewport": {"width": 2400, "height": 1500, "deviceScaleFactor": 2,
                     "scale": 1, "offsetX": 0, "offsetY": 0},
        "scroll": {"x": 0, "y": 120}, "image": {"width": 1600, "height": 1000},
        "dataUrl": "data:image/png;base64," + base64.b64encode(b"private-image-pixels").decode(),
    }
    return bridge, runtime, tab, SimpleNamespace(deps=runtime)


def capture(visual):
    _, _, tab, ctx = visual
    result = bt.browser_visual(ctx, tab, bt.VisualCapture(operation="capture"))
    assert result["ok"], result
    return result


def test_capture_delivers_private_binary_image_without_serialized_bytes(visual):
    bridge, runtime, _, _ = visual
    result = capture(visual)
    image = runtime.visual_content(result["data"]["screenshot"])
    assert isinstance(image, BinaryContent)
    assert image.data == b"private-image-pixels"
    assert image.media_type == "image/png"
    serialized = json.dumps(result)
    assert "dataUrl" not in serialized and "base64" not in serialized
    assert "raw-bridge-shot" not in serialized and "tabId" not in serialized
    assert bridge.calls[-1] == ("page.screenshot", {"tabId": 41, "modelFacing": True})


@pytest.mark.parametrize("operation", ["click", "hover"])
def test_coordinates_scale_to_css_and_bridge_receives_live_freshness_contract(visual, operation):
    bridge, runtime, tab, ctx = visual
    screenshot = capture(visual)["data"]["screenshot"]
    result = bt.browser_visual(ctx, tab, bt.VisualPoint(operation=operation, screenshot=screenshot, x=800, y=200))
    assert result["ok"] and result["data"]["refsInvalidated"]
    method, params = bridge.calls[-1]
    assert method == f"computer.{operation}"
    assert (params["x"], params["y"]) == (1200, 300)
    assert params["screenshotId"] == "raw-bridge-shot"
    assert params["expectedVisualState"]["scroll"] == {"x": 0, "y": 120}
    assert params["expectedVisualState"]["tabId"] == 41
    with pytest.raises(bt.ToolFault, match="fresh screenshot"):
        runtime.visual_content(screenshot)
    calls = len(bridge.calls)
    repeat = bt.browser_visual(ctx, tab, bt.VisualPoint(operation=operation, screenshot=screenshot, x=800, y=200))
    assert repeat["error"]["code"] == "STALE_SCREENSHOT"
    assert len(bridge.calls) == calls


def test_drag_scroll_and_type_translate_only_image_positions(visual):
    bridge, _, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    assert bt.browser_visual(ctx, tab, bt.VisualDrag(operation="drag", screenshot=shot, x=100, y=50, end_x=200, end_y=100))["ok"]
    params = bridge.calls[-1][1]
    assert (params["fromX"], params["fromY"], params["toX"], params["toY"]) == (150, 75, 300, 150)
    shot = capture(visual)["data"]["screenshot"]
    assert bt.browser_visual(ctx, tab, bt.VisualScroll(operation="scroll", screenshot=shot, x=10, y=20, delta_y=300))["ok"]
    assert bridge.calls[-1][1]["deltaY"] == 300
    assert bridge.calls[-1][1]["x"] == 15
    shot = capture(visual)["data"]["screenshot"]
    assert bt.browser_visual(ctx, tab, bt.VisualType(operation="type", screenshot=shot, value="hello"))["ok"]
    assert bridge.calls[-1][0] == "computer.type"
    assert bridge.calls[-1][1]["text"] == "hello"


def test_latest_image_and_same_tab_are_required(visual):
    bridge, runtime, tab, ctx = visual
    old = capture(visual)["data"]["screenshot"]
    current = capture(visual)["data"]["screenshot"]
    other = runtime._register({"id": 42, "url": "https://example.test/other"})
    calls = len(bridge.calls)
    for target, screenshot in [(tab, old), (other, current)]:
        result = bt.browser_visual(ctx, target, bt.VisualPoint(operation="click", screenshot=screenshot, x=1, y=1))
        assert result["error"]["code"] == "STALE_SCREENSHOT"
    assert len(bridge.calls) == calls


@pytest.mark.parametrize("x,y", [(1600, 100), (100, 1000), (1599, 1001)])
def test_out_of_image_coordinates_rejected_without_consuming_capture(visual, x, y):
    bridge, runtime, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    calls = len(bridge.calls)
    result = bt.browser_visual(ctx, tab, bt.VisualPoint(operation="click", screenshot=shot, x=x, y=y))
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert len(bridge.calls) == calls
    assert runtime.visual_content(shot)


@pytest.mark.parametrize("enabled,coordinate,expected", [
    (False, True, "VISUAL_DISABLED"), (True, False, "COORDINATE_FALLBACK_DISABLED"),
])
def test_runtime_requires_visual_unlock_and_coordinate_configuration(visual, enabled, coordinate, expected):
    bridge, runtime, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    runtime.visual_enabled, runtime.allow_coordinate_fallback = enabled, coordinate
    calls = len(bridge.calls)
    result = bt.browser_visual(ctx, tab, bt.VisualPoint(operation="click", screenshot=shot, x=1, y=1))
    assert result["error"]["code"] == expected
    assert len(bridge.calls) == calls


def test_visual_firewall_denial_sends_no_input(visual):
    bridge, runtime, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    runtime.deny_methods = ("computer.*",)
    calls = len(bridge.calls)
    result = bt.browser_visual(ctx, tab, bt.VisualPoint(operation="click", screenshot=shot, x=1, y=1))
    assert result["error"]["code"] == "FIREWALL_DENIED"
    assert len(bridge.calls) == calls


def test_bridge_rejection_consumes_local_image_and_hides_bridge_id(visual):
    bridge, runtime, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    bridge.responses["computer.click"] = BrowserBridgeError("Viewport changed", data={
        "code": "STALE_VISUAL_STATE", "screenshotId": "raw-bridge-shot", "tabId": 41,
    })
    result = bt.browser_visual(ctx, tab, bt.VisualPoint(operation="click", screenshot=shot, x=1, y=1))
    assert result["error"]["code"] == "STALE_VISUAL_STATE"
    assert "raw-bridge-shot" not in json.dumps(result)
    with pytest.raises(bt.ToolFault):
        runtime.visual_content(shot)


@pytest.mark.parametrize("change", ["url", "image", "tab", "data"])
def test_invalid_captures_never_retain_pixels(visual, change):
    bridge, runtime, tab, ctx = visual
    payload = bridge.responses["page.screenshot"]
    if change == "url":
        payload["url"] = "chrome://settings/"
    elif change == "image":
        payload["image"]["width"] = 2000
    elif change == "tab":
        payload["tabId"] = 42
    else:
        payload["dataUrl"] = "data:image/png;base64,not base64"
    result = bt.browser_visual(ctx, tab, bt.VisualCapture(operation="capture"))
    assert not result["ok"]
    assert runtime._visual_screenshot is None


def test_semantically_identical_observation_keeps_capture_but_changed_content_invalidates(visual):
    bridge, runtime, tab, _ = visual
    bridge.responses["page.accessibilityTree"] = {
        "snapshot": '[f0:first] button "Save"', "snapshotId": "first",
        "url": "https://example.test/", "scroll": {"x": 0, "y": 120},
    }
    runtime.observe(tab)
    shot = capture(visual)["data"]["screenshot"]
    bridge.responses["page.accessibilityTree"]["snapshot"] = '[f0:second] button   "Save"'
    runtime.observe(tab)
    assert runtime.visual_content(shot)
    bridge.responses["page.accessibilityTree"]["snapshot"] = '[f0:third] button "Submitted"'
    runtime.observe(tab)
    with pytest.raises(bt.ToolFault):
        runtime.visual_content(shot)


@pytest.mark.parametrize("operation", ["click", "navigate"])
def test_ordinary_interactions_invalidate_visual_lease(visual, operation):
    _, runtime, tab, ctx = visual
    shot = capture(visual)["data"]["screenshot"]
    if operation == "navigate":
        result = bt.browser_navigate(ctx, tab, bt.Open(operation="open", url="https://example.test/next"))
    else:
        result = bt.browser_act(ctx, tab, bt.Click(action="click", target=bt.LocatorTarget(
            kind="locator", locator=bt.Locator(role="button", name="Save"),
        )))
    assert result["ok"]
    with pytest.raises(bt.ToolFault):
        runtime.visual_content(shot)


def test_visual_schema_has_no_bridge_identifiers_or_raw_methods():
    schema = json.dumps(Tool(bt.browser_visual, takes_ctx=True).tool_def.parameters_json_schema)
    assert "screenshotId" not in schema and "tabId" not in schema and "computer.click" not in schema
    assert "oneOf" in schema


def task_download(runtime, **changes):
    return {
        "id": 123, "filename": "report.csv", "state": "complete", "exists": True,
        "startTime": datetime.fromtimestamp(runtime.task_started_wallclock + 1, timezone.utc).isoformat(),
        **changes,
    }


def test_download_started_before_wait_but_during_task_is_accepted(visual):
    bridge, runtime, _, ctx = visual
    runtime.task_started_wallclock -= 60
    bridge.responses["downloads.waitFor"] = {"item": task_download(runtime)}
    result = bt.browser_downloads(ctx, bt.WaitForDownload(operation="wait", filename_contains="report"))
    assert result["ok"] and result["data"]["download"]["taskEligible"]
    assert bridge.calls[-1][1]["startedAfter"] == int(runtime.task_started_wallclock * 1000)
    assert "id" not in result["data"]["download"]


@pytest.mark.parametrize("changes", [
    {"state": "interrupted"}, {"state": "in_progress"}, {"exists": False},
    {"startTime": "2020-01-01T00:00:00Z"}, {"startTime": None}, {"filename": "unrelated.csv"},
])
def test_download_wait_rejects_incomplete_old_unrelated_or_missing_file(visual, changes):
    bridge, runtime, _, ctx = visual
    bridge.responses["downloads.waitFor"] = {"item": task_download(runtime, **changes)}
    result = bt.browser_downloads(ctx, bt.WaitForDownload(operation="wait", filename_contains="report", include_existing=True))
    assert result["error"]["code"] == "DOWNLOAD_NOT_MATCHED"


def test_empty_download_wait_does_not_match(visual):
    _, _, _, ctx = visual
    result = bt.browser_downloads(ctx, bt.WaitForDownload(operation="wait"))
    assert result["error"]["code"] == "DOWNLOAD_NOT_MATCHED"


@pytest.mark.parametrize("value", [0, False, "", [], {}, None])
def test_locator_extraction_preserves_empty_scalar_and_structured_values(visual, value):
    bridge, _, tab, ctx = visual
    bridge.responses["locator.textContent"] = value
    result = bt.browser_observe(ctx, tab, locator=bt.Locator(selector="#result"), extract="text")
    assert result["data"]["result"] == value
    assert type(result["data"]["result"]) is type(value)
