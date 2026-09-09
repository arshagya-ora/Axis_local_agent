"""Regressions from the six-technology research run; no browser/provider needed."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import browser_tools as bt
from axis.agents import StepGate
from axis.models import GoalCheck, PlanDecision, SourceNote, TaskRequirements, TaskRunState
from axis.orchestrator import extract_requirements, downloads_requested, verify_completion
from test_axis_agent import Script, FakeBridge, browse, done, make
from test_axis_reliability import ready, action, extraction
from test_browser_tools import runtime_with_tab, context


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_exact_long_task_preserves_all_prohibitions_and_numeric_requirements():
    task = (Path(__file__).parent / "fixtures/long_research_task.txt").read_text(encoding="utf-8")
    req = extract_requirements(task)
    assert not req.require_download
    assert {"login", "submit", "purchase", "modify", "download"} <= req.forbidden_actions
    assert (req.min_sources, req.min_tabs, req.min_actions) == (15, 6, 30)
    assert req.require_search
    assert any("download files" in p for p in req.prohibitions)


@pytest.mark.parametrize("text", [
    "Do not download files.", "Don't download anything.", "Research download support.",
    "Investigate how frameworks support downloads.", "Compare download handling.",
    "Read the documentation without downloading files.",
])
def test_discussing_or_prohibiting_downloads_does_not_request_one(text):
    assert not downloads_requested(text)


@pytest.mark.parametrize("text", ["Download the report.", "Click Download report and verify it.", "Export the CSV file."])
def test_positive_download_requests_still_work(text):
    assert downloads_requested(text)


@pytest.mark.parametrize("target", ['link "Source code (zip)"', "Download report", "https://github.com/org/repo/archive/refs/tags/v1.zip"])
def test_download_prohibition_blocks_click_before_execution(target):
    gate = StepGate(max_actions=3, remaining_total_actions=10, forbidden_actions=frozenset({"download"}))
    result = gate.check("browser_act", {"tab": "tab_1", "command": bt.Click(action="click",
        target=bt.RefTarget(kind="ref", ref="ref_1"))}, {"target": target})
    assert result["error"]["code"] == "POLICY_DENIED"
    assert gate.actions_used == 0


def test_reading_download_documentation_is_allowed():
    gate = StepGate(max_actions=3, remaining_total_actions=10, forbidden_actions=frozenset({"download"}))
    assert gate.check("browser_observe", {"tab": "tab_1", "mode": "text"},
                      {"target": "Download support"}) is None


def test_tab_limit_rejection_does_not_create_an_orphan_tab():
    bridge, runtime, tab = runtime_with_tab()
    runtime.max_tabs = 1
    result = bt.browser_tabs(context(runtime), bt.CreateTab(operation="create", url="https://example.test/new"))
    assert result["error"]["code"] == "TAB_LIMIT"
    assert "tabs.create" not in [m for m, _ in bridge.calls]


def test_duplicate_source_reuses_alias_even_when_tab_capacity_is_full():
    bridge, runtime, tab = runtime_with_tab()
    runtime.max_tabs = 1
    result = bt.browser_tabs(context(runtime), bt.CreateTab(operation="create", url="https://example.test/"))
    assert result["ok"] and result["tab"] == tab and result["data"]["reused"]
    assert not bridge.calls
    result = bt.browser_tabs(context(runtime), bt.CreateTab(operation="create", url="https://example.test/", reuse=False))
    assert result["error"]["code"] == "TAB_LIMIT"


def test_one_batch_records_all_created_tabs_and_navigation_observation_target():
    orchestrator, _, _ = ready()
    gate = StepGate(max_actions=3, remaining_total_actions=10, on_record=orchestrator._register_action)
    for index in range(2, 5):
        alias = orchestrator.browser._register({"id": 100 + index, "url": f"https://example.test/{index}"})
        gate.record("browser_tabs", {"command": bt.CreateTab(operation="create", url=f"https://example.test/{index}")},
                    {"ok": True, "tab": alias, "data": {"tab": alias}})
    orchestrator._commit_gate(gate)
    assert {"tab_2", "tab_3", "tab_4"} <= set(orchestrator.state.task_tabs)
    assert {"tab_2", "tab_3", "tab_4"} <= {t.tab for t in orchestrator._tab_summaries()}
    gate = StepGate(max_actions=3, remaining_total_actions=10)
    gate.record("browser_navigate", {"tab": "tab_2", "command": bt.Open(operation="open", url="https://example.test/next")},
                {"ok": True, "data": {"refsInvalidated": True}})
    orchestrator._commit_gate(gate)
    assert orchestrator.tab == "tab_2"


def test_research_notes_require_actual_quoted_evidence_and_keep_other_dimensions():
    orchestrator, _, _ = ready()
    read = extraction(orchestrator, {"text": "Checkpoints preserve task state. Interrupts wait for human approval."})
    notes = [
        SourceNote(topic="Example", dimension="durability", evidence_id=read.evidence_id,
                   quote="Checkpoints preserve task state."),
        SourceNote(topic="Example", dimension="approval", evidence_id=read.evidence_id,
                   quote="Interrupts wait for human approval."),
        SourceNote(topic="Example", dimension="false claim", evidence_id=read.evidence_id,
                   quote="This invented capability is unsupported."),
    ]
    orchestrator._retain_source_notes(PlanDecision(decision="browse", source_notes=notes, remaining_work=["Testing"]))
    retained = [f for f in orchestrator.state.facts if f.tool == "source_note"]
    assert len(retained) == 1
    values = json.loads(retained[0].value)
    assert {v["dimension"] for v in values} == {"approval", "durability"}
    assert all(v["evidence_id"] == read.evidence_id and v["url"] for v in values)
    assert orchestrator.state.remaining_work == ["Testing"]
    assert "rejected" in orchestrator.state.last_error
    assert not verify_completion(orchestrator.state, None).valid


def test_opening_tabs_and_model_prose_cannot_satisfy_source_review_requirements():
    orchestrator, _, _ = ready()
    state = orchestrator.state
    state.requirements = TaskRequirements(min_sources=1, require_search=True)
    state.add_evidence("navigator", "Reviewed 15 official sources")
    assert not verify_completion(state, None).valid
    extraction(orchestrator, "The official documentation describes persistent checkpoints and human approval.")
    assert len(state.source_register) == 1
    assert not verify_completion(state, None).valid
    orchestrator._record_search("https://www.google.com/search?q=framework")
    assert verify_completion(state, None).valid


def test_research_memory_stays_bounded_and_repeated_sources_are_deduplicated():
    orchestrator, _, _ = ready()
    for index in range(35):
        orchestrator.browser.tabs["tab_1"].url = f"https://docs.example.test/{index}"
        read = extraction(orchestrator, f"Source {index} supports persistent checkpoints and human approval.")
        orchestrator._retain_source_notes(PlanDecision(decision="browse", source_notes=[
            SourceNote(topic=f"Technology {index % 6}", dimension=f"dimension {index}", evidence_id=read.evidence_id,
                       quote="supports persistent checkpoints and human approval.")]))
    state = orchestrator.state
    assert len(state.facts) <= 16
    assert sum(len(f.model_dump_json()) for f in state.facts) + len(json.dumps(state.source_register)) <= 32_000
    assert state.memory_truncated
    before = len(state.source_register)
    extraction(orchestrator, "Source supports persistent checkpoints and human approval.")
    assert len(state.source_register) == before
    assert len([f for f in state.facts if f.tool == "source_note"]) == 6


@pytest.mark.anyio
async def test_budget_extension_keeps_evidence_usage_and_task_identity():
    script = Script([done("A short answer.")])
    orchestrator, _, config = make(script)
    result = await orchestrator.run("Answer briefly.")
    state = orchestrator.state
    original_id = state.task_id
    config.run.max_model_requests = int(state.usage.requests)
    state.source_register["https://docs.example.test/"] = {"title": "Docs", "url": "https://docs.example.test/"}
    stopped = await orchestrator.continue_task("Keep going.")
    assert stopped.status == "limit_reached" and "https://docs.example.test/" in stopped.answer
    resumed = await orchestrator.extend_budget(4)
    assert resumed.status == "completed" and resumed.model_requests == 2
    assert state.task_id == original_id and state.source_register
    assert config.run.max_model_requests == 1
    await orchestrator.run("A new independent request.")
    assert orchestrator.state.extra_model_requests == 0


def test_long_final_answers_are_not_silently_clipped_to_4000_characters():
    answer = "Evidence-backed comparison. " * 300
    assert PlanDecision(decision="complete", final_answer=answer).final_answer == answer


def test_corrected_validation_failure_does_not_block_completion_but_other_tabs_stay_unresolved():
    from axis.models import ActionRecord
    for failed_tab, expected in [("tab_1", True), ("tab_2", False)]:
        invalid = ActionRecord(tool="browser_observe", tab=failed_tab, executed=False,
                               execution_success=False, code="INVALID_ARGUMENT")
        gate = StepGate(max_actions=3, remaining_total_actions=10)
        gate.records.append(invalid)
        gate.effects.append((False, False))
        gate.record("browser_observe", {"tab": "tab_1", "mode": "text"},
                    {"ok": True, "data": {"snapshot": "Corrected read"}})
        assert invalid.recovered is expected
        state = TaskRunState(actions=gate.records)
        assert verify_completion(state, None).valid is expected


@pytest.mark.anyio
async def test_long_research_with_39_existing_tabs_reviews_18_sources_using_six_task_tabs():
    class ResearchBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.pages = {i: {"id": i, "url": f"https://user.example/{i}", "title": "User tab",
                              "active": i == 1} for i in range(1, 40)}
            self.pages[1]["url"] = "https://www.google.com/"
            self.query = ""
        def rpc(self, method, params=None, **kwargs):
            params = params or {}
            self.calls.append((method, params))
            if method == "tabs.list":
                return {"tabs": list(self.pages.values())}
            if method == "tabs.create":
                raw = {"id": max(self.pages) + 1, "url": params["url"], "title": "Official docs", "active": True}
                self.pages[raw["id"]] = raw
                return {"tab": raw}
            if method == "page.navigate":
                self.pages[params["tabId"]]["url"] = params["url"]
                return {"tab": self.pages[params["tabId"]]}
            if method == "locator.fill":
                self.query = params["text"]
                return {}
            if method == "locator.press":
                from urllib.parse import urlencode
                self.pages[params["tabId"]]["url"] = "https://www.google.com/search?" + urlencode({"q": self.query})
                return {}  # Real search navigation can omit action-change metadata.
            if method == "page.accessibilityTree":
                self.observations += 1
                raw = self.pages[params["tabId"]]
                return {"url": raw["url"], "title": raw["title"], "snapshotId": str(self.observations),
                        "snapshot": f'Official documentation for {raw["url"]}: runtime evidence and durable state.'}
            if method == "locator.allInnerTexts":
                return [f'Official documentation at {self.pages[params["tabId"]]["url"]}. '
                        'Checkpoints retain task state and interruptions preserve human approval.']
            return {}

    target = {"kind": "locator", "locator": {"role": "combobox", "name": "Search"}}
    steps = [{"calls": [
        ("browser_act", {"tab": "tab_1", "command": {"action": "fill", "target": target, "value": "official framework documentation"}}),
        ("browser_act", {"tab": "tab_1", "command": {"action": "press", "target": target, "key": "Enter"}}),
    ], "outcome": {"status": "continue"}}]
    for index in range(18):
        tab = f"tab_{40 + index % 6}"
        url = f"https://docs.example.test/technology-{index % 6}/source-{index // 6}"
        if index < 6:
            call = ("browser_tabs", {"command": {"operation": "create", "url": url}})
        else:
            call = ("browser_navigate", {"tab": tab, "command": {"operation": "open", "url": url}})
        steps.append({"calls": [call], "outcome": {"status": "continue"}})
        steps.append({"calls": [("browser_observe", {"tab": tab, "locator": {"role": "main"}, "extract": "all_inner_text"})],
                      "outcome": {"status": "goal_reached" if index == 17 else "continue"}})

    class ResearchScript(Script):
        def _respond(self, messages, info):
            if not info.function_tools:
                self.planner = [done("All six technologies reviewed using eighteen fixture sources.")
                                if self.navigator_calls >= len(steps) else browse("Review all six technologies.")]
            return super()._respond(messages, info)

    script = ResearchScript([browse()], steps)
    bridge = ResearchBridge()
    orchestrator, _, config = make(script, bridge=bridge)
    config.run.max_total_steps = 45
    config.run.max_model_requests = 120
    request = (Path(__file__).parent / "fixtures/long_research_task.txt").read_text(encoding="utf-8")
    result = await orchestrator.run(request)
    assert result.status == "completed", result.reason
    assert len(orchestrator.state.source_register) == 18
    assert len([m for m, _ in bridge.calls if m == "tabs.create"]) == 6
    assert not any(m.startswith("downloads.") or m == "tabs.close" for m, _ in bridge.calls)
    assert all(bridge.pages[i]["url"] == f"https://user.example/{i}" for i in range(2, 40))
    assert result.browser_actions == 38
    assert not orchestrator.state.pending_changes
    assert not any(m.startswith("expect.") for m, _ in bridge.calls)
    assert result.model_requests < 120


def search_actions(orchestrator, *, submit=True):
    gate = StepGate(max_actions=3, remaining_total_actions=10, on_record=orchestrator._register_action)
    target = bt.LocatorTarget(kind="locator", locator=bt.Locator(role="combobox", name="Search"))
    gate.record("browser_act", {"tab": "tab_1", "command": bt.Fill(action="fill", target=target, value="official docs")},
                {"ok": True, "data": {}})
    if submit:
        gate.record("browser_act", {"tab": "tab_1", "command": bt.Press(action="press", target=target, key="Enter")},
                    {"ok": True, "data": {}})
    return gate


@pytest.mark.anyio
async def test_search_results_verify_query_despite_irrelevant_planner_count():
    orchestrator, bridge, _ = ready()
    orchestrator.state.current_verification = GoalCheck(assertion="count", expected=3, target="official source tabs")
    gate = search_actions(orchestrator)
    assert gate.active_tab_changed_to == "tab_1"
    assert (await orchestrator._browser_state()).unverified_page_changes == 2  # Delayed navigation.
    bridge.url = "https://www.google.com/search?q=official+docs"
    assert (await orchestrator._browser_state()).unverified_page_changes == 0
    assert orchestrator.state.counters.verified_mutation_count == 2


@pytest.mark.parametrize("url,tab,goal,submit", [
    ("https://www.google.com/search?q=wrong", "tab_1", "goal_1", True),
    ("https://www.bing.com/search?q=official+docs", "tab_1", "goal_1", True),
    ("https://www.google.com/search?q=official+docs", "tab_2", "goal_1", True),
    ("https://www.google.com/search?q=official+docs", "tab_1", "goal_2", True),
    ("https://www.google.com/search?q=official+docs", "tab_1", "goal_1", False),
])
def test_search_requires_submitted_matching_query_goal_and_tab(url, tab, goal, submit):
    orchestrator, _, _ = ready()
    search_actions(orchestrator, submit=submit)
    orchestrator.state.current_goal_id = goal
    orchestrator._verify_navigation(tab, url, orchestrator.state.next_sequence())
    assert orchestrator.state.pending_changes


def test_search_does_not_clear_prior_business_mutation_or_stale_evidence():
    orchestrator, _, _ = ready()
    orchestrator._register_action(action(), True)
    search_actions(orchestrator)
    orchestrator._verify_navigation("tab_1", "https://www.google.com/search?q=official+docs", orchestrator.state.next_sequence())
    assert orchestrator.state.unverified_mutations == 3
    orchestrator, _, _ = ready()
    search_actions(orchestrator)
    pending = next(iter(orchestrator.state.pending_changes.values()))
    orchestrator._verify_navigation("tab_1", "https://www.google.com/search?q=official+docs", pending.sequence)
    assert orchestrator.state.unverified_mutations == 2


def test_explicit_search_observation_verifies_without_touching_other_tabs():
    orchestrator, bridge, _ = ready()
    search_actions(orchestrator)
    orchestrator._register_action(action(tab="tab_2"), True)
    orchestrator.browser.tabs["tab_1"].url = "https://www.google.com/search?q=official+docs"
    extraction(orchestrator, "Official source results")
    assert len(orchestrator.state.pending_changes) == 2  # An extraction has no fresh URL.
    orchestrator._register_action(action("browser_observe", "compact", result_data={
        "snapshot": "Official source results", "url": "https://www.google.com/search?q=official+docs"},
        extracted_data="Official source results"), False)
    assert [p.tab for p in orchestrator.state.pending_changes.values()] == ["tab_2"]


@pytest.mark.parametrize("selector", ['a:has-text("Docs")', 'a:text("Docs")', 'a:visible', 'a:nth-match(2)'])
def test_playwright_selectors_rejected_before_bridge(selector):
    with pytest.raises(ValueError, match="native CSS"):
        bt.Locator(selector=selector)
    assert bt.Locator(selector='a:has(h3)', has_text="Docs").has_text == "Docs"


def test_failed_research_returns_sources_with_honest_failure_reason():
    orchestrator, _, _ = ready()
    extraction(orchestrator, "Official documentation describes how checkpoints retain task state and human approvals.")
    result = orchestrator._result("failed", reason="3 consecutive failures.")
    assert result.status == "failed" and "Reviewed sources" in result.answer
    assert "3 consecutive failures" in result.answer and "/continue" in result.answer
    assert "budget was reached" not in result.answer


def test_cancelled_inflight_call_is_retained_once_and_late_result_cannot_complete_it():
    import time
    orchestrator, _, _ = ready()
    gate = StepGate(max_actions=3, remaining_total_actions=10, on_record=orchestrator._register_action)
    kwargs = {"tab": "tab_1", "command": bt.Click(action="click", target=bt.RefTarget(kind="ref", ref="ref_1"))}
    gate.in_flight = ("browser_act", kwargs, time.perf_counter())
    orchestrator._commit_gate(gate)
    assert gate.unknown_outcome and len(orchestrator.state.actions) == 1
    assert orchestrator.state.counters.browser_actions == 1
    assert orchestrator.state.unverified_mutations == 1
    late = gate.record("browser_act", kwargs, {"ok": True, "data": {}})
    orchestrator._commit_gate(gate)
    assert not late.success and late.code == "BRIDGE_ERROR"
    assert len(gate.records) == len(orchestrator.state.actions) == 1
    assert orchestrator.state.counters.browser_actions == 1


def test_invalid_assertion_does_not_poison_completion_after_corrected_call():
    orchestrator, _, _ = ready()
    gate = StepGate(max_actions=3, remaining_total_actions=10, on_record=orchestrator._register_action)
    bad = bt.CountAssertion(assertion="count", locator=bt.Locator(selector="a["), count=3)
    invalid = gate.record("browser_assert", {"tab": "tab_1", "command": bad},
                          {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "Invalid CSS"}})
    corrected = bt.CountAssertion(assertion="count", locator=bt.Locator(selector="a"), count=3)
    gate.record("browser_assert", {"tab": "tab_1", "command": corrected},
                {"ok": True, "data": {"assertion": "count", "passed": True, "expected": 3}})
    orchestrator._commit_gate(gate)
    assert invalid.recovered and len(orchestrator.state.assertions) == 1
    assert verify_completion(orchestrator.state, None).valid
