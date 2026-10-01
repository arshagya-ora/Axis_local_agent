"""Deterministic regressions for completion evidence, task facts and progress.

No external browser or provider is required; model-driven checks use the same
FunctionModel harness as the existing orchestrator suite.
"""

from datetime import datetime, timezone
import json
import time

import pytest

from test_axis_agent import FakeBridge, Script, browse, done, make
import browser_tools as bt
from axis.agents import StepGate, _extracted, browser_context
from axis.models import ActionRecord, BrowserState, GoalCheck, TaskFact, TaskRequirements, TaskRunState
from axis.orchestrator import verify_completion


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ready(*, script=None, bridge=None):
    script = script or Script([browse()], [{"outcome": {"status": "continue"}}])
    orchestrator, bridge, config = make(script, bridge=bridge)
    orchestrator.state = config.new_memory("Read and compare the available records.", TaskRequirements())
    orchestrator.state.current_goal = "Read the records"
    orchestrator.state.current_goal_id = "goal_1"
    orchestrator.state.current_verification = GoalCheck(assertion="text", expected="Saved", target="#status")
    orchestrator.state.goal_number = 1
    alias = orchestrator.browser._register({"id": bridge.base_id, "url": bridge.url, "title": bridge.title, "active": True})
    orchestrator.tab = alias
    return orchestrator, bridge, script


def action(tool="browser_act", operation="click", *, tab="tab_1", **kwargs):
    return ActionRecord(tool=tool, operation=operation, tab=tab, execution_success=True, **kwargs)


def page(text="Ready", tab="tab_1"):
    return BrowserState(tab=tab, url="https://example.test/records", snapshot=text)


def extraction(orchestrator, value, *, target="#records", tab="tab_1"):
    record = action("browser_observe", "extract", tab=tab, target=target,
                    result_data={"result": value}, extracted_data=_extracted("browser_observe", {"result": value}))
    orchestrator._register_action(record, False)
    return record


def assertion(orchestrator, *, tab="tab_1", target="#status", passed=True, expected="Saved"):
    record = action("browser_assert", "text", tab=tab, target=json.dumps({"locator": {"selector": target}}),
                    semantic_success=passed, result_data={"expected": expected, "passed": passed})
    orchestrator._register_action(record, False)
    return record


def test_heading_role_is_a_valid_goal_check_target():
    orchestrator, _, _ = ready()
    orchestrator.state.current_verification = GoalCheck(assertion="text", expected="Step 2", target="heading")
    orchestrator._register_action(action(), True)
    record = action("browser_assert", "text", target=json.dumps({"locator": {"role": "heading", "exact": True}}),
                    semantic_success=True, result_data={"expected": "Step 2", "passed": True})
    orchestrator._register_action(record, False)
    assert not orchestrator.state.pending_changes


def test_inferred_check_can_be_corrected_but_requires_a_further_verification():
    orchestrator, _, _ = ready()
    state = orchestrator.state
    orchestrator._register_action(action(), True)
    assertion(orchestrator, passed=False)
    actual = assertion(orchestrator, expected="Saved successfully")
    corrected = GoalCheck(assertion="text", expected="Saved successfully", target="#status")
    assert orchestrator._refine_verification(corrected)
    assert state.assertions[0].superseded_by == actual.evidence_id
    assert not verify_completion(state, None).valid
    assertion(orchestrator, expected="Saved successfully")
    assert verify_completion(state, None).valid


@pytest.mark.parametrize("invalid", ["old", "other_tab", "other_goal", "prose", "regex", "user_literal", "changed_target"])
def test_inferred_check_correction_requires_matching_runtime_evidence(invalid):
    orchestrator, _, _ = ready()
    state = orchestrator.state
    if invalid == "old":
        assertion(orchestrator, expected="Saved successfully")
    orchestrator._register_action(action(), True)
    if invalid == "prose":
        state.add_evidence("navigator", "Saved successfully")
    elif invalid != "old":
        assertion(orchestrator, expected="Saved successfully", tab="tab_2" if invalid == "other_tab" else "tab_1")
        if invalid == "other_goal":
            state.assertions[-1].goal_id = "goal_2"
        if invalid == "regex":
            state.assertions[-1].match = "regex"
    if invalid == "user_literal":
        state.original_request = 'Require the confirmation "Saved".'
    corrected = GoalCheck(assertion="text", expected="Saved successfully",
                          target="#other" if invalid == "changed_target" else "#status")
    assert not orchestrator._refine_verification(corrected)
    assert state.pending_changes


def test_correcting_inferred_wording_preserves_other_target_failures():
    orchestrator, _, _ = ready()
    orchestrator._register_action(action(), True)
    assertion(orchestrator, target="#other", passed=False)
    assertion(orchestrator, passed=False)
    assertion(orchestrator, expected="Saved successfully")
    assert orchestrator._refine_verification(GoalCheck(assertion="text", expected="Saved successfully", target="#status"))
    assertion(orchestrator, expected="Saved successfully")
    assert not orchestrator.state.assertions[0].superseded_by
    assert not verify_completion(orchestrator.state, None).valid


def test_diagnostic_details_survive_planner_passes_as_sourced_facts():
    orchestrator, _, _ = ready()
    record = action("browser_diagnose", "network", result_data={"events": [{"url": "/failed", "status": 503}]})
    orchestrator._register_action(record, False)
    fact = orchestrator.state.facts[-1]
    assert "503" in fact.value and fact.evidence_id == record.evidence_id
    assert fact.goal_id == "goal_1" and fact.target == "network" and fact.priority == 1


def test_model_prose_and_uncited_page_content_cannot_verify_required_text_or_url():
    url = "https://example.test/receipt"
    state = TaskRunState(requirements=TaskRequirements(required_text=["Paid"], literal_urls=[url]))
    state.add_evidence("navigator", f"Paid at {url}", verified=False)
    # A caller-created BrowserState is not runtime page evidence merely because
    # its fields contain the expected words.
    verdict = verify_completion(state, BrowserState(url=url, snapshot="Paid"))
    assert not verdict.valid
    assert any("Required text" in reason for reason in verdict.reasons)
    assert any("Required URL" in reason for reason in verdict.reasons)


def test_literal_url_in_page_text_does_not_count_as_visited_url():
    state = TaskRunState(requirements=TaskRequirements(literal_urls=["https://example.test/receipt"]))
    state.add_evidence("observation", "Link to https://example.test/receipt", source_url="https://example.test/home")
    assert not verify_completion(state, None).valid
    state.add_evidence("navigation", "Visited receipt", source_url="https://example.test/receipt")
    assert verify_completion(state, None).valid


def test_runtime_observation_supplies_citable_required_text_evidence():
    state = TaskRunState(requirements=TaskRequirements(required_text=["Paid"]))
    evidence = state.add_evidence("observation", "Payment status: Paid", tab="tab_1", source_url="https://example.test/receipt")
    assert evidence.id and evidence.sequence > 0
    assert verify_completion(state, None).valid


@pytest.mark.parametrize("variant", ["empty", "old", "interrupted", "missing_time", "missing_file", "ineligible"])
def test_only_completed_downloads_from_the_task_window_satisfy_completion(variant):
    orchestrator, _, _ = ready()
    state = orchestrator.state
    state.requirements.require_download = True
    state.add_evidence("observation", "Download report", tab="tab_1")
    item = {"filename": "report.csv", "state": "complete", "exists": True,
            "startTime": datetime.fromtimestamp(state.started_wallclock + 1, timezone.utc).isoformat()}
    if variant == "old":
        item["startTime"] = datetime.fromtimestamp(state.started_wallclock - 1, timezone.utc).isoformat()
    elif variant == "interrupted":
        item["state"] = "interrupted"
    elif variant == "missing_time":
        item.pop("startTime")
    elif variant == "missing_file":
        item["exists"] = False
    elif variant == "ineligible":
        item["taskEligible"] = False
    record = action("browser_downloads", "list", result_data={"items": [] if variant == "empty" else [item]})
    orchestrator._register_action(record, False)
    assert not state.downloads
    assert not verify_completion(state, None).valid


@pytest.mark.parametrize("operation,key", [("list", "items"), ("wait", "download")])
def test_current_completed_download_is_accepted_with_provenance(operation, key):
    orchestrator, _, _ = ready()
    state = orchestrator.state
    state.requirements.require_download = True
    state.current_verification = GoalCheck(assertion="download", expected="report.csv")
    state.add_evidence("observation", "Download report", tab="tab_1")
    item = {"filename": "report.csv", "state": "complete", "exists": True,
            "startTime": datetime.fromtimestamp(state.started_wallclock + 1, timezone.utc).isoformat()}
    record = action("browser_downloads", operation, result_data={key: [item] if key == "items" else item})
    orchestrator._register_action(record, False)
    assert len(state.downloads) == 1
    assert state.downloads[0].goal_id == "goal_1"
    assert state.downloads[0].id == record.evidence_id
    assert verify_completion(state, None).valid


@pytest.mark.anyio
async def test_observation_and_other_tab_assertion_do_not_clear_a_mutation():
    orchestrator, _, _ = ready()
    orchestrator.browser._register({"id": 99, "url": "https://example.test/unrelated", "active": False})
    orchestrator._register_action(action(), True)
    observed = await orchestrator._browser_state()
    assert observed.unverified_page_changes == 1
    assertion(orchestrator, tab="tab_2")
    assert orchestrator.state.unverified_mutations == 1
    assert not verify_completion(orchestrator.state, observed).valid
    assertion(orchestrator)
    assert orchestrator.state.unverified_mutations == 0
    assert verify_completion(orchestrator.state, observed).valid


@pytest.mark.anyio
async def test_old_assertion_and_another_goals_assertion_do_not_clear_new_changes():
    orchestrator, _, _ = ready()
    old = assertion(orchestrator)
    mutation = action()
    orchestrator._register_action(mutation, True)
    assert old.sequence < mutation.sequence
    await orchestrator._browser_state()
    assert orchestrator.state.unverified_mutations == 1
    orchestrator.state.current_goal_id = "goal_2"
    assertion(orchestrator)
    assert orchestrator.state.unverified_mutations == 1
    orchestrator.state.current_goal_id = "goal_1"
    assertion(orchestrator)
    assert orchestrator.state.unverified_mutations == 0


def test_same_goal_unrelated_assertion_or_wrong_expected_value_does_not_verify_changes():
    orchestrator, _, _ = ready()
    orchestrator._register_action(action(), True)
    assertion(orchestrator, target="#unrelated-widget")
    assert orchestrator.state.unverified_mutations == 1
    assertion(orchestrator, expected="Not saved")
    assert orchestrator.state.unverified_mutations == 1
    assertion(orchestrator)
    assert orchestrator.state.unverified_mutations == 0


def test_arbitrary_assertion_without_declared_goal_check_does_not_verify_click():
    orchestrator, _, _ = ready()
    orchestrator.state.current_verification = None
    orchestrator._register_action(action(), True)
    assertion(orchestrator)
    assert orchestrator.state.unverified_mutations == 1


def test_assertion_identity_includes_locator_so_unrelated_success_cannot_hide_failure():
    orchestrator, _, _ = ready()
    gate = StepGate(max_actions=3, remaining_total_actions=3, on_record=orchestrator._register_action)
    for selector, passed in [("#invoice-status", False), ("#delivery-status", True)]:
        gate.record("browser_assert", {"tab": "tab_1", "command": bt.TextAssertion(
            assertion="text", locator=bt.Locator(selector=selector), expected="Saved")},
            {"ok": True, "data": {"assertion": "text", "expected": "Saved", "passed": passed}})
    first, second = orchestrator.state.assertions
    assert first.target != second.target
    verdict = verify_completion(orchestrator.state, None)
    assert not verdict.valid
    assert any("assertion" in reason.lower() for reason in verdict.reasons)


@pytest.mark.parametrize("value", [0, [], "", False, ["Alice", "Bob"], {"count": 0, "rows": []}])
def test_extraction_preserves_valid_empty_scalar_and_structured_values(value):
    orchestrator, bridge, _ = ready()
    record = extraction(orchestrator, value)
    fact = orchestrator.state.facts[-1]
    assert json.loads(fact.value) == value
    assert fact.source_url == bridge.url
    assert fact.tab == "tab_1" and fact.goal_id == "goal_1"
    assert fact.target == "#records" and fact.evidence_id == record.evidence_id


def test_repeated_extraction_replaces_same_source_target_and_preserves_other_tab_facts():
    orchestrator, _, _ = ready()
    orchestrator.browser._register({"id": 99, "url": "https://example.test/second", "active": False})
    extraction(orchestrator, ["old"])
    extraction(orchestrator, ["new"])
    extraction(orchestrator, 0, tab="tab_2")
    assert len(orchestrator.state.facts) == 2
    assert [json.loads(fact.value) for fact in orchestrator.state.facts] == [["new"], 0]


@pytest.mark.anyio
async def test_retained_facts_are_available_in_both_agent_contexts_after_tab_switch():
    orchestrator, bridge, script = ready()
    extraction(orchestrator, {"account": "Retained-Account-72", "balance": 0})
    original_id = orchestrator.state.facts[0].evidence_id
    bridge.extra_tabs = [{"id": 99, "url": "https://example.test/other-records", "title": "Other records", "active": True}]
    orchestrator.tab = orchestrator.browser._register(bridge.extra_tabs[0])
    await orchestrator._plan()
    browser_state = await orchestrator._browser_state()
    await orchestrator._navigate(browser_state)
    for prompt in (script.planner_prompts[-1], script.navigator_prompts[-1]):
        assert "Retained-Account-72" in prompt and original_id in prompt
        assert "goal_1" in prompt and "#records" in prompt
        assert "facts" in prompt
    assert browser_state.tab == "tab_2"


def test_fact_count_and_character_budget_are_bounded_and_evictions_are_reported():
    state = TaskRunState()
    for index in range(20):
        state.retain_fact(TaskFact(value=str(index), target=str(index), source_url="https://example.test"))
    assert len(state.facts) == 16
    assert state.memory_truncated
    state = TaskRunState()
    for index in range(16):
        state.retain_fact(TaskFact(value="x" * 3_000, target=str(index), source_url="https://example.test"))
    assert sum(len(fact.model_dump_json()) for fact in state.facts) <= 32_000
    assert state.memory_truncated


def test_fact_budget_includes_source_and_target_metadata():
    state = TaskRunState()
    for index in range(16):
        state.retain_fact(TaskFact(value="retained value", target=str(index) + "x" * 2_000,
                                   source_url="https://example.test/" + "y" * 1_000))
    assert sum(len(fact.model_dump_json()) for fact in state.facts) <= 32_000
    assert state.memory_truncated


def test_multiple_results_from_one_action_have_distinct_evidence_ids():
    state = TaskRunState(current_goal_id="goal_1")
    sequence = state.next_sequence()
    first = state.add_evidence("download", "first file", sequence=sequence, tab="tab_1")
    second = state.add_evidence("download", "second file", sequence=sequence, tab="tab_1")
    assert first.id != second.id
    assert first.sequence == second.sequence == sequence


@pytest.mark.parametrize("check", [
    {"assertion": "text"}, {"assertion": "checked", "expected": True},
    {"assertion": "count", "expected": True, "target": "Rows"},
    {"assertion": "download", "expected": ""},
    {"assertion": "visible", "expected": False, "target": "Submit"},
])
def test_goal_verification_requires_a_concrete_result_and_element_target(check):
    with pytest.raises(ValueError):
        GoalCheck(**check)


def test_fact_priorities_keep_completed_goal_results_before_page_snapshots():
    state = TaskRunState(current_goal_id="goal_1")
    state.retain_fact(TaskFact(value="important", goal_id="goal_1", target="total", priority=1))
    state.complete_goal("Read the total")
    for index in range(20):
        state.retain_fact(TaskFact(value=f"snapshot {index}", target=f"page-{index}", priority=0))
    assert any(fact.value == "important" for fact in state.facts)
    assert next(fact for fact in state.facts if fact.value == "important").priority > 1
    assert state.memory_truncated


def test_oversized_individual_fact_has_explicit_truncation_information():
    state = TaskRunState()
    state.retain_fact(TaskFact(value="x" * 10_000))
    assert len(state.facts[0].value) < 10_000
    assert state.memory_truncated or "chars]" in state.facts[0].value


def test_changed_snapshots_show_progress_even_when_action_feedback_is_missing():
    orchestrator, _, _ = ready()
    before = page("Rows: 1")
    orchestrator._update_progress(before, [])
    for number in range(2, 5):
        orchestrator._update_progress(before, [action(meaningful_change=None)])
        before = page(f"Rows: {number}")
        orchestrator._update_progress(before, [])
        assert orchestrator.state.made_progress
        assert orchestrator.state.counters.no_progress_steps == 0


def test_page_fingerprint_ignores_regenerated_refs_and_insignificant_whitespace():
    orchestrator, _, _ = ready()
    first = page('[ref_1] button "Next"\n[ref_2] text "Pending"')
    second = page(' [ref_301] button  "Next"\n\n[ref_302] text "Pending" ')
    assert orchestrator._page_fingerprint(first, []) == orchestrator._page_fingerprint(second, [])


def test_explicit_reobservation_with_new_refs_does_not_hide_ineffective_actions():
    orchestrator, _, _ = ready()
    observed = page('[ref_1] button "Next"')
    orchestrator._update_progress(observed, [])
    for count in range(1, 4):
        snapshot = f'  [ref_{count + 10}] button   "Next" '
        reread = action("browser_observe", "compact", source_url=observed.url,
                        extracted_data=snapshot, result_data={"snapshot": snapshot})
        orchestrator._update_progress(observed, [action(target='button "Next"'), reread])
        orchestrator._update_progress(page(snapshot), [])
        assert orchestrator.state.counters.no_progress_steps == count


def test_new_screenshot_alias_does_not_disguise_the_same_ineffective_visual_click():
    orchestrator, _, _ = ready()
    observed = page("Unchanged canvas")
    orchestrator._update_progress(observed, [])
    for count in range(1, 4):
        click = action("browser_visual", "click", args=f"screenshot='screenshot_{count}', x=20, y=30")
        orchestrator._update_progress(observed, [click])
        orchestrator._update_progress(observed, [])
        assert orchestrator.state.counters.no_progress_steps == count


def test_three_equivalent_ineffective_actions_stop_and_changed_targets_reset_streak():
    orchestrator, _, _ = ready()
    observed = page("No updates")
    orchestrator._update_progress(observed, [])
    for count in range(1, 4):
        orchestrator._update_progress(observed, [action(target='button "Next"')])
        orchestrator._update_progress(observed, [])
        assert orchestrator.state.counters.no_progress_steps == count
    result = orchestrator._stop_reason()
    assert result and result.status == "failed" and "NO_PROGRESS" in result.reason
    orchestrator.state.status = "active"
    orchestrator._update_progress(observed, [action(target='button "Previous"')])
    orchestrator._update_progress(observed, [])
    assert orchestrator.state.counters.no_progress_steps == 1


def test_delayed_change_and_changes_on_another_tab_are_compared_to_the_correct_action():
    orchestrator, _, _ = ready()
    first, second = page("Waiting"), page("Unrelated", tab="tab_2")
    orchestrator._update_progress(first, [])
    orchestrator._update_progress(first, [action()])
    orchestrator._update_progress(second, [])
    assert not orchestrator.state.made_progress
    orchestrator._update_progress(page("Loaded"), [])
    assert orchestrator.state.made_progress
    assert orchestrator.state.counters.no_progress_steps == 0


def test_reference_invalidation_alone_is_not_positive_action_feedback():
    orchestrator, _, _ = ready()
    observed = page("Unchanged")
    orchestrator._update_progress(observed, [])
    for _ in range(3):
        gate = StepGate(max_actions=1, remaining_total_actions=10, on_record=orchestrator._register_action)
        gate.record("browser_act", {"tab": "tab_1", "command": bt.Click(action="click",
                    target=bt.LocatorTarget(kind="locator", locator=bt.Locator(selector="#next")))},
                    {"ok": True, "data": {"whatChanged": {}, "refsInvalidated": True}})
        orchestrator._update_progress(observed, gate.records)
        orchestrator._update_progress(observed, [])
    assert orchestrator.state.counters.no_progress_steps == 3


class TwoControlBridge(FakeBridge):
    def rpc(self, method, params=None, **kwargs):
        if method == "page.accessibilityTree":
            self.observations += 1
            self.calls.append((method, params or {}))
            return {"snapshotId": f"snapshot-{self.observations}", "url": self.url, "title": self.title,
                    "snapshot": '[f0:node-a] button "Next"\n[f0:node-b] button "Previous"'}
        return super().rpc(method, params, **kwargs)


@pytest.mark.anyio
async def test_different_ref_targets_do_not_collide_when_refs_are_reissued():
    script = Script([browse()], [{"calls": [("browser_act", {"tab": "tab_1", "command": {
        "action": "click", "target": {"kind": "ref", "ref": ref}}})],
        "outcome": {"status": "continue"}} for ref in ("ref_1", "ref_4")])
    orchestrator, _, _ = ready(script=script, bridge=TwoControlBridge())
    records = []
    for _ in range(2):
        observed = await orchestrator._browser_state()
        orchestrator._update_progress(observed, [])
        _, gate = await orchestrator._navigate(observed)
        records.extend(gate.records)
        orchestrator._update_progress(observed, gate.records)
    orchestrator._update_progress(await orchestrator._browser_state(), [])
    assert len(records) == 2 and all(record.success for record in records)
    assert orchestrator.state.counters.no_progress_steps == 1


class FixedLengthScript(Script):
    def _respond(self, messages, info):
        if not info.function_tools:
            self.planner = [done("Read twelve results.") if self.navigator_calls >= 12 else browse("Read twelve results")]
        return super()._respond(messages, info)


@pytest.mark.anyio
async def test_adaptive_cadence_reduces_planner_calls_without_changing_outcomes_or_action_budget():
    runs = []
    for maximum_interval in (3, 6):
        script = FixedLengthScript([browse()], [{
            "calls": [("browser_observe", {"tab": "tab_1", "locator": {"selector": "#row-count"}, "extract": "count"})],
            "outcome": {"status": "goal_reached" if index == 11 else "continue"}
        } for index in range(12)])
        bridge = FakeBridge()
        bridge.responses["locator.count"] = lambda: {"count": sum(method == "locator.count" for method, _ in bridge.calls)}
        orchestrator, _, config = make(script, bridge=bridge)
        config.run.planner_interval_steps = 3
        config.run.planner_max_interval_steps = maximum_interval
        result = await orchestrator.run("Read twelve changing row counts.")
        assert result.status == "completed"
        assert result.total_steps == 12 and result.browser_actions == 12
        assert result.browser_actions <= config.run.max_browser_actions
        runs.append(result)
    fixed, adaptive = runs
    assert adaptive.answer == fixed.answer
    assert adaptive.planner_passes < fixed.planner_passes
    assert adaptive.model_requests < fixed.model_requests


@pytest.mark.anyio
async def test_slow_automatic_observation_does_not_erase_model_latency_or_token_usage():
    class DelayedScript(Script):
        def _respond(self, messages, info):
            time.sleep(0.025)
            return super()._respond(messages, info)

    class SlowObservation(FakeBridge):
        def rpc(self, method, params=None, **kwargs):
            if method == "page.accessibilityTree":
                time.sleep(0.15)
            return super().rpc(method, params, **kwargs)

    script = DelayedScript([browse("Read the current page"), done("Read the fixture.")],
                           [{"outcome": {"status": "goal_reached"}}])
    orchestrator, _, _ = make(script, bridge=SlowObservation())
    result = await orchestrator.run("Read the current page.")
    assert result.status == "completed" and result.model_requests == 3
    assert result.browser_ms >= 140
    assert result.model_ms >= 50
    assert result.duration_ms >= result.browser_ms
    assert result.input_tokens > 0 and result.output_tokens > 0


@pytest.mark.parametrize("condition,expected", [("checked", True), ("checked", False), ("visible", True), ("disabled", True)])
def test_boolean_state_assertions_verify_the_declared_goal_result(condition, expected):
    orchestrator, bridge, _ = ready()
    orchestrator.state.current_verification = GoalCheck(assertion=condition, expected=expected, target="Newsletter")
    orchestrator._register_action(action(operation="check" if expected else "uncheck"), True)
    command = bt.StateAssertion(assertion=condition, expected=expected,
                                locator=bt.Locator(role="checkbox", name="Newsletter"))
    result = bt.browser_assert(browser_context(orchestrator.browser), "tab_1", command)
    assert result["ok"] and result["data"]["passed"]
    assert result["data"]["expected"] is expected
    if condition == "checked":
        method, params = bridge.calls[-1]
        assert method == "expect.locator.toBeChecked" and params["checked"] is expected
    gate = StepGate(max_actions=1, remaining_total_actions=1, on_record=orchestrator._register_action)
    gate.record("browser_assert", {"tab": "tab_1", "command": command}, result)
    assert orchestrator.state.unverified_mutations == 0
    assert verify_completion(orchestrator.state, None).valid


def test_checked_true_cannot_verify_a_goal_to_uncheck_the_control():
    orchestrator, _, _ = ready()
    orchestrator.state.current_verification = GoalCheck(assertion="checked", expected=False, target="Newsletter")
    orchestrator._register_action(action(operation="uncheck"), True)
    command = bt.StateAssertion(assertion="checked", expected=True,
                                locator=bt.Locator(role="checkbox", name="Newsletter"))
    result = bt.browser_assert(browser_context(orchestrator.browser), "tab_1", command)
    gate = StepGate(max_actions=1, remaining_total_actions=1, on_record=orchestrator._register_action)
    gate.record("browser_assert", {"tab": "tab_1", "command": command}, result)
    assert orchestrator.state.unverified_mutations == 1


@pytest.mark.anyio
async def test_continue_after_no_progress_replans_while_preserving_extracted_facts():
    script = Script([done("Recovered using the retained record.")])
    orchestrator, _, _ = ready(script=script)
    record = extraction(orchestrator, {"retained": "Keep this result"})
    orchestrator.state.counters.no_progress_steps = 3
    orchestrator.state.counters.repeated_actions = 3
    orchestrator.state.counters.repeated_observations = 3
    orchestrator.state.progress_by_tab = {"tab_1": {"streak": 3, "last_action": "old attempt"}}
    orchestrator.state.status = "failed"
    result = await orchestrator.continue_task("Use the retained record and a different strategy.")
    assert result.status == "completed"
    assert script.planner_calls == 1
    assert orchestrator.state.counters.no_progress_steps == 0
    assert orchestrator.state.counters.repeated_actions == 0
    assert orchestrator.state.counters.repeated_observations == 0
    assert not orchestrator.state.progress_by_tab
    assert any(fact.evidence_id == record.evidence_id for fact in orchestrator.state.facts)
    assert "Keep this result" in script.planner_prompts[-1]


def test_tabless_download_result_verifies_only_the_current_tabs_pending_changes():
    orchestrator, _, _ = ready()
    state = orchestrator.state
    state.current_verification = GoalCheck(assertion="download", expected="report.csv")
    orchestrator.browser._register({"id": 99, "url": "https://example.test/other", "active": False})
    orchestrator._register_action(action(tab="tab_1"), True)
    orchestrator._register_action(action(tab="tab_2"), True)
    item = {"filename": "report.csv", "state": "complete", "exists": True,
            "startTime": datetime.fromtimestamp(state.started_wallclock + 1, timezone.utc).isoformat()}
    record = action("browser_downloads", "list", tab=None, result_data={"items": [item]})
    orchestrator._register_action(record, False)
    assert record.tab == "tab_1"
    assert state.downloads[-1].tab == "tab_1"
    assert state.unverified_mutations == 1
    assert [pending.tab for pending in state.pending_changes.values()] == ["tab_2"]


def test_exact_url_assertion_verifies_the_declared_navigation_goal():
    orchestrator, bridge, _ = ready()
    expected = "https://example.test/receipt?order=42"
    orchestrator.state.current_verification = GoalCheck(assertion="url", expected=expected)
    orchestrator._register_action(action(), True)
    bridge.responses["page.waitForURL"] = {"ok": True, "url": expected}
    command = bt.URLAssertion(assertion="url", expected=expected)
    result = bt.browser_assert(browser_context(orchestrator.browser), "tab_1", command)
    gate = StepGate(max_actions=1, remaining_total_actions=1, on_record=orchestrator._register_action)
    gate.record("browser_assert", {"tab": "tab_1", "command": command}, result)
    assert orchestrator.state.unverified_mutations == 0
