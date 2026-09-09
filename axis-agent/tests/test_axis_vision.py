"""Visual recovery integration through real Pydantic AI messages and gates."""

import base64
import json
import sys
from pathlib import Path

import pytest
from pydantic_ai import BinaryContent, RunContext, RunUsage, ToolFailed, ToolReturn
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_tools as bt
from axis.agents import AxisDeps, StepGate, build_navigator, build_planner, guarded
from axis.models import AxisConfig
from axis.orchestrator import AxisOrchestrator
from browser_bridge_client import BrowserBridgeError
from test_axis_agent import FakeBridge, LOCATOR_CLICK, Script, browse, done
from test_browser_visual import visual  # Reuse the private-image bridge/runtime fixture.

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def image_parts(messages):
    return [
        item
        for message in messages
        for part in message.parts
        if getattr(part, "part_kind", None) == "user-prompt" and not isinstance(part.content, str)
        for item in part.content
        if isinstance(item, BinaryContent)
    ]


class ObservedScript(Script):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.navigator_messages = []
        self.navigator_tool_names = []

    def _respond(self, messages, info):
        if info.function_tools:
            self.navigator_messages.append(messages)
            self.navigator_tool_names.append({tool.name for tool in info.function_tools})
        return super()._respond(messages, info)


def vision_bridge():
    bridge = FakeBridge()
    bridge.responses["page.screenshot"] = {
        "tabId": 41, "screenshotId": "private-bridge-shot", "url": bridge.url,
        "viewport": {"width": 1200, "height": 800, "deviceScaleFactor": 1,
                     "scale": 1, "offsetX": 0, "offsetY": 0},
        "scroll": {"x": 0, "y": 0}, "image": {"width": 1200, "height": 800},
        "dataUrl": "data:image/png;base64," + base64.b64encode(b"private-vision-pixels").decode(),
    }
    return bridge


def make_vision(script, *, config=None, bridge=None):
    config = config or AxisConfig.load()
    bridge = bridge or vision_bridge()
    events, rebuilt = [], []
    model = script.model()

    def rebuild(**options):
        rebuilt.append(options)
        selected = config.model_copy(deep=True)
        selected.tools.capture_evidence = options.get("capture", False)
        selected.tools.diagnose = options.get("diagnose", False)
        selected.tools.downloads = options.get("downloads", False)
        selected.tools.visual = options.get("visual", False)
        return build_navigator(selected, model)

    runtime = bt.BrowserRuntime(bridge, allow_coordinate_fallback=config.browser.allow_coordinate_fallback)
    orchestrator = AxisOrchestrator(
        browser=runtime, planner=build_planner(config, model), navigator=build_navigator(config, model),
        navigator_factory=rebuild, config=config, on_event=events.append,
    )
    return orchestrator, bridge, events, rebuilt


async def test_needs_visual_enables_optional_tool_and_actual_image_on_next_step():
    script = ObservedScript([browse(), done()], [
        {"outcome": {"status": "continue", "needs_visual": True, "summary": "DOM lacks canvas content"}},
        {"outcome": {"status": "goal_reached", "summary": "Read the canvas"}},
    ])
    orchestrator, bridge, events, rebuilt = make_vision(script)
    result = await orchestrator.run("Read the chart on the current page.")
    assert result.status == "completed"
    assert "browser_visual" not in script.navigator_tool_names[0]
    assert "browser_visual" in script.navigator_tool_names[1]
    assert not image_parts(script.navigator_messages[0])
    images = image_parts(script.navigator_messages[1])
    assert len(images) == 1 and images[0].data == b"private-vision-pixels"
    assert images[0].media_type == "image/png"
    assert bridge.methods.count("page.screenshot") == 1
    assert any(item.get("visual") is True for item in rebuilt)
    assert orchestrator.state.last_browser_state.screenshot == "screenshot_1"
    # Logs and persisted task state carry metadata/aliases, never pixel payloads.
    serialized = orchestrator.state.model_dump_json() + json.dumps([event.model_dump() for event in events]) + result.model_dump_json()
    assert "private-vision-pixels" not in serialized
    assert base64.b64encode(b"private-vision-pixels").decode() not in serialized
    assert "dataUrl" not in serialized and "private-bridge-shot" not in serialized


async def test_two_recoverable_targeting_failures_trigger_visual_recovery():
    script = ObservedScript([browse(), browse(), done()], [
        {"calls": [LOCATOR_CLICK], "outcome": {"status": "continue"}},
        {"calls": [LOCATOR_CLICK], "outcome": {"status": "continue"}},
        {"outcome": {"status": "goal_reached", "summary": "Read visual result"}},
    ])
    bridge = vision_bridge()

    def missing_target():
        raise BrowserBridgeError("Element not actionable", data={"code": "LOCATOR_ACTIONABILITY_TIMEOUT"})

    bridge.responses["locator.click"] = missing_target
    orchestrator, bridge, _, rebuilt = make_vision(script, bridge=bridge)
    result = await orchestrator.run("Read the chart on the current page.")
    assert result.status == "completed"
    assert bridge.methods.count("locator.click") == 2
    assert bridge.methods.count("page.screenshot") == 1
    assert any(image_parts(messages) for messages in script.navigator_messages)
    assert any(item.get("visual") is True for item in rebuilt)


async def test_visual_recovery_cannot_repeatedly_reset_the_failure_budget():
    script = ObservedScript([browse()] * 10, [
        {"calls": [LOCATOR_CLICK], "outcome": {"status": "continue", "needs_visual": True}}
    ] * 10)
    bridge = vision_bridge()

    def missing_target():
        raise BrowserBridgeError("Element not actionable", data={"code": "LOCATOR_ACTIONABILITY_TIMEOUT"})

    bridge.responses["locator.click"] = missing_target
    orchestrator, bridge, _, _ = make_vision(script, bridge=bridge)
    result = await orchestrator.run("Read the current chart.")
    assert result.status == "failed"
    assert bridge.methods.count("locator.click") <= 4
    assert orchestrator.state.counters.consecutive_failures >= 3


async def test_policy_denial_does_not_unlock_visual_workaround():
    script = ObservedScript([browse(), {"decision": "fail", "reason": "Blocked by browser policy"}], [
        {"calls": [LOCATOR_CLICK], "outcome": {"status": "continue", "needs_visual": True}},
    ])
    orchestrator, bridge, _, rebuilt = make_vision(script)
    orchestrator.browser.deny_methods = ("locator.click",)
    result = await orchestrator.run("Try the current page.")
    assert result.status == "failed"
    assert not orchestrator.state.visual_tabs
    assert "page.screenshot" not in bridge.methods
    assert not any(item.get("visual") for item in rebuilt)


@pytest.mark.parametrize("message,code,expected", [
    ("Permission denied by user: page_action", None, "APPROVAL_DENIED"),
    ("Permission denied by user for this session: page_action", None, "APPROVAL_DENIED"),
    ("Access denied: locator.click is limited to Agent-managed tab groups", None, "FIREWALL_DENIED"),
    ("Method blocked by policy: locator.click", None, "FIREWALL_DENIED"),
    ("locator.click blocked by policy for https://www.google.com/", None, "FIREWALL_DENIED"),
    ("The browser policy forbids this interaction", "POLICY_DENIED", "POLICY_DENIED"),
])
async def test_bridge_policy_denials_cannot_activate_visual_recovery(message, code, expected):
    script = ObservedScript([browse(), browse(), {"decision": "fail", "reason": "Policy denied action"}], [
        {"calls": [LOCATOR_CLICK], "outcome": {"status": "continue", "needs_visual": True}},
        {"outcome": {"status": "blocked"}},
    ])
    bridge = vision_bridge()

    def denied():
        raise BrowserBridgeError(message, data={"code": code} if code else None)

    bridge.responses["locator.click"] = denied
    orchestrator, bridge, _, rebuilt = make_vision(script, bridge=bridge)
    result = await orchestrator.run("Read the current page.")
    assert result.status == "failed"
    denied_records = [record for record in orchestrator.state.actions if record.tool == "browser_act"]
    assert len(denied_records) == 1 and denied_records[0].code == expected
    assert not orchestrator.state.visual_tabs
    assert "page.screenshot" not in bridge.methods
    assert not any(item.get("visual") for item in rebuilt)


async def test_automatic_visual_capture_respects_configured_approval():
    config = AxisConfig.load()
    config.tools.visual = True
    config.tools.approval_required_actions = ["capture"]
    script = ObservedScript([browse(), {"decision": "fail", "reason": "Capture denied"}], [
        {"outcome": {"status": "blocked"}},
    ])
    orchestrator, bridge, _, _ = make_vision(script, config=config)
    approvals = []
    orchestrator.approval = lambda *args: approvals.append(args) or False
    result = await orchestrator.run("Read the current page.")
    assert result.status == "failed"
    assert len(approvals) == 1
    assert approvals[0][0:2] == ("browser_visual", "capture")
    assert approvals[0][2]["current_url"] == bridge.url
    assert "page.screenshot" not in bridge.methods
    assert not any(image_parts(messages) for messages in script.navigator_messages)
    assert any(record.code == "APPROVAL_DENIED" and not record.executed for record in orchestrator.state.actions)


async def test_visual_off_disables_images_and_optional_tool_even_when_requested():
    config = AxisConfig.load()
    config.run.visual_mode = "off"
    config.run.include_screenshot = True
    config.tools.visual = True
    script = ObservedScript([browse(), done()], [
        {"outcome": {"status": "continue", "needs_visual": True}},
        {"outcome": {"status": "goal_reached"}},
    ])
    orchestrator, bridge, _, rebuilt = make_vision(script, config=config)
    result = await orchestrator.run("Read the current page.")
    assert result.status == "completed"
    assert not any(image_parts(messages) for messages in script.navigator_messages)
    assert all("browser_visual" not in names for names in script.navigator_tool_names)
    assert "page.screenshot" not in bridge.methods
    assert not any(item.get("visual") for item in rebuilt)


class RejectingImageScript(ObservedScript):
    def __init__(self, *, body="Image input is not supported", status=400, after_tool=False, model_name="fixture-model"):
        super().__init__([browse(), done()], [{"outcome": {"status": "goal_reached"}}])
        self.body, self.status, self.after_tool = body, status, after_tool
        self.model_name = model_name
        self.rejections = 0
        self.sent_tool = False
        self.provider_requests = []

    def _respond(self, messages, info):
        if info.function_tools:
            self.provider_requests.append(bool(image_parts(messages)))
            if self.after_tool and not self.sent_tool:
                self.sent_tool = True
                return ModelResponse(parts=[ToolCallPart("browser_observe", {"tab": "tab_1"})])
            if image_parts(messages):
                self.rejections += 1
                raise ModelHTTPError(status_code=self.status, model_name=self.model_name, body=self.body)
        return super()._respond(messages, info)


@pytest.mark.parametrize("status", [400, 422])
async def test_explicit_provider_image_rejection_retries_once_as_text(status):
    config = AxisConfig.load()
    config.tools.visual = True
    script = RejectingImageScript(status=status)
    orchestrator, bridge, events, rebuilt = make_vision(script, config=config)
    result = await orchestrator.run("Read the current page.")
    assert result.status == "completed"
    assert script.provider_requests == [True, False]
    assert script.rejections == 1
    assert orchestrator.state.vision_disabled and not orchestrator.browser.visual_enabled
    assert result.limitations == ["The configured provider rejected image input; visual recovery was disabled for this run."]
    assert not orchestrator.state.visual_tabs
    assert any(item.get("visual") is False for item in rebuilt)
    assert any(event.detail.get("phase") == "vision_unavailable" for event in events)
    assert bridge.methods.count("page.screenshot") == 1


@pytest.mark.parametrize("body,status,model_name", [
    ("Invalid response schema", 400, "fixture-model"),
    ("Image input is not supported", 500, "fixture-model"),
    ("response_format is not supported", 400, "vision-fixture"),
])
async def test_unrelated_provider_errors_are_not_retried_as_text(body, status, model_name):
    config = AxisConfig.load()
    config.tools.visual = True
    script = RejectingImageScript(body=body, status=status, model_name=model_name)
    orchestrator, _, _, _ = make_vision(script, config=config)
    result = await orchestrator.run("Read the current page.")
    assert result.status == "failed"
    assert script.provider_requests == [True]
    assert not orchestrator.state.vision_disabled


async def test_image_rejection_after_executed_tools_does_not_replay_step():
    config = AxisConfig.load()
    config.tools.visual = True
    script = RejectingImageScript(after_tool=True)
    orchestrator, bridge, _, _ = make_vision(script, config=config)
    result = await orchestrator.run("Read the current page.")
    assert result.status == "failed"
    assert script.provider_requests == [True, True]
    assert script.rejections == 1 and not orchestrator.state.vision_disabled
    assert bridge.methods.count("page.accessibilityTree") == 2  # one auto-read and one executed tool
    assert sum(record.tool == "browser_observe" and record.executed for record in orchestrator.state.actions) == 1


def gate_context(runtime, gate):
    return RunContext(deps=AxisDeps(browser=runtime, gate=gate), model=Script([done()]).model(), usage=RunUsage())


def test_visual_capture_tool_returns_binary_content_outside_safe_envelope(visual):
    bridge, runtime, tab, _ = visual
    gate = StepGate(max_actions=2, remaining_total_actions=2)
    result = guarded(bt.browser_visual)(gate_context(runtime, gate), tab=tab, command=bt.VisualCapture(operation="capture"))
    assert isinstance(result, ToolReturn)
    assert isinstance(result.content[0], BinaryContent)
    assert result.content[0].data == b"private-image-pixels"
    assert result.return_value["data"]["screenshot"] == "screenshot_1"
    assert "dataUrl" not in json.dumps(result.return_value)
    assert "private-image-pixels" not in json.dumps([record.model_dump() for record in gate.records])
    assert gate.actions_used == 1 and bridge.calls[-1][0] == "page.screenshot"


@pytest.mark.parametrize("restriction,expected", [
    ("step_budget", "ACTION_BUDGET_EXHAUSTED"), ("run_budget", "ACTION_BUDGET_EXHAUSTED"),
    ("cancelled", "RUN_CANCELLED"), ("paused", "RUN_PAUSED"),
    ("approval", "APPROVAL_DENIED"), ("readonly", "POLICY_DENIED"),
])
def test_visual_interaction_uses_existing_execution_gates(visual, restriction, expected):
    bridge, runtime, tab, browser_ctx = visual
    captured = bt.browser_visual(browser_ctx, tab, bt.VisualCapture(operation="capture"))
    options = {"max_actions": 2, "remaining_total_actions": 2}
    if restriction == "step_budget":
        options["max_actions"] = 0
    elif restriction == "run_budget":
        options["remaining_total_actions"] = 0
    elif restriction == "cancelled":
        options["is_cancelled"] = lambda: True
    elif restriction == "paused":
        options["is_paused"] = lambda: True
    elif restriction == "approval":
        options.update(approval_actions=frozenset({"click"}), approval=lambda *args: False)
    elif restriction == "readonly":
        options["mutation_allowed"] = False
    gate = StepGate(**options)
    before = len(bridge.calls)
    try:
        result = guarded(bt.browser_visual)(gate_context(runtime, gate), tab=tab, command=bt.VisualPoint(
            operation="click", screenshot=captured["data"]["screenshot"], x=100, y=100,
        ))
        assert result["error"]["code"] == expected
    except ToolFailed:
        assert expected == "POLICY_DENIED"
    assert gate.records[-1].code == expected and not gate.records[-1].executed
    assert len(bridge.calls) == before and gate.actions_used == 0


def test_visual_success_consumes_screenshot_and_interrupts_queued_actions(visual):
    bridge, runtime, tab, browser_ctx = visual
    captured = bt.browser_visual(browser_ctx, tab, bt.VisualCapture(operation="capture"))
    gate = StepGate(max_actions=3, remaining_total_actions=3)
    ctx = gate_context(runtime, gate)
    command = bt.VisualPoint(operation="click", screenshot=captured["data"]["screenshot"], x=100, y=100)
    result = guarded(bt.browser_visual)(ctx, tab=tab, command=command)
    assert result["ok"] and gate.interrupted
    assert gate.effects == [(True, False)]
    assert gate.records[-1].meaningful_change is None  # Lease invalidation is not page progress.
    calls = len(bridge.calls)
    refused = guarded(bt.browser_visual)(ctx, tab=tab, command=command)
    assert refused["error"]["code"] == "STEP_INTERRUPTED"
    assert len(bridge.calls) == calls
    with pytest.raises(bt.ToolFault):
        runtime.visual_content(captured["data"]["screenshot"])
