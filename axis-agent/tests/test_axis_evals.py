"""The workflow harness is deterministic here; real provider runs are opt-in."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axis.models import AxisConfig, AxisResult
from browser_bridge_client import BrowserBridgeError
from evals import run
from evals.axis_quality import AXIS_QUALITY_DATASET, quality_dataset


def http(origin, path, data=None):
    request = Request(origin + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        return response.read().decode()


class FakeBridge:
    def __init__(self):
        self.tabs = {99: {"id": 99, "url": "https://user.example/private", "active": True}}
        self.next_id = 100
        self.calls = []
        self.fail_close = False

    def health(self):
        return {"hostReady": True, "extensionReady": True}

    def rpc(self, method, params=None, **kwargs):
        params = params or {}
        self.calls.append((method, params))
        if method == "tabs.list":
            return {"tabs": list(self.tabs.values())}
        if method == "tabs.create":
            self.next_id += 1
            tab = {"id": self.next_id, "url": params["url"], "active": True}
            self.tabs[tab["id"]] = tab
            with urlopen(params["url"], timeout=3) as response:
                response.read()
            return tab
        if method == "tabs.close":
            if self.fail_close:
                raise BrowserBridgeError("Disconnected")
            self.tabs.pop(params["tabId"])
        if method == "page.waitForLoad":
            self.tabs[params["tabId"]]["status"] = "complete"
            return {"tab": dict(self.tabs[params["tabId"]])}
        if method in {"page.readText", "page.accessibilityTree"}:
            tab = self.tabs[params["tabId"]]
            with urlopen(tab["url"], timeout=3) as response:
                html = response.read().decode()
            html = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", html)
            return {"text": text, "snapshot": text, "url": tab["url"]}
        if method == "downloads.list":
            return {"items": [
                {"url": "https://user.example/private.pdf", "state": "complete"},
                {"url": params.get("testOrigin", "") + "/eval/report.csv", "state": "complete"},
            ]}
        return {}


def test_dataset_has_twelve_concrete_cases_and_judge_is_lazy(monkeypatch):
    import pydantic_evals.evaluators
    monkeypatch.setattr(pydantic_evals.evaluators, "LLMJudge",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("judge constructed")))
    dataset = quality_dataset()
    assert len(dataset.cases) == 12
    for case in dataset.cases:
        assert run.fixture.quality_page(case.inputs.case_name) is not None
        assert case.inputs.initial_url.endswith("/eval/" + case.name)
    assert len(quality_dataset(["canvas_interaction"]).cases) == 1
    with pytest.raises(ValueError, match="Unknown evaluation"):
        quality_dataset(["missing"])


def test_fixture_server_retains_existing_routes_and_counts_real_submissions():
    with run.fixture_server("changing_spa_content") as (origin, state):
        assert "Axis POC Test Harness" in http(origin, "/")
        assert "Step 1" in http(origin, "/eval/changing_spa_content")
        assert "Exact report 42" in http(origin, "/eval/report?case=Exact-42")
        with pytest.raises(HTTPError) as error:
            http(origin, "/eval/report?case=exact-42")
        assert error.value.code == 404
        for _ in range(2):
            assert json.loads(http(origin, "/eval/action", {"action": "finish", "value": True}))["ok"]
        assert state.snapshot()["actions"]["finish"] == 2
        assert state.snapshot()["values"]["finish"] is True
        assert json.loads(http(origin, "/eval/state"))["actions"]["finish"] == 2


def test_all_twelve_pages_and_download_are_real_http_fixtures():
    with run.fixture_server("manual") as (origin, state):
        for case in AXIS_QUALITY_DATASET.cases:
            assert "<html" in http(origin, "/eval/" + case.name)
        assert "31.50" in http(origin, "/eval/axis-eval-report.csv")
        assert state.snapshot()["actions"]["download"] == 1
        with pytest.raises(HTTPError) as error:
            http(origin, "/eval/unavailable")
        assert error.value.code == 503


def test_scoped_bridge_hides_user_tabs_and_blocks_external_navigation():
    client = FakeBridge()
    with run.fixture_server("grounded_final_answer") as (origin, _):
        scope = run.ScopedBridge(client, origin, "grounded_final_answer")
        assert scope.rpc("tabs.list")["tabs"] == []
        tab = scope.rpc("tabs.create", {"url": origin + "/eval/grounded_final_answer"})
        assert scope.rpc("tabs.list")["tabs"] == [tab]
        for method, params in [
            ("page.navigate", {"tabId": tab["id"], "url": "https://example.org"}),
            ("tabs.close", {"tabId": 99}),
            ("tabs.create", {"url": "about:blank"}),
            ("tabs.create", {"url": origin + ".evil.example/page"}),
            ("native.saveDataUrl", {}),
        ]:
            with pytest.raises(BrowserBridgeError, match="Evaluation permits"):
                scope.rpc(method, params)
        scope.close()
        assert list(client.tabs) == [99]


def test_scoped_downloads_and_cleanup_failure_are_reported():
    client = FakeBridge()
    with run.fixture_server("download_completion") as (origin, _):
        scope = run.ScopedBridge(client, origin, "download_completion")
        scope.rpc("tabs.create", {"url": origin + "/eval/download_completion"})
        items = scope.rpc("downloads.list", {"testOrigin": origin})["items"]
        assert len(items) == 1 and items[0]["url"].startswith(origin)
        client.fail_close = True
        scope.close()
        assert len(scope.cleanup_errors) == 1
        assert 99 in client.tabs


class ProvisionalBridge(FakeBridge):
    def __init__(self, *, timeout=False, redirect=None):
        super().__init__()
        self.pending = {}
        self.timeout = timeout
        self.redirect = redirect

    def rpc(self, method, params=None, **kwargs):
        params = params or {}
        if method == "tabs.create":
            raw = super().rpc(method, params, **kwargs)
            self.pending[raw["id"]] = raw["url"]
            self.tabs[raw["id"]].update(url="", status="loading")
            return {"tab": dict(self.tabs[raw["id"]])}
        if method == "page.waitForLoad":
            if self.timeout:
                self.calls.append((method, params))
                raise BrowserBridgeError("Timed out waiting for page load", {"code": "PAGE_WAIT_TIMEOUT"})
            self.tabs[params["tabId"]].update(
                url=self.redirect or self.pending[params["tabId"]], title="Loaded fixture", status="complete")
        return super().rpc(method, params, **kwargs)


def test_created_tabs_wait_for_load_and_return_confirmed_metadata_before_listing():
    client = ProvisionalBridge()
    with run.fixture_server("multi_tab_memory") as (origin, _):
        scope = run.ScopedBridge(client, origin, "multi_tab_memory")
        for path in ("/eval/multi_tab_memory", "/eval/product-beta"):
            tab = scope.rpc("tabs.create", {"url": origin + path})["tab"]
            assert tab["url"] == origin + path and tab["title"] == "Loaded fixture"
            assert tab["status"] == "complete"
        assert len(scope.rpc("tabs.list")["tabs"]) == 2
        waits = [params for method, params in client.calls if method == "page.waitForLoad"]
        assert len(waits) == 2 and all(params["timeoutMs"] == 10_000 for params in waits)
        scope.close()
        assert list(client.tabs) == [99]


def test_created_tab_load_does_not_bypass_origin_scope():
    client = ProvisionalBridge(redirect="https://outside.example/private")
    with run.fixture_server("grounded_final_answer") as (origin, _):
        scope = run.ScopedBridge(client, origin, "grounded_final_answer")
        with pytest.raises(BrowserBridgeError) as error:
            scope.rpc("tabs.create", {"url": origin + "/eval/grounded_final_answer"})
        assert error.value.data["code"] == "FIREWALL_DENIED"
        scope.close()
        assert list(client.tabs) == [99]


def test_initial_tab_load_timeout_is_unavailable_and_cleans_only_owned_tabs():
    client = ProvisionalBridge(timeout=True)
    runner = run.WorkflowRunner(AxisConfig.load(), client,
        factory=lambda *args, **kwargs: pytest.fail("must not run a model before the tab is ready"))
    row = asyncio.run(runner(quality_dataset(["grounded_final_answer"]).cases[0].inputs))
    assert row["prerequisite_unavailable"] and not row["success"]
    assert not row["false_completion"]
    assert any(call.get("error_code") == "PAGE_WAIT_TIMEOUT" for call in row["bridge_calls"])
    assert list(client.tabs) == [99]


def test_recovery_fixture_injects_one_real_reload_before_first_ref_action():
    client = FakeBridge()
    with run.fixture_server("unexpected_ui_recovery") as (origin, _):
        scope = run.ScopedBridge(client, origin, "unexpected_ui_recovery")
        tab = scope.rpc("tabs.create", {"url": origin + "/eval/unexpected_ui_recovery"})
        scope.rpc("locator.clickRef", {"tabId": tab["id"], "ref": "old-ref"})
        scope.rpc("locator.clickRef", {"tabId": tab["id"], "ref": "fresh-ref"})
        assert scope.stale_injected
        methods = [method for method, _ in client.calls]
        assert methods.count("page.reload") == 1
        assert methods.index("page.reload") < methods.index("locator.clickRef")
        scope.close()


def test_download_oracle_rejects_empty_interrupted_and_unrelated_results():
    result = {"status": "completed", "answer": "Downloaded"}
    state = {"actions": {"download": 1}}
    for payload in ({}, {"items": []}, {"items": [{"state": "interrupted", "filename": "axis-eval-report.csv"}]},
                    {"items": [{"state": "complete", "filename": "unrelated.csv"}]}):
        verdict = run.check_fixture("download_completion", result, state, [],
                                    [{"method": "downloads.list", "result": payload}])
        assert not verdict["success"] and verdict["false_completion"]
    verdict = run.check_fixture("download_completion", result, state, [], [{
        "method": "downloads.waitFor", "result": {"item": {
            "state": "complete", "filename": "C:/Downloads/axis-eval-report.csv"}}}])
    assert verdict["success"]


def test_duplicate_submissions_fail_even_when_final_state_and_answer_are_correct():
    result = {"status": "completed", "answer": "Workflow complete"}
    state = {"actions": {"advance": 1, "finish": 2}, "values": {"advance": True, "finish": True}}
    verdict = run.check_fixture("changing_spa_content", result, state, [{"text": "Workflow complete"}], [])
    assert verdict["duplicate_actions"] == 1
    assert verdict["false_completion"] and not verdict["success"]


def test_false_completion_is_independent_of_agent_evidence_claims():
    verdict = run.check_fixture("correct_tool_selection",
        {"status": "completed", "answer": "Canada selected", "evidence": ["Country: Canada"]},
        {"values": {"country": "United States"}}, [{"text": "Country: United States"}], [])
    assert verdict["false_completion"] and not verdict["success"]


def test_memory_oracle_rejects_wrong_cheaper_product_even_with_correct_prices():
    state = {"visits": ["/eval/product-beta"]}
    pages = [{"text": "Alpha $31.50"}, {"text": "Beta $24.25"}]
    for answer in ("Alpha is $31.50, Beta is $24.25. Alpha is cheaper than Beta.",
                   "Alpha is $31.50, Beta is $24.25. Beta is not cheaper."):
        verdict = run.check_fixture("multi_tab_memory", {"status": "completed", "answer": answer}, state, pages, [])
        assert verdict["false_completion"]
    verdict = run.check_fixture("multi_tab_memory", {"status": "completed", "answer":
        "Alpha costs $31.50. Beta ($24.25) is cheaper."}, state, pages, [])
    assert verdict["success"]


def test_fixture_setup_failure_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(run, "fixture_server", lambda *args: (_ for _ in ()).throw(OSError("busy port")))
    runner = run.WorkflowRunner(AxisConfig.load(), FakeBridge())
    row = asyncio.run(runner(quality_dataset(["grounded_final_answer"]).cases[0].inputs))
    assert row["prerequisite_unavailable"] and not row["success"]
    assert runner.rows == [row]


def test_report_excludes_unavailable_runs_from_metrics_and_strips_image_bytes(tmp_path):
    rows = [
        {"case": "ok", "repeat": 1, "success": True, "latency_ms": 100,
         "result": {"model_requests": 4, "input_tokens": 123, "output_tokens": 45},
         "image": {"dataUrl": "data:image/png;base64,secret"}},
        {"case": "offline", "repeat": 1, "prerequisite_unavailable": True, "latency_ms": 9000},
    ]
    summary = run.write_reports(rows, tmp_path)
    assert summary["measured_runs"] == 1 and summary["success_rate"] == 1
    assert summary["median_latency_ms"] == 100
    assert summary["input_tokens"] == 123 and summary["output_tokens"] == 45
    assert "secret" not in (tmp_path / "results.json").read_text()
    assert "unavailable" in (tmp_path / "results.md").read_text()


def test_preflight_checks_both_prerequisites_without_provider_request(monkeypatch):
    monkeypatch.setattr(run, "build_model", lambda config: object())
    assert run.prerequisites(AxisConfig.load(), FakeBridge()) == []
    class Offline:
        def health(self):
            raise BrowserBridgeError("offline")
    monkeypatch.setattr(run, "build_model", lambda config: (_ for _ in ()).throw(ValueError("secret-key")))
    issues = run.prerequisites(AxisConfig.load(), Offline())
    assert {item["component"] for item in issues} == {"bridge", "provider"}
    assert "secret-key" not in json.dumps(issues)


def test_real_dataset_runs_sequentially_with_fresh_origins_and_writes_evals_report(tmp_path):
    client = FakeBridge()
    active = 0
    max_active = 0
    configs = []

    def factory(config, *, bridge, on_event):
        configs.append(config)
        async def execute(task):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            tab_id = next(iter(bridge.owned))
            bridge.rpc("page.accessibilityTree", {"tabId": tab_id})
            active -= 1
            return AxisResult(status="completed", answer="The invoice total is $184.27.", model_requests=2)
        return SimpleNamespace(run=execute)

    runner = run.WorkflowRunner(AxisConfig.load(), client, factory=factory)
    dataset = quality_dataset(["grounded_final_answer"])
    report = asyncio.run(dataset.evaluate(runner, max_concurrency=1, repeat=3, progress=False))
    assert len(report.cases) == 3 and not report.failures
    assert len(runner.rows) == 3 and all(row["success"] for row in runner.rows)
    assert max_active == 1
    assert len({row["origin"] for row in runner.rows}) == 3
    assert [row["repeat"] for row in runner.rows] == [1, 2, 3]
    assert list(client.tabs) == [99]
    for config, row in zip(configs, runner.rows):
        assert config.browser.firewall.default == "deny"
        assert config.browser.firewall.allow_urls == [row["origin"] + "/*"]
    run.write_reports(runner.rows, tmp_path, judge_report=report)
    serialized = json.loads((tmp_path / "pydantic-evals.json").read_text())
    assert len(serialized["cases"]) == 3


def test_cli_default_repeat_and_unavailable_exit_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "prerequisites", lambda *args: [{"component": "bridge", "reason": "offline"}])
    monkeypatch.setattr(run, "build_orchestrator", lambda *args, **kwargs: pytest.fail("must not execute"))
    assert run.main(["--case", "canvas_interaction", "--output", str(tmp_path)]) == 2
    payload = json.loads((tmp_path / "results.json").read_text())
    assert payload["summary"]["total_runs"] == 3
    assert payload["summary"]["measured_runs"] == 0
    with pytest.raises(SystemExit):
        run.main(["--repeat", "0"])
