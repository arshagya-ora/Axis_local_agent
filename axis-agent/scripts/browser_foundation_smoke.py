#!/usr/bin/env python3
"""Run the browser foundation flow directly through BrowserAgentTools."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from browser_agent_tools import BrowserAgentTools  # noqa: E402
from browser_bridge_client import BrowserBridgeClient  # noqa: E402


FIXTURE_HTML = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AXIS Browser Foundation</title></head>
<body><main><h1>AXIS Browser Foundation</h1>
<form id="smoke-form"><label for="smoke-input">Certification value</label>
<input id="smoke-input" name="value" autocomplete="off" autofocus>
<button type="submit">Submit</button>
<p id="smoke-result" role="status">Waiting for submission</p>
<p id="smoke-debug">No key received</p></form></main>
<script>
document.addEventListener('keydown', event => {
  document.getElementById('smoke-debug').textContent =
    'Key: ' + event.key + '; active: ' + (document.activeElement.id || document.activeElement.tagName);
  if (event.key === 'Enter' && document.activeElement.id === 'smoke-input') {
    event.preventDefault();
    const value = document.getElementById('smoke-input').value;
    setTimeout(() => { document.getElementById('smoke-result').textContent = 'Certified: ' + value; }, 25);
  }
});
</script></body></html>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(FIXTURE_HTML)))
        self.end_headers()
        self.wfile.write(FIXTURE_HTML)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


@contextmanager
def local_fixture() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def require_ok(label: str, result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise RuntimeError(f"{label} failed: {json.dumps(result, ensure_ascii=False)}")
    return result.get("data") or {}


def run_step(label: str, operation: Any) -> dict[str, Any]:
    try:
        return require_ok(label, operation())
    except Exception as error:
        raise RuntimeError(f"{label} raised: {error}") from error


def run_local_flow(tools: BrowserAgentTools, session_id: str, fixture_url: str, attempt: int) -> None:
    value = f"axis-local-{attempt:02d}"
    expected = f"Certified: {value}"
    run_step("local navigate", lambda: tools.browser_navigate({
        "browserSessionId": session_id, "operation": "open", "url": fixture_url,
    }))
    observed = run_step("local observe", lambda: tools.browser_observe({"browserSessionId": session_id}))
    if "Certification value" not in observed.get("snapshot", ""):
        raise RuntimeError("local observe did not expose the certification input")
    run_step("local fill", lambda: tools.browser_act({
        "browserSessionId": session_id, "action": "fill",
        "locator": {"selector": "#smoke-input"}, "value": value,
    }))
    run_step("local focused Enter", lambda: tools.browser_act({
        "browserSessionId": session_id, "action": "press", "key": "Enter",
    }))
    try:
        run_step("local wait", lambda: tools.browser_wait({
            "browserSessionId": session_id, "condition": "text", "expected": expected, "timeoutMs": 3000,
        }))
    except Exception as error:
        diagnostic = run_step("local failure observe", lambda: tools.browser_observe({
            "browserSessionId": session_id, "mode": "text",
        }))
        raise RuntimeError(f"{error}; page text: {diagnostic.get('snapshot', '')}") from error
    result_observation = run_step("local result observe", lambda: tools.browser_observe({
        "browserSessionId": session_id, "mode": "text",
    }))
    if expected not in result_observation.get("snapshot", ""):
        raise RuntimeError("local result observation did not contain the submitted value")
    assertion = run_step("local assert", lambda: tools.browser_assert({
        "browserSessionId": session_id, "assertion": "text",
        "locator": {"selector": "#smoke-result"}, "expected": expected,
    }))
    if assertion.get("passed") is not True:
        raise RuntimeError(f"local assertion did not pass: {assertion}")


def run_google_flow(tools: BrowserAgentTools, session_id: str, attempt: int) -> None:
    query = f"axis browser foundation certification {attempt:02d}"
    run_step("Google navigate", lambda: tools.browser_navigate({
        "browserSessionId": session_id, "operation": "open",
        "url": "https://www.google.com/webhp?hl=en&pws=0",
    }))
    observed = run_step("Google observe", lambda: tools.browser_observe({"browserSessionId": session_id}))
    if "Search" not in observed.get("snapshot", ""):
        raise RuntimeError("Google observe did not expose the search input")
    run_step("Google fill", lambda: tools.browser_act({
        "browserSessionId": session_id, "action": "fill",
        "locator": {"selector": "textarea[name='q'], input[name='q']"}, "value": query,
    }))
    run_step("Google focused Enter", lambda: tools.browser_act({
        "browserSessionId": session_id, "action": "press", "key": "Enter",
    }))
    run_step("Google wait", lambda: tools.browser_wait({
        "browserSessionId": session_id, "condition": "url", "expected": "/search", "timeoutMs": 20000,
    }))
    result_observation = run_step("Google result observe", lambda: tools.browser_observe({
        "browserSessionId": session_id, "mode": "text",
    }))
    if not result_observation.get("snapshot", "").strip():
        raise RuntimeError("Google result observation was empty")
    assertion = run_step("Google assert", lambda: tools.browser_assert({
        "browserSessionId": session_id, "assertion": "title",
        "expected": query, "contains": True, "timeoutMs": 20000,
    }))
    if assertion.get("passed") is not True:
        raise RuntimeError(f"Google assertion did not pass: {assertion}")


def certify(name: str, flow: Any, required_streak: int, max_attempts: int) -> None:
    streak = 0
    attempts = 0
    while streak < required_streak and attempts < max_attempts:
        attempts += 1
        try:
            flow(attempts)
        except Exception as error:  # one failed full execution resets certification
            streak = 0
            print(f"{name} attempt {attempts}: FAIL: {error}", flush=True)
        else:
            streak += 1
            print(f"{name} attempt {attempts}: PASS ({streak}/{required_streak} consecutive)", flush=True)
    if streak != required_streak:
        raise RuntimeError(f"{name} did not reach {required_streak} consecutive passes in {attempts} attempts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("local", "google", "all"), default="all")
    parser.add_argument("--required-streak", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=None)
    args = parser.parse_args()
    if args.required_streak < 1:
        parser.error("--required-streak must be at least 1")
    max_attempts = args.max_attempts or args.required_streak * 3

    client = BrowserBridgeClient(timeout=30)
    health = client.health()
    if not health.get("ok") or not health.get("hostReady") or not health.get("extensionReady"):
        raise RuntimeError(f"bridge is not ready: {health}")
    tools = BrowserAgentTools(client)
    print(f"bridge ready: extension {health.get('extensionVersion', 'unknown')}", flush=True)

    created = run_step("create dedicated smoke tab", lambda: tools.browser_tabs({
        "operation": "create", "url": "about:blank",
    }))
    session_id = created.get("browserSessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("dedicated smoke tab returned no browserSessionId")
    print(f"dedicated smoke tab: {session_id}", flush=True)
    try:
        if args.target in ("local", "all"):
            with local_fixture() as fixture_url:
                certify(
                    "local", lambda attempt: run_local_flow(tools, session_id, fixture_url, attempt),
                    args.required_streak, max_attempts,
                )
        if args.target in ("google", "all"):
            certify(
                "google", lambda attempt: run_google_flow(tools, session_id, attempt),
                args.required_streak, max_attempts,
            )
    finally:
        run_step("close dedicated smoke tab", lambda: tools.browser_tabs({
            "operation": "close", "browserSessionId": session_id,
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
