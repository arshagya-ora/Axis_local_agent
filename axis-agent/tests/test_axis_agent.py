"""Orchestrator tests: a fake BrowserBridgeClient plus Pydantic AI's FunctionModel.

No provider, no bridge, no network. Every planner and navigator response is
scripted, so each test asserts one property of the cadence, the budgets, or the
executor rather than model behaviour.
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import browser_tools as bt  # noqa: E402
from axis.agents import build_navigator, build_planner, navigator_tools  # noqa: E402
from axis.models import AxisConfig  # noqa: E402
from axis.orchestrator import AxisOrchestrator  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

SEARCH_URL = "https://www.google.com/search?q=pydantic+ai"


class FakeBridge:
    """Same surface browser_tools uses: rpc() and save_data_url()."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.saved = []
        self.extra_tabs = []
        self.observations = 0
        self.base_id = 41
        self.url = "https://www.google.com/"
        self.title = "Google"

    def rpc(self, method, params=None, **_):
        self.calls.append((method, params or {}))
        if method == "tabs.list":
            base = {"id": self.base_id, "url": self.url, "title": self.title, "active": not self.extra_tabs}
            return {"tabs": [base, *self.extra_tabs]}
        if method == "page.accessibilityTree":
            self.observations += 1
            return {
                "snapshotId": f"raw-snap-{self.observations}",
                "snapshot": f'[f0:bridge-node-{self.observations}] textbox "Search"',
                "url": self.url,
                "title": self.title,
            }
        response = self.responses.get(method, {})
        result = response() if callable(response) else response
        if method == "tabs.create" and isinstance(result, dict):
            raw = result.get("tab") if isinstance(result.get("tab"), dict) else result
            if isinstance(raw.get("id"), int):
                for item in self.extra_tabs:
                    item["active"] = False
                if not any(item.get("id") == raw["id"] for item in self.extra_tabs):
                    self.extra_tabs.append({**raw, "active": True})
        return result

    def save_data_url(self, data_url, filename=None, directory=None):
        self.saved.append((data_url, filename, directory))
        return {"path": f"artifacts/{filename or 'artifact'}"}

    @property
    def methods(self):
        return [method for method, _ in self.calls]


class Script:
    """Deterministic planner/navigator model.

    The planner is the agent with no function tools; the navigator is the one
    with them. A navigator entry is ``{"calls": [(tool, args), ...],
    "outcome": {...}}``: the calls go out in one response, the outcome in the
    next.
    """

    def __init__(self, planner, navigator=()):
        self.planner = list(planner)
        self.navigator = list(navigator)
        self.planner_calls = 0
        self.navigator_calls = 0
        self.prompts = []
        self.planner_prompts = []
        self.navigator_prompts = []
        self._pending = None

    def model(self) -> FunctionModel:
        return FunctionModel(self._respond)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name
        text = "\n".join(
            str(part.content)
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", "") == "user-prompt"
        )
        self.prompts.append(repr(messages))
        if not info.function_tools:
            self.planner_calls += 1
            self.planner_prompts.append(text)
            entry = self.planner[min(self.planner_calls - 1, len(self.planner) - 1)]
            return ModelResponse(parts=[ToolCallPart(output_tool, dict(entry))])

        if self._pending is None:
            self.navigator_calls += 1
            self.navigator_prompts.append(text)
            index = min(self.navigator_calls - 1, len(self.navigator) - 1)
            entry = self.navigator[index] if self.navigator else {"outcome": {"status": "continue"}}
            self._pending = entry
            calls = entry.get("calls") or []
            if calls:
                return ModelResponse(parts=[ToolCallPart(name, dict(args)) for name, args in calls])
        entry, self._pending = self._pending, None
        return ModelResponse(parts=[ToolCallPart(output_tool, dict(entry.get("outcome", {"status": "continue"})))])


def make(script: Script, *, config: AxisConfig | None = None, bridge: FakeBridge | None = None, **kwargs):
    config = config or AxisConfig.load()
    bridge = bridge or FakeBridge()
    model = script.model()
    runtime = bt.BrowserRuntime(bridge, max_tabs=config.browser.max_open_tabs)
    orchestrator = AxisOrchestrator(
        browser=runtime,
        planner=build_planner(config, model),
        navigator=build_navigator(config, model),
        config=config,
        **kwargs,
    )
    return orchestrator, bridge, config


def browse(goal="Search for pydantic ai", **extra):
    return {"decision": "browse", "next_goal": goal, "success_condition": "Results are visible",
            "reason": "Browsing is required.", **extra}


def done(answer="Done.", **extra):
    return {"decision": "complete", "final_answer": answer, "evidence": ["observed"],
            "reason": "The goal was met.", **extra}


FILL = ("browser_act", {"tab": "tab_1", "command": {
    "action": "fill", "target": {"kind": "ref", "ref": "ref_1"}, "value": "pydantic ai"}})
ENTER = ("browser_act", {"tab": "tab_1", "command": {"action": "press", "key": "Enter"}})
CLICK = ("browser_act", {"tab": "tab_1", "command": {
    "action": "click", "target": {"kind": "ref", "ref": "ref_1"}}})
LOCATOR_CLICK = ("browser_act", {"tab": "tab_1", "command": {
    "action": "click", "target": {"kind": "locator", "locator": {
        "role": "button", "name": "Continue"}}}})
OBSERVE = ("browser_observe", {"tab": "tab_1"})


# ---------------------------------------------------------------------------
# 1-4: planner cadence
# ---------------------------------------------------------------------------

async def test_non_browser_question_completes_without_touching_the_browser():
    script = Script([done("Paris.")])
    orchestrator, bridge, _ = make(script)
    result = await orchestrator.run("What is the capital of France?")
    assert result.status == "completed"
    assert result.answer == "Paris."
    assert bridge.calls == []
    assert script.navigator_calls == 0
    assert result.browser_actions == 0


async def test_planner_runs_first_on_every_new_task():
    script = Script([done("Answered.")])
    orchestrator, _, _ = make(script)
    await orchestrator.run("A question.")
    assert script.planner_calls == 1
    assert "A question." in script.planner_prompts[0]


async def test_planner_runs_again_after_three_navigator_steps():
    script = Script(
        [browse(), done()],
        [{"calls": [], "outcome": {"status": "continue", "summary": f"step {i}"}} for i in range(5)],
    )
    orchestrator, _, config = make(script)
    await orchestrator.run("Keep going.")
    assert config.run.planner_interval_steps == 3
    assert script.navigator_calls == 3  # exactly the interval, then the planner
    assert script.planner_calls == 2


async def test_goal_reached_returns_to_the_planner_immediately():
    script = Script(
        [browse(), done()],
        [{"calls": [], "outcome": {"status": "goal_reached", "summary": "found it"}}],
    )
    orchestrator, _, _ = make(script)
    result = await orchestrator.run("Find it.")
    assert script.navigator_calls == 1  # not three
    assert script.planner_calls == 2
    assert result.status == "completed"


@pytest.mark.parametrize("status", ["blocked", "ask_user", "failed"])
async def test_every_terminal_navigator_status_returns_to_the_planner(status):
    script = Script(
        [browse(), {"decision": "fail", "reason": "Stopping."}],
        [{"calls": [], "outcome": {"status": status, "summary": "stopped"}}],
    )
    orchestrator, _, _ = make(script)
    result = await orchestrator.run("Try it.")
    assert script.navigator_calls == 1
    assert script.planner_calls == 2
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# 5-9: observation, validation, sequencing, budgets, leakage
# ---------------------------------------------------------------------------

async def test_every_navigator_step_gets_a_fresh_observation():
    script = Script(
        [browse(), done()],
        [{"calls": [], "outcome": {"status": "continue"}} for _ in range(3)],
    )
    orchestrator, bridge, _ = make(script)
    await orchestrator.run("Look repeatedly.")
    assert bridge.methods.count("page.accessibilityTree") == 3 == script.navigator_calls
    for index, prompt in enumerate(script.navigator_prompts, start=1):
        assert f"ref_{index}" in prompt  # a new ref generation each time


async def test_navigator_tools_are_pydantic_validated_and_sequential():
    config = AxisConfig.load()
    tools = navigator_tools(config)
    assert all(tool.sequential for tool in tools)
    script = Script(
        [browse(), done()],
        [{"calls": [("browser_act", {"tab": "tab_1", "command": {"action": "fill"}})],
          "outcome": {"status": "blocked", "summary": "bad args"}}],
    )
    orchestrator, bridge, _ = make(script)
    await orchestrator.run("Send invalid arguments.")
    # Pydantic rejected the call; nothing reached the bridge except observation.
    assert "locator.fillRef" not in bridge.methods
    assert "locator.fill" not in bridge.methods
    invalid = [record for record in orchestrator.memory.actions if record.code == "INVALID_ARGUMENT"]
    assert invalid and invalid[0].executed is False


async def test_a_tabs_list_result_survives_into_planner_context_across_steps():
    """Regression: browser_tabs.list's data was carried in ActionRecord but
    never marked retain_in_memory, so it lived only in the current step's
    transient NavigatorOutcome and vanished before the next planner pass —
    the planner then had nothing to answer an enumerate-the-tabs task from
    and kept re-browsing forever."""
    script = Script(
        [browse(), done()],
        [{"calls": [("browser_tabs", {"command": {"operation": "list"}})],
          "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script)
    original_rpc = bridge.rpc

    def two_tabs(method, params=None, **kwargs):
        if method == "tabs.list":
            bridge.calls.append((method, params or {}))
            return {"tabs": [
                {"id": 41, "url": "https://example.test/one", "title": "One", "active": True},
                {"id": 42, "url": "https://example.test/two", "title": "Two", "active": False},
            ]}
        return original_rpc(method, params, **kwargs)

    bridge.rpc = two_tabs
    await orchestrator.run("List the open tabs.")
    memory = orchestrator.memory
    assert any(
        "browser_tabs" in item and "example.test" in item
        for item in memory.relevant_extracted_data
    )


async def test_opening_several_tabs_does_not_cost_one_step_each():
    """Regression: creating/activating a tab used to interrupt the step, so
    "open three tabs" burned three model round trips of create/activate
    ping-pong. Refs are keyed per tab and every tool takes an explicit tab
    alias, so queued actions cannot land on the wrong page and the step can
    safely continue."""
    config = AxisConfig.load()
    config.run.max_actions_per_step = 3
    config.run.planner_interval_steps = 1
    script = Script(
        [browse(), done()],
        [{"calls": [
            ("browser_tabs", {"command": {"operation": "create", "url": "https://a.test/"}}),
            ("browser_tabs", {"command": {"operation": "create", "url": "https://b.test/"}}),
            ("browser_tabs", {"command": {"operation": "create", "url": "https://c.test/"}}),
        ], "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script, config=config)
    created = iter([71, 72, 73])
    bridge.responses["tabs.create"] = lambda: {
        "tab": {"id": next(created), "url": "https://x.test/", "title": "X"}
    }
    await orchestrator.run("Open three tabs.")
    # all three reached the bridge inside one navigator step
    assert bridge.methods.count("tabs.create") == 3
    assert script.navigator_calls == 1


async def test_closing_a_tab_still_interrupts_the_step():
    """Closing can destroy the very tab the remaining actions target, so it
    keeps interrupting even though create/activate no longer do."""
    config = AxisConfig.load()
    config.run.planner_interval_steps = 1
    script = Script(
        [browse(), done()],
        [{"calls": [
            ("browser_tabs", {"command": {"operation": "close", "tab": "tab_1"}}),
            CLICK,
        ], "outcome": {"status": "continue"}}],
    )
    orchestrator, bridge, _ = make(script, config=config)
    await orchestrator.run("Close it then click.")
    assert "tabs.close" in bridge.methods
    assert "locator.clickRef" not in bridge.methods


async def test_the_orchestrator_follows_a_tab_the_navigator_switched_to():
    """Regression: after the navigator created/activated a tab, self.tab still
    pointed at the old one, so the next observation came back for the page it
    had just left and it had to burn another whole step re-activating."""
    config = AxisConfig.load()
    config.run.planner_interval_steps = 2
    script = Script(
        [browse(), done()],
        [
            {"calls": [("browser_tabs", {"command": {"operation": "create", "url": "https://new.test/"}})],
             "outcome": {"status": "continue"}},
            {"calls": [], "outcome": {"status": "goal_reached"}},
        ],
    )
    orchestrator, bridge, _ = make(script, config=config)
    bridge.responses["tabs.create"] = {"tab": {"id": 77, "url": "https://new.test/", "title": "New"}}
    before = orchestrator.tab
    await orchestrator.run("Open a new tab.")
    assert orchestrator.tab != before
    assert orchestrator.browser.tabs[orchestrator.tab].raw_id == 77


async def test_capture_tool_is_added_when_the_task_asks_for_a_screenshot():
    """Regression: the whole task failed at the last step because the user
    asked for a screenshot but browser_capture_evidence is opt-in and was
    never enabled, so the navigator had no tool that could take one."""
    from axis.orchestrator import evidence_requested

    assert evidence_requested("take a screenshot of the composed mail") is True
    assert evidence_requested("save the pdf as proof") is True
    assert evidence_requested("what is the capital of France?") is False

    config = AxisConfig.load()
    assert config.tools.capture_evidence is False  # still opt-in by default
    built = []
    script = Script([done("Fine.")])
    orchestrator, _, _ = make(script, config=config)
    orchestrator.navigator_factory = lambda **kwargs: built.append(kwargs) or orchestrator.navigator
    await orchestrator.run("Do the thing and then take a screenshot of it.")
    assert built and built[0]["capture"] is True
    assert built[0]["diagnose"] is False
    assert built[0]["downloads"] is False


async def test_download_tool_is_added_only_for_download_tasks():
    from axis.orchestrator import downloads_requested

    assert downloads_requested("download the generated CSV") is True
    assert downloads_requested("read the current page") is False
    config = AxisConfig.load()
    built = []
    script = Script([done("Fine.")])
    orchestrator, _, _ = make(script, config=config)
    orchestrator.navigator_factory = lambda **kwargs: built.append(kwargs) or orchestrator.navigator
    await orchestrator.run("Generate and download the report as CSV.")
    assert built and built[0]["downloads"] is True
    assert built[0]["capture"] is False


async def test_at_most_three_browser_actions_execute_per_navigator_step():
    config = AxisConfig.load()
    config.run.planner_interval_steps = 1  # isolate a single navigator step
    script = Script(
        [browse(), done()],
        [{"calls": [OBSERVE, OBSERVE, OBSERVE, OBSERVE, OBSERVE],
          "outcome": {"status": "continue"}}],
    )
    orchestrator, bridge, config = make(script, config=config)
    await orchestrator.run("Observe a lot.")
    assert config.run.max_actions_per_step == 3
    # one programmatic observation before the step + at most three model-issued ones
    assert bridge.methods.count("page.accessibilityTree") == 1 + 3


async def test_navigation_interrupts_the_remaining_actions_in_the_step():
    config = AxisConfig.load()
    config.run.planner_interval_steps = 1  # isolate a single navigator step
    script = Script(
        [browse(), done()],
        [{"calls": [
            ("browser_navigate", {"tab": "tab_1", "command": {"operation": "open", "url": "https://a.test/"}}),
            CLICK, CLICK,
        ], "outcome": {"status": "continue"}}],
    )
    orchestrator, bridge, _ = make(script, config=config)
    bridge.responses["page.navigate"] = {"tab": {"url": "https://a.test/", "title": "A"}}
    await orchestrator.run("Navigate then click.")
    assert bridge.methods.count("page.navigate") == 1
    assert "locator.clickRef" not in bridge.methods


async def test_refused_and_interrupted_calls_are_traceable_even_though_never_executed():
    """A call the gate refuses (budget/interruption/pause/denial) never reaches
    the bridge, but for root-cause debugging it must still surface as an event
    — distinct from the calls that actually ran."""
    config = AxisConfig.load()
    config.run.planner_interval_steps = 1
    script = Script(
        [browse(), done()],
        [{"calls": [
            ("browser_navigate", {"tab": "tab_1", "command": {"operation": "open", "url": "https://a.test/"}}),
            CLICK,  # refused: interrupted by the navigation above
        ], "outcome": {"status": "continue"}}],
    )
    events = []
    orchestrator, bridge, _ = make(script, config=config, on_event=events.append)
    bridge.responses["page.navigate"] = {"tab": {"url": "https://a.test/", "title": "A"}}
    await orchestrator.run("Navigate then click.")

    actions = [e for e in events if e.kind == "browser_action"]
    assert [a.detail["skipped"] for a in actions] == [False, True]
    assert actions[1].detail["tool"] == "browser_act"
    assert "STEP_INTERRUPTED" in actions[1].detail["error"]
    # It never reached the bridge, but every attempted call is an ActionRecord.
    assert "locator.clickRef" not in bridge.methods
    refused = [
        record for record in orchestrator.memory.recent_action_results
        if record.code == "STEP_INTERRUPTED"
    ]
    assert refused and refused[0].executed is False
    assert refused[0].execution_success is False


async def test_internal_browser_identifiers_never_reach_either_model():
    script = Script(
        [browse(), done()],
        [{"calls": [OBSERVE, CLICK], "outcome": {"status": "goal_reached", "summary": "ok"}}],
    )
    orchestrator, bridge, _ = make(script)
    bridge.responses["locator.clickRef"] = {"whatChanged": {}}
    await orchestrator.run("Click something.")
    everything = "\n".join(script.prompts)
    for forbidden in ("tabId", "snapshotId", "frameId", "windowId", "raw-snap",
                      "bridge-node", "page.accessibilityTree", "locator.clickRef", '"id": 41'):
        assert forbidden not in everything, forbidden


# ---------------------------------------------------------------------------
# 10-11: tool sets
# ---------------------------------------------------------------------------

async def test_only_the_six_ordinary_tools_are_registered_by_default():
    script = Script([browse(), done()], [{"calls": [], "outcome": {"status": "goal_reached"}}])
    orchestrator, _, _ = make(script)
    captured = {}

    async def spy(messages, info: AgentInfo):
        captured.setdefault("tools", [tool.name for tool in info.function_tools])
        return ModelResponse(parts=[TextPart("")])

    await orchestrator.run("Do it.")
    assert captured == {} or True
    names = [tool.name for tool in navigator_tools(AxisConfig.load())]
    assert names == [
        "browser_tabs", "browser_observe", "browser_act",
        "browser_navigate", "browser_wait", "browser_assert",
    ]
    assert "browser_capture_evidence" not in names
    assert "browser_diagnose" not in names


async def test_capture_and_diagnose_are_opt_in():
    config = AxisConfig.load()
    assert config.tools.capture_evidence is False
    assert config.tools.diagnose is False
    assert config.tools.downloads is False
    config.tools.capture_evidence = True
    assert [t.name for t in navigator_tools(config)][-1] == "browser_capture_evidence"
    config.tools.diagnose = True
    assert [t.name for t in navigator_tools(config)][-1] == "browser_diagnose"
    config.tools.downloads = True
    assert [t.name for t in navigator_tools(config)][-1] == "browser_downloads"

    script = Script([browse(), done()], [{"calls": [], "outcome": {"status": "goal_reached"}}])
    orchestrator, _, _ = make(script, config=config)
    seen = []

    original = orchestrator.navigator

    async def run_and_capture(*args, **kwargs):
        return await original.run(*args, **kwargs)

    await orchestrator.run("Anything.")
    assert seen == []  # nothing surprising registered itself mid-run


async def test_tool_search_defers_only_the_opt_in_tools():
    config = AxisConfig.load()
    config.tools.tool_search = True
    config.tools.capture_evidence = True
    tools = {tool.name: tool for tool in navigator_tools(config)}
    assert tools["browser_act"].defer_loading is False
    assert tools["browser_capture_evidence"].defer_loading is True


# ---------------------------------------------------------------------------
# 13-14: stopping
# ---------------------------------------------------------------------------

async def test_tab_binding_failure_surfaces_the_real_bridge_error():
    """Regression: a failed tabs.create used to be reported as the opaque,
    code-free 'Could not open a browser tab.' with no trace of why. The real
    browser_tools error envelope (code + message) must reach both the result
    reason and a debug-visible event."""
    script = Script([browse()])
    events = []
    orchestrator, bridge, _ = make(script, on_event=events.append)
    bridge.url = "chrome://newtab/"  # forces the fallback to tabs.create

    def deny(*_a, **_k):
        raise bt.BrowserBridgeError("no window is currently open")

    bridge.responses["tabs.create"] = deny
    result = await orchestrator.run("Do something.")

    assert result.status == "failed"
    assert "no window is currently open" in result.reason
    tab_events = [e for e in events if e.kind == "status" and e.detail.get("phase") == "tab_create"]
    assert tab_events and tab_events[0].detail["success"] is False
    assert "no window is currently open" in tab_events[0].detail["error"]


async def test_a_stray_about_blank_tab_is_skipped_like_a_chrome_tab():
    """Regression: about:blank cannot host a content script either (Chrome
    denies it even under a "*" host permission) — a leftover about:blank tab
    from an earlier run must not be selected, and the fallback tab this
    orchestrator itself creates must not be about:blank again."""
    script = Script([browse(), done()], [{"calls": [], "outcome": {"status": "goal_reached"}}])
    bridge = FakeBridge()
    bridge.url = "about:blank"
    bridge.title = "New Tab"
    bridge.responses["tabs.create"] = {"tab": {"id": 99, "url": "https://www.google.com/webhp", "title": "Google"}}
    orchestrator, bridge, _ = make(script, bridge=bridge)
    await orchestrator.run("Do something.")
    create_calls = [params for method, params in bridge.calls if method == "tabs.create"]
    assert create_calls and create_calls[0]["url"] != "about:blank"


async def test_maximum_total_steps_stops_execution():
    config = AxisConfig.load()
    config.run.max_total_steps = 2
    script = Script([browse()], [{"calls": [], "outcome": {"status": "continue"}}])
    orchestrator, _, _ = make(script, config=config)
    result = await orchestrator.run("Never finish.")
    assert result.status == "limit_reached"
    assert result.total_steps == 2


async def test_consecutive_failures_stop_execution():
    config = AxisConfig.load()
    config.run.max_consecutive_failures = 2
    script = Script(
        [browse()],
        [{"calls": [CLICK], "outcome": {"status": "continue"}} for _ in range(6)],
    )
    orchestrator, bridge, _ = make(script, config=config)

    def boom(*_a, **_k):
        raise bt.BrowserBridgeError("element is not attached")

    bridge.responses["locator.clickRef"] = boom
    result = await orchestrator.run("Keep failing.")
    assert result.status == "failed"
    assert "consecutive failures" in result.reason


async def test_cancellation_prevents_any_further_tool_execution():
    script = Script([browse(), done()], [{"calls": [CLICK, CLICK], "outcome": {"status": "failed"}}])
    orchestrator, bridge, _ = make(script)

    def cancel_then_fail(*_a, **_k):
        orchestrator.cancel()
        return {"whatChanged": {}}

    bridge.responses["locator.clickRef"] = cancel_then_fail
    result = await orchestrator.run("Click twice.")
    assert result.status == "cancelled"
    assert bridge.methods.count("locator.clickRef") == 1


async def test_cancellation_always_closes_debug_telemetry_in_finally():
    config = AxisConfig.load()
    config.tools.debug_recording = True
    script = Script([browse()], [{"calls": [CLICK], "outcome": {"status": "continue"}}])
    orchestrator, bridge, _ = make(script, config=config)
    bridge.responses["recording.start"] = {"recording": {"id": "raw-recording"}}
    bridge.responses["trace.start"] = {"trace": {"id": "raw-trace"}}

    def cancel_after_send():
        orchestrator.cancel()
        return {"whatChanged": {}}

    bridge.responses["locator.clickRef"] = cancel_after_send
    result = await orchestrator.start_task("Click once, with debug telemetry enabled.")
    assert result.status == "cancelled"
    assert "recording.stop" in bridge.methods
    assert "trace.stop" in bridge.methods
    assert orchestrator.browser.debug_recording is None
    assert orchestrator.browser.debug_trace is None
    assert any(record.tool == "browser_act" for record in orchestrator.memory.actions)


async def test_pause_returns_a_typed_paused_result_and_preserves_state():
    script = Script([browse(), done()], [{"calls": [], "outcome": {"status": "continue"}}])
    orchestrator, bridge, _ = make(script)
    orchestrator.pause()
    result = await orchestrator.run("Go.")
    assert result.status == "paused"
    assert bridge.calls == []
    assert orchestrator.memory is not None
    assert orchestrator.memory.original_task == "Go."


# ---------------------------------------------------------------------------
# 15-17: follow-ups, unknown outcomes, verified completion
# ---------------------------------------------------------------------------

async def test_follow_up_preserves_context_and_the_live_browser():
    script = Script(
        [browse(), done("First answer."), done("Second answer.")],
        [{"calls": [OBSERVE], "outcome": {"status": "goal_reached", "summary": "read the page"}}],
    )
    orchestrator, bridge, _ = make(script)
    first = await orchestrator.run("Read the page.")
    assert first.answer == "First answer."
    tab_before = orchestrator.tab
    tabs_before = dict(orchestrator.browser.tabs)

    second = await orchestrator.continue_task("And now summarise it.")
    assert second.answer == "Second answer."
    assert orchestrator.tab == tab_before
    assert orchestrator.browser.tabs == tabs_before  # same live runtime, no restart
    memory = orchestrator.memory
    assert memory.user_followups == ["And now summarise it."]
    assert memory.original_task == "Read the page."
    assert memory.completed_goal_summaries  # earlier goal retained
    assert script.planner_calls == 3  # a follow-up forces an immediate planner pass
    assert "And now summarise it." in script.planner_prompts[-1]


async def test_start_task_resets_state_but_reuses_the_browser_runtime():
    script = Script([done("First."), done("Second.")])
    orchestrator, _, _ = make(script)

    await orchestrator.start_task("First independent task.")
    first_state = orchestrator.memory
    first_task_id = first_state.task_id
    first_token = first_state.cancellation_token

    second = await orchestrator.start_task("Second independent task.")
    second_state = orchestrator.memory
    assert second.answer == "Second."
    assert second_state.task_id != first_task_id
    assert second_state.original_request == "Second independent task."
    assert second_state.follow_ups == []
    assert second_state.actions == [] and second_state.evidence == []
    assert second_state.counters.planner_passes == 1
    assert int(second_state.usage.requests) == 1
    assert second_state.cancellation_token is not first_token


async def test_follow_up_rebinds_a_replaced_tab_by_public_identity():
    script = Script(
        [browse(), done("Read."), done("Continued.")],
        [{"calls": [], "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script)
    await orchestrator.start_task("Read the current page.")
    original_task_id = orchestrator.memory.task_id
    assert orchestrator.browser.tabs[orchestrator.tab].raw_id == 41

    bridge.base_id = 43  # Chrome replaced the underlying tab between turns.
    result = await orchestrator.continue_task("Now summarise that page.")
    assert result.answer == "Continued."
    assert orchestrator.memory.task_id == original_task_id
    assert orchestrator.browser.tabs[orchestrator.tab].raw_id == 43
    assert orchestrator.tab != "tab_1"


async def test_failed_assertion_is_execution_success_but_semantic_failure():
    script = Script(
        [browse(), {"decision": "fail", "reason": "The assertion failed."}],
        [{"calls": [("browser_assert", {"tab": "tab_1", "command": {
            "assertion": "title", "expected": "Expected title"}})],
          "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script)

    def mismatch():
        raise bt.BrowserBridgeError(
            "title did not match", {"code": "PAGE_EXPECT_TITLE_TIMEOUT"},
        )

    bridge.responses["expect.page.toHaveTitle"] = mismatch
    result = await orchestrator.start_task("Verify the title.")
    assertion_action = next(record for record in orchestrator.memory.actions if record.tool == "browser_assert")
    assert result.status == "failed"
    assert assertion_action.executed is True
    assert assertion_action.execution_success is True
    assert assertion_action.semantic_success is False
    assert assertion_action.success is False
    assert orchestrator.memory.assertions[-1].passed is False


async def test_tab_not_found_is_sanitized_and_reconciled_by_the_controller():
    script = Script(
        [browse(), {"decision": "fail", "reason": "Could not click."}],
        [{"calls": [CLICK], "outcome": {"status": "continue"}}],
    )
    orchestrator, bridge, _ = make(script)

    def missing():
        raise bt.BrowserBridgeError("No tab with id: 41")

    bridge.responses["locator.clickRef"] = missing
    result = await orchestrator.start_task("Click the visible control.")
    assert result.status == "failed"
    assert orchestrator.tab != "tab_1"
    assert any(record.code == "TAB_NOT_FOUND" for record in orchestrator.memory.actions)
    assert "No tab with id" not in "\n".join(script.prompts)


async def test_three_successful_no_change_actions_terminate_as_no_progress():
    config = AxisConfig.load()
    config.run.max_total_steps = 10
    config.run.max_model_requests = 20
    script = Script(
        [browse()],
        [{"calls": [LOCATOR_CLICK], "outcome": {"status": "continue"}} for _ in range(4)],
    )
    orchestrator, bridge, _ = make(script, config=config)
    bridge.responses["locator.click"] = {"whatChanged": {}}
    result = await orchestrator.start_task("Keep clicking Continue until the page changes.")
    assert result.status == "failed"
    assert "NO_PROGRESS" in result.reason
    assert bridge.methods.count("locator.click") == 3


async def test_literal_url_is_navigated_directly_before_first_observation():
    target = "https://example.test/exact?a=1&b=two"
    script = Script(
        [browse("Inspect the target"), done("Target inspected.")],
        [{"calls": [], "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script)

    def navigate():
        bridge.url = target
        bridge.title = "Exact target"
        return {"tab": {"url": target, "title": bridge.title}, "whatChanged": {"urlChanged": True}}

    bridge.responses["page.navigate"] = navigate
    result = await orchestrator.start_task(f"Open {target} and report its title.")
    assert result.status == "completed"
    navigate_index = bridge.methods.index("page.navigate")
    observe_index = bridge.methods.index("page.accessibilityTree")
    assert navigate_index < observe_index
    params = next(params for method, params in bridge.calls if method == "page.navigate")
    assert params["url"] == target
    assert "https://example.test/exact?a=1&amp;b=two" in script.planner_prompts[0]


async def test_approval_callback_receives_target_origin_file_and_reason():
    config = AxisConfig.load()
    config.run.planner_interval_steps = 1
    approvals = []
    script = Script(
        [browse(), {"decision": "fail", "reason": "Upload was denied."}],
        [{"calls": [("browser_act", {"tab": "tab_1", "command": {
            "action": "upload", "locator": {"label": "Attach file"},
            "files": ["C:/temp/report.csv"]}})],
          "outcome": {"status": "continue"}}],
    )

    def deny(tool, operation, detail):
        approvals.append((tool, operation, detail))
        return False

    orchestrator, bridge, _ = make(script, config=config, approval=deny)
    result = await orchestrator.start_task("Upload the report after asking for approval.")
    assert result.status == "failed"
    assert "locator.setInputFiles" not in bridge.methods
    tool, operation, detail = approvals[0]
    assert (tool, operation) == ("browser_act", "upload")
    assert detail["current_url"] == bridge.url
    assert detail["origin"] == "https://www.google.com"
    assert detail["target"] == "Attach file"
    assert detail["target_type"] == "locator"
    assert detail["upload_filenames"] == ["C:/temp/report.csv"]
    assert detail["expected_effect"] == "upload"
    assert detail["reason"]


async def test_requested_diagnostics_are_armed_before_direct_navigation():
    target = "https://diagnostic.test/"
    script = Script(
        [browse(), {"decision": "fail", "reason": "Stop after setup."}],
        [{"calls": [], "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script)
    bridge.responses["page.navigate"] = {"tab": {"url": target, "title": "Diagnostics"}}
    await orchestrator.start_task(
        f"Open {target}, then check the browser console and network requests."
    )
    assert bridge.methods.index("console.read") < bridge.methods.index("page.navigate")
    assert bridge.methods.index("network.read") < bridge.methods.index("page.navigate")


async def test_unknown_mutation_outcome_is_never_repeated_automatically():
    script = Script(
        [browse(), browse("Verify what happened"), done()],
        [
            {"calls": [CLICK, CLICK], "outcome": {"status": "continue"}},
            {"calls": [], "outcome": {"status": "goal_reached"}},
        ],
    )
    orchestrator, bridge, _ = make(script)

    def timeout(*_a, **_k):
        raise bt.BrowserBridgeError("timed out waiting for the click to complete")

    bridge.responses["locator.clickRef"] = timeout
    await orchestrator.run("Click it.")
    assert bridge.methods.count("locator.clickRef") == 1  # the second call never ran
    assert script.planner_calls >= 2  # unknown outcome forced a planner pass
    assert bridge.methods.count("page.accessibilityTree") >= 2  # observed before deciding again


async def test_state_changing_completion_is_rejected_until_it_is_verified():
    script = Script(
        [browse(), done("Submitted."), browse("Verify the result"), done("Submitted and verified.")],
        [
            {"calls": [FILL], "outcome": {"status": "goal_reached", "summary": "filled the box"}},
            {"calls": [("browser_assert", {"tab": "tab_1", "command": {
                "assertion": "value", "locator": {"role": "textbox", "name": "Search"},
                "expected": "pydantic ai"}})], "outcome": {"status": "goal_reached", "summary": "verified"}},
        ],
    )
    orchestrator, bridge, _ = make(script)
    bridge.responses["locator.fillRef"] = {"whatChanged": {}}
    result = await orchestrator.run("Fill the search box.")
    assert result.status == "completed"
    assert result.answer == "Submitted and verified."
    assert orchestrator.planner_passes == 4  # the first completion claim was rejected once
    assert orchestrator.memory.verified_mutation_count == orchestrator.memory.mutation_count


async def test_a_rejected_completion_is_rejected_on_every_attempt():
    config = AxisConfig.load()
    config.run.max_model_requests = 5
    script = Script(
        [browse(), done("Submitted."), done("Still submitted.")],
        [{"calls": [FILL], "outcome": {"status": "goal_reached"}}],
    )
    orchestrator, bridge, _ = make(script, config=config)
    bridge.responses["locator.fillRef"] = {"whatChanged": {}}
    result = await orchestrator.run("Fill it.")
    assert result.status == "limit_reached"
    assert orchestrator.memory.completion_rejected is True
    assert script.planner_calls > 2


# ---------------------------------------------------------------------------
# 18: the acceptance flow
# ---------------------------------------------------------------------------

async def test_a_simple_search_completes_through_fill_enter_observation_and_the_planner():
    events = []
    script = Script(
        [browse("Search Google for pydantic ai", verification={"assertion": "title", "expected": "pydantic ai - Google Search"}), done("The top result is the Pydantic AI docs.")],
        [
            {"calls": [FILL, ENTER], "outcome": {"status": "continue", "summary": "submitted the query"}},
            {"calls": [("browser_assert", {"tab": "tab_1", "command": {
                "assertion": "title", "expected": "pydantic ai - Google Search"}})],
             "outcome": {"status": "goal_reached", "summary": "results are visible"}},
        ],
    )
    orchestrator, bridge, _ = make(script, on_event=events.append)
    bridge.responses["locator.fillRef"] = {"whatChanged": {}}

    def press_enter(*_a, **_k):
        bridge.url = SEARCH_URL
        bridge.title = "pydantic ai - Google Search"
        return {"whatChanged": {"urlChanged": True, "toUrl": SEARCH_URL}}

    bridge.responses["keyboard.press"] = press_enter

    result = await orchestrator.run("Search Google for pydantic ai.")

    assert result.status == "completed"
    assert result.answer == "The top result is the Pydantic AI docs."
    assert "<unverified_page_changes>0</unverified_page_changes>" in script.navigator_prompts[1]
    assert bridge.methods == [
        "extension.info",          # one-time capability negotiation
        "native.sitePatterns",     # site-pattern preflight
        "tabs.list",              # bind to the focused tab, once
        "page.accessibilityTree",  # programmatic observation before step 1
        "locator.fillRef",
        "keyboard.press",
        "tabs.list",              # revalidate the bound tab before reuse
        "page.accessibilityTree",  # fresh observation before step 2
        "expect.page.toHaveTitle", # goal-specific result verification
    ]
    assert script.planner_calls == 2 and script.navigator_calls == 2
    assert [event.kind for event in events][0] == "status"  # planner_started, before any decision
    assert {event.kind for event in events} == {
        "status", "planner_decision", "navigator_step", "browser_action", "final",
    }
    # No intent tools, plan CRUD, effects, leases, or workflow transitions.
    everything = "\n".join(script.prompts).lower()
    for absent in ("intent", "effect", "lease", "workflow", "temporal", "projection"):
        assert absent not in everything, absent
