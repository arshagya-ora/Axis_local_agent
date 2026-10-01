"""Run isolated local browser workflows: python -m evals.run --repeat 3.

Requires the existing development dependencies, bridge, Chrome extension, and
AXIS provider configuration. Deterministic fixture checks are always enabled;
--judge adds an LLM judge using the same configured model.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from axis.agents import build_model
from axis.cli import build_orchestrator
from axis.models import AxisConfig
from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError

from .axis_quality import AXIS_QUALITY_DATASET, AxisQualityInput, quality_dataset

AGENT_DIR = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "axis_quality_fixture", AGENT_DIR / "tests" / "fixtures" / "poc_app" / "server.py",
)
assert _SPEC and _SPEC.loader
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)


@contextmanager
def fixture_server(case_name: str, used_origins: set[str] | None = None):
    """A fresh port also isolates cookies, local storage and downloads by URL."""
    for _ in range(100):
        server = fixture.ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        origin = f"http://127.0.0.1:{server.server_port}"
        if used_origins is None or origin not in used_origins:
            break
        server.server_close()
    else:
        raise RuntimeError("Could not allocate a fresh fixture origin.")
    if used_origins is not None:
        used_origins.add(origin)
    server.daemon_threads = True
    server.quality_state = fixture.QualityState(case_name)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield origin, server.quality_state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _report_value(value: Any) -> Any:
    """Keep screenshots out of traces and reports, including nested payloads."""
    if isinstance(value, dict):
        return {key: _report_value(item) for key, item in value.items()
                if key.lower() not in {"dataurl", "imagedata", "base64", "image_bytes", "bytes"}}
    if isinstance(value, list):
        return [_report_value(item) for item in value]
    if isinstance(value, str):
        return "[image omitted]" if value.startswith("data:image/") else value[:8_000]
    if isinstance(value, bytes):
        return "[bytes omitted]"
    return value


class ScopedBridge:
    """Only task-owned tabs and this fixture origin are visible to the agent."""

    def __init__(self, client: Any, origin: str, case_name: str):
        self.client, self.origin, self.case_name = client, origin, case_name
        self.owned: set[int] = set()
        self.calls: list[dict] = []
        self.stale_injected = False
        self.cleanup_errors: list[str] = []

    def allowed_url(self, url: str | None) -> bool:
        if not url:
            return False
        parsed, expected = urlsplit(url), urlsplit(self.origin)
        return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)

    @staticmethod
    def denied() -> None:
        raise BrowserBridgeError("Evaluation permits only its fixture origin and task-owned tabs.",
                                 {"code": "FIREWALL_DENIED"})

    def rpc(self, method: str, params: dict | None = None, **kwargs):
        params = dict(params or {})
        tab_id = params.get("tabId")
        if tab_id is not None and tab_id not in self.owned:
            self.denied()
        if params.get("url") and not self.allowed_url(params["url"]):
            self.denied()
        if method == "tabs.create" and not self.allowed_url(params.get("url")):
            self.denied()
        if method == "native.sitePatterns":
            return {"patterns": []}
        if method.startswith("native."):
            self.denied()
        if method == "downloads.waitFor":
            params["urlRegex"] = "^" + re.escape(self.origin + "/")
        if (self.case_name == "unexpected_ui_recovery" and method == "locator.clickRef"
                and not self.stale_injected):
            self.stale_injected = True
            self.client.rpc("page.reload", {"tabId": tab_id})
        entry = {"method": method, "params": _report_value(params)}
        self.calls.append(entry)
        try:
            result = self.client.rpc(method, params, **kwargs)
        except BrowserBridgeError as exc:
            entry["error_code"] = (exc.data or {}).get("code") if isinstance(exc.data, dict) else "BRIDGE_ERROR"
            raise
        if method == "tabs.create":
            raw = result.get("tab", result)
            if not isinstance(raw.get("id"), int):
                raise BrowserBridgeError("The bridge did not return a tab ID.")
            self.owned.add(raw["id"])
            # Chrome can return an empty URL while the newly-created tab is
            # loading. Wait on this owned tab before exposing its metadata;
            # substituting the requested URL would hide off-origin redirects.
            loaded = self.rpc("page.waitForLoad", {"tabId": raw["id"], "timeoutMs": 10_000})
            loaded_tab = loaded.get("tab") if isinstance(loaded, dict) else None
            if not isinstance(loaded_tab, dict) or loaded_tab.get("id") != raw["id"]:
                raise BrowserBridgeError("The bridge did not confirm the created tab finished loading.")
            if not self.allowed_url(loaded_tab.get("url")):
                self.denied()
            ready = {**raw, **loaded_tab}
            result = {**result, "tab": ready} if "tab" in result else ready
        elif method == "tabs.close":
            self.owned.discard(tab_id)
        elif method == "tabs.list":
            result = {**result, "tabs": [tab for tab in result.get("tabs", []) if tab.get("id") in self.owned]}
            if any(not self.allowed_url(tab.get("url")) for tab in result["tabs"]):
                self.denied()
        elif method == "downloads.list":
            result = {**result, "items": [item for item in result.get("items", [])
                                           if self.allowed_url(item.get("url"))]}
        elif method == "downloads.waitFor" and result.get("item"):
            if not self.allowed_url(result["item"].get("url")):
                self.denied()
        entry["result"] = _report_value(result)
        return result

    def save_data_url(self, *args, **kwargs):
        self.denied()

    def read_pages(self) -> list[dict]:
        live = self.rpc("tabs.list").get("tabs", [])
        pages = []
        for tab_id in sorted(tab["id"] for tab in live):
            result = self.client.rpc("page.readText", {"tabId": tab_id}) or {}
            pages.append({"tab_id": tab_id, **_report_value(result)})
        return pages

    def close(self) -> None:
        for tab_id in list(self.owned):
            try:
                self.client.rpc("tabs.close", {"tabId": tab_id})
                self.owned.discard(tab_id)
            except BrowserBridgeError:
                self.cleanup_errors.append(f"Could not close task-owned tab {tab_id}.")


def fixture_config(config: AxisConfig, origin: str, case_name: str) -> AxisConfig:
    local = config.model_copy(deep=True)
    local.browser.initial_tab = "focused"
    local.browser.max_open_tabs = 4
    local.browser.firewall.default = "deny"
    local.browser.firewall.allow_urls = [origin + "/*"]
    local.browser.firewall.deny_urls = []
    local.tools.capture_evidence = False
    local.tools.debug_recording = False
    local.tools.diagnose = case_name == "no_invented_diagnostics"
    local.tools.downloads = case_name == "download_completion"
    local.browser.allow_coordinate_fallback = case_name == "canvas_interaction"
    return local


def check_fixture(case_name: str, result: dict, state: dict, pages: list[dict], calls: list[dict],
                  *, stale_injected: bool = False) -> dict:
    """Evaluate independent state, observed browser results, and public actions."""
    answer = result.get("answer") or ""
    text = "\n".join(str(page.get("text", "")) for page in pages)
    actions, values = state.get("actions", {}), state.get("values", {})
    methods = [call["method"] for call in calls]
    visits = state.get("visits", [])
    checks: dict[str, bool] = {}
    if case_name == "literal_preservation":
        checks = {"exact_url_visited": "/eval/report?case=Exact-42" in visits,
                  "grounded_heading": "Exact report 42" in answer and "Exact report 42" in text}
    elif case_name == "planner_decision_quality":
        checks = {"title_answered": "Fixture Home" in answer, "page_observed": "page.accessibilityTree" in methods,
                  "no_mutations": not actions}
    elif case_name == "correct_tool_selection":
        checks = {"selected_canada": values.get("country") == "Canada" and "Country: Canada" in text,
                  "select_tool_used": any(method.startswith("locator.selectOption") for method in methods)}
    elif case_name == "ref_first_interaction":
        interactions = [method for method in methods if method.startswith(("locator.click", "computer."))]
        checks = {"step_two": values.get("continue") is True and "Step 2" in text,
                  "ref_first": bool(interactions) and interactions[0] == "locator.clickRef",
                  "heading_answered": "Step 2" in answer}
    elif case_name == "grounded_final_answer":
        checks = {"exact_total": "$184.27" in answer and "$184.27" in text, "no_mutations": not actions,
                  "agent_read_page": any(method in {"page.readText", "page.accessibilityTree", "page.ariaSnapshot",
                      "locator.textContent", "locator.allTextContents", "locator.allInnerTexts"} for method in methods)}
    elif case_name == "no_invented_diagnostics":
        diagnostic_calls = [call for call in calls if call["method"] in {"console.read", "network.read"}]
        diagnostics = json.dumps(diagnostic_calls)
        checks = {"diagnostics_generated": values.get("diagnostics") is True,
                  "both_tools_used": "console.read" in methods and "network.read" in methods,
                  "grounded_errors": all(item in diagnostics and item in answer for item in ("TypeError", "503"))}
    elif case_name == "unexpected_ui_recovery":
        checks = {"real_rerender_injected": stale_injected, "reobserved": methods.count("page.accessibilityTree") >= 2,
                  "compact_enabled": values.get("compact") is True and "Compact mode: enabled" in text}
    elif case_name == "cross_provider_consistency":
        checks = {"exact_status": answer.strip().strip(". ") == "Active", "observed_status": "Active" in text,
                  "no_mutations": not actions,
                  "agent_read_page": any(method in {"page.readText", "page.accessibilityTree", "page.ariaSnapshot",
                      "locator.textContent", "locator.allTextContents", "locator.allInnerTexts"} for method in methods)}
    elif case_name == "multi_tab_memory":
        comparison = re.sub(r"\$\d+(?:\.\d+)?", "", answer)
        beta_cheaper = re.search(
            r"\bBeta\b(?:(?!\bnot\b)[^.!?\n]){0,100}\b(?:cheaper|costs less|less expensive)\b|"
            r"\b(?:cheaper|less expensive)(?:\s+(?:product|option))?\s*(?:is|:|=)\s*\**Beta\b",
            comparison, re.I,
        )
        checks = {"two_tabs_retained": len(pages) >= 2, "both_pages_visited": "/eval/product-beta" in visits,
                  "both_prices": all(price in answer for price in ("31.50", "24.25")),
                  "correct_comparison": bool(beta_cheaper)}
    elif case_name == "changing_spa_content":
        checks = {"workflow_complete": values.get("advance") is True and values.get("finish") is True
                  and "Workflow complete" in text}
    elif case_name == "download_completion":
        downloads = []
        for call in calls:
            if call["method"] in {"downloads.list", "downloads.waitFor"}:
                data = call.get("result") or {}
                downloads.extend(data.get("items", []))
                if data.get("item"):
                    downloads.append(data["item"])
        checks = {"file_requested": actions.get("download", 0) > 0,
                  "completed_file_observed": any(item.get("state") == "complete"
                      and "axis-eval-report.csv" in item.get("filename", "") for item in downloads)}
    elif case_name == "canvas_interaction":
        checks = {"tile_confirmed": values.get("canvas_confirm") is True and "Canvas confirmed" in text,
                  "visual_used": any(method.startswith("computer.") for method in methods)}
    else:
        raise ValueError(f"Unknown fixture: {case_name}")
    duplicates = sum(max(0, count - 1) for count in actions.values())
    valid = all(checks.values()) and duplicates == 0
    completed = result.get("status") == "completed"
    return {"checks": checks, "success": completed and valid,
            "false_completion": completed and not valid, "duplicate_actions": duplicates}


def prerequisites(config: AxisConfig, client: Any) -> list[dict]:
    issues = []
    try:
        health = client.health()
        if not health.get("hostReady") or not health.get("extensionReady"):
            issues.append({"component": "bridge", "reason": "Bridge host and Chrome extension must both be ready."})
        else:
            client.rpc("tabs.list", {})  # Check authentication without modifying any tabs.
    except Exception as exc:
        issues.append({"component": "bridge", "reason": f"Bridge preflight failed ({type(exc).__name__})."})
    try:
        build_model(config)  # Validates provider configuration and signing setup; no model request.
    except Exception as exc:
        issues.append({"component": "provider", "reason": f"Provider setup is unavailable ({type(exc).__name__})."})
    return issues


class WorkflowRunner:
    def __init__(self, config: AxisConfig, client: Any, *, factory=build_orchestrator):
        self.config, self.client, self.factory = config, client, factory
        self.rows: list[dict] = []
        self.repetitions: dict[str, int] = {}
        self.used_origins: set[str] = set()

    async def __call__(self, inputs: AxisQualityInput) -> dict:
        name = inputs.case_name
        repeat = self.repetitions[name] = self.repetitions.get(name, 0) + 1
        print(f"[{name} #{repeat}] running", flush=True)
        try:
            row = await self._run_case(inputs, repeat)
        except Exception as exc:
            row = {"case": name, "repeat": repeat, "success": False,
                   "false_completion": False, "duplicate_actions": 0,
                   "prerequisite_unavailable": True,
                   "prerequisite_reason": f"Fixture setup or teardown raised {type(exc).__name__}."}
            self.rows.append(row)
        outcome = "unavailable" if row.get("prerequisite_unavailable") else "pass" if row.get("success") else "fail"
        print(f"[{name} #{repeat}] {outcome}; {row.get('latency_ms', 0)} ms; "
              f"false_completion={row.get('false_completion', False)}; "
              f"duplicates={row.get('duplicate_actions', 0)}", flush=True)
        return row

    async def _run_case(self, inputs: AxisQualityInput, repeat: int) -> dict:
        name = inputs.case_name
        started = time.monotonic()
        with fixture_server(name, self.used_origins) as (origin, state):
            bridge = ScopedBridge(self.client, origin, name)
            events = []
            result_data: dict = {}
            row = {"case": name, "repeat": repeat, "origin": origin,
                   "prerequisite_unavailable": False}
            try:
                bridge.rpc("tabs.create", {"url": inputs.initial_url.replace("https://fixture.test", origin), "active": True})
                config = fixture_config(self.config, origin, name)
                orchestrator = self.factory(config, bridge=bridge,
                    on_event=lambda event: events.append(_report_value(event.model_dump())))
                task = inputs.request.replace("https://fixture.test", origin)
                result = await orchestrator.run(task)
                result_data = result.model_dump()
                pages = bridge.read_pages()
                truth = state.snapshot()
                row.update(check_fixture(name, result_data, truth, pages, bridge.calls,
                                         stale_injected=bridge.stale_injected))
                row.update(result=result_data, fixture_state=truth, final_pages=pages)
                reason = result_data.get("reason", "")
                if (result_data.get("status") == "failed" and
                        ("provider" in reason.lower() or "BRIDGE_UNAVAILABLE" in reason)):
                    row.update(prerequisite_unavailable=True, success=False, false_completion=False,
                               prerequisite_reason=reason)
            except Exception as exc:
                # Preserve completed claims even when independent validation could not finish.
                completed = result_data.get("status") == "completed"
                row.update(success=False, false_completion=completed, duplicate_actions=0,
                           error=f"Workflow raised {type(exc).__name__}.")
                if isinstance(exc, (BrowserBridgeError, ConnectionError)):
                    row.update(prerequisite_unavailable=True, prerequisite_reason="Bridge became unavailable.")
            finally:
                bridge.close()
            row.update(latency_ms=int((time.monotonic() - started) * 1000),
                       events=events, bridge_calls=bridge.calls, cleanup_errors=bridge.cleanup_errors)
            self.rows.append(row)
            return row


def write_reports(rows: list[dict], output: Path, *, judge_report: Any = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    measured = [row for row in rows if not row.get("prerequisite_unavailable")]
    summary = {
        "total_runs": len(rows), "measured_runs": len(measured),
        "prerequisite_unavailable": len(rows) - len(measured),
        "successful_runs": sum(bool(row.get("success")) for row in measured),
        "false_completions": sum(bool(row.get("false_completion")) for row in measured),
        "duplicate_actions": sum(row.get("duplicate_actions", 0) for row in measured),
        "median_latency_ms": statistics.median([row.get("latency_ms", 0) for row in measured]) if measured else None,
    }
    summary["success_rate"] = summary["successful_runs"] / len(measured) if measured else None
    for metric in ("model_requests", "browser_actions", "input_tokens", "output_tokens"):
        summary[metric] = sum(row.get("result", {}).get(metric, 0) for row in measured)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "runs": rows}
    (output / "results.json").write_text(json.dumps(_report_value(payload), indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# AXIS local workflow evaluation", "", f"Measured runs: {len(measured)} / {len(rows)}. "
             f"Unavailable prerequisites: {summary['prerequisite_unavailable']}.", "",
             "| Case | Repeat | Outcome | False completion | Duplicate actions | ms | Requests | Input / output tokens |",
             "|---|---:|---|---|---:|---:|---:|---:|"]
    for row in rows:
        data = row.get("result", {})
        outcome = "unavailable" if row.get("prerequisite_unavailable") else "pass" if row.get("success") else "fail"
        lines.append(f"| {row['case']} | {row['repeat']} | {outcome} | {row.get('false_completion', False)} | "
                     f"{row.get('duplicate_actions', 0)} | {row.get('latency_ms', 0)} | {data.get('model_requests', 0)} | "
                     f"{data.get('input_tokens', 0)} / {data.get('output_tokens', 0)} |")
    lines += ["", "Unavailable runs are excluded from success and latency statistics. "
              "Task success requires both an agent completion and independent fixture checks. "
              "Detailed checks, prerequisites, and cleanup failures are recorded in results.json.", ""]
    (output / "results.md").write_text("\n".join(lines), encoding="utf-8")
    if judge_report is not None:
        # Pydantic Evals serializes assertions and judge failures alongside case outputs.
        (output / "pydantic-evals.json").write_text(
            json.dumps(_report_value(TypeAdapter(type(judge_report)).dump_python(judge_report, mode="json")), indent=2),
            encoding="utf-8")
    return summary


async def run_evaluations(args: argparse.Namespace) -> int:
    config = AxisConfig.load(args.config)
    dataset = quality_dataset(args.case)
    client = BrowserBridgeClient(timeout=10)
    issues = prerequisites(config, client)
    output = args.output or AGENT_DIR / "evals" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    if issues:
        rows = [{"case": case.name, "repeat": repeat, "prerequisite_unavailable": True,
                 "prerequisites": issues, "success": False, "false_completion": False, "duplicate_actions": 0}
                for case in dataset.cases for repeat in range(1, args.repeat + 1)]
        write_reports(rows, output)
        print(json.dumps({"prerequisites": issues, "output": str(output)}, indent=2))
        return 2
    if args.preflight:
        print("Bridge and provider setup are available. No browser task or model request was executed.")
        return 0
    if args.judge:
        dataset = quality_dataset(args.case, judge_model=build_model(config))
    client.timeout = max(10, config.run.tool_timeout_seconds or 120)
    runner = WorkflowRunner(config, client)
    report = await dataset.evaluate(runner, max_concurrency=1, repeat=args.repeat, progress=False)
    if report.failures:
        # Even unexpected harness failures must not disappear from the denominator.
        for failure in report.failures:
            runner.rows.append({"case": failure.source_case_name or failure.name, "repeat": 0,
                "success": False, "false_completion": False, "duplicate_actions": 0,
                "prerequisite_unavailable": True, "prerequisite_reason": "Dataset task execution failed."})
    summary = write_reports(runner.rows, output, judge_report=report if args.judge else None)
    print(json.dumps({"summary": summary, "output": str(output)}, indent=2))
    return 2 if summary["prerequisite_unavailable"] else 0 if all(row["success"] for row in runner.rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=[case.name for case in AXIS_QUALITY_DATASET.cases])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Report directory (default: evals/results/<timestamp>).")
    parser.add_argument("--judge", action="store_true", help="Add a judge using the configured provider/model.")
    parser.add_argument("--preflight", action="store_true", help="Check setup without executing model or browser tasks.")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return asyncio.run(run_evaluations(args))


if __name__ == "__main__":
    raise SystemExit(main())
