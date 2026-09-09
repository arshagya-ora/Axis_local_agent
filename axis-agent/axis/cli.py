"""Terminal entry point: bootstrap the provider, browser, and both agents.

    python -m axis.cli "what is the capital of France?"
    python -m axis.cli --interactive "search google for pydantic ai"
    python -m axis.cli --debug              # full-trace interactive chat
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python axis/cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Web content (page titles, model answers) routinely contains characters a
# default console codepage can't encode (e.g. Windows cp1252) — without this,
# printing them raises UnicodeEncodeError, which AxisOrchestrator._emit()
# swallows silently for event callbacks, so output just vanishes or garbles
# (e.g. curly quotes rendering as `<63>`). UTF-8 with replacement keeps the
# CLI legible regardless of the host console's default encoding.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from browser_bridge_client import BrowserBridgeClient
from browser_tools import BrowserRuntime

from axis.agents import build_model, build_navigator, build_planner
from axis.models import AxisConfig, AxisEvent, AxisResult
from axis.orchestrator import AxisOrchestrator


def build_runtime(config: AxisConfig, client: Any | None = None) -> BrowserRuntime:
    """One :class:`BrowserRuntime` configured from ``axis.yaml``."""
    browser = config.browser
    firewall = browser.firewall
    return BrowserRuntime(
        client or BrowserBridgeClient(),
        max_tabs=browser.max_open_tabs,
        allow_response_body=browser.allow_response_body,
        upload_roots=tuple(browser.upload_roots),
        artifact_directory=browser.artifact_directory,
        firewall_default=firewall.default,
        allow_urls=tuple(firewall.allow_urls),
        deny_urls=tuple(firewall.deny_urls),
        deny_schemes=tuple(firewall.deny_schemes),
        allow_methods=tuple(firewall.allow_methods),
        deny_methods=tuple(firewall.deny_methods),
        allow_coordinate_fallback=browser.allow_coordinate_fallback,
    )


def build_orchestrator(
    config: AxisConfig,
    *,
    bridge: Any | None = None,
    openai_client: Any | None = None,
    model_name: str | None = None,
    on_event: Any | None = None,
    approval: Any | None = None,
) -> AxisOrchestrator:
    """Wire config -> one shared model -> planner + navigator -> orchestrator."""
    model = build_model(config, client=openai_client, model_name=model_name)

    def navigator_factory(*, capture: bool, diagnose: bool, downloads: bool, visual: bool = False):
        """Rebuild the navigator when an opt-in tool becomes necessary. Cheap:
        the model instance is shared, only the tool list changes."""
        variant = config.model_copy(deep=True)
        variant.tools.capture_evidence = capture
        variant.tools.diagnose = diagnose
        variant.tools.downloads = downloads
        variant.tools.visual = visual
        return build_navigator(variant, model)

    return AxisOrchestrator(
        browser=build_runtime(config, bridge),
        planner=build_planner(config, model),
        navigator=build_navigator(config, model),
        config=config,
        on_event=on_event,
        approval=approval,
        navigator_factory=navigator_factory,
    )


# ---------------------------------------------------------------------------
# Plain event printing (--verbose)
# ---------------------------------------------------------------------------

def print_event(event: AxisEvent) -> None:
    detail = ", ".join(f"{key}={value!r}" for key, value in event.detail.items() if value not in (None, ""))
    print(f"  · {event.kind}: {detail}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Full-trace debug printing (--debug): every planner/navigator output in
# full, every browser tool call — including ones the runtime refused to
# execute — with its arguments and pass/fail status, so a failure or a
# success can be traced back to its exact step.
# ---------------------------------------------------------------------------

_RULE = "-" * 72  # plain ASCII: box-drawing/emoji chars silently vanish on a
# non-UTF-8 console (e.g. Windows cp1252) since AxisOrchestrator._emit()
# swallows callback exceptions rather than letting a print failure end a run.


def _kv(detail: dict[str, Any], *keys: str) -> str:
    return "  ".join(f"{k}={detail[k]!r}" for k in keys if detail.get(k) not in (None, "", []))


def _stamp(detail: dict[str, Any]) -> str:
    """Wall-clock offset since the run started, as [mm:ss.mmm]."""
    ms = int(detail.get("at_ms") or 0)
    return f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}]"


def _took(detail: dict[str, Any]) -> str:
    ms = detail.get("duration_ms")
    return f"  ({ms / 1000:.1f}s)" if isinstance(ms, int) and ms else ""


def print_debug_event(event: AxisEvent) -> None:
    d = event.detail
    at = _stamp(d)
    if event.kind == "status":
        phase = d.get("phase", "")
        if phase == "planner_started":
            print(f"\n{_RULE}\n{at} >> PLANNER  (steps_since_plan={d.get('steps_since_plan')})", flush=True)
        elif phase == "navigator_step_started":
            print(f"\n{_RULE}\n{at} >> NAVIGATOR STEP  goal={d.get('goal')!r}", flush=True)
        elif phase == "tools_changed":
            print(f"{at}   [TOOLS] capture={d.get('capture')} diagnose={d.get('diagnose')}")
        elif phase == "observed":
            print(f"{at}   [OBSV] tab={d.get('tab')}{_took(d)}")
        elif phase == "vision_unavailable":
            print(f"{at}   [VISION] {d.get('reason')}")
        elif phase in ("tab_list", "tab_create", "tab_selected"):
            icon = "OK  " if d.get("success", True) else "FAIL"
            print(f"{at}   [{icon}] {phase}  {_kv(d, 'tabs', 'tab', 'url')}")
            if d.get("error"):
                print(f"          error: {d['error']}")
        return

    if event.kind == "planner_decision":
        print(f"  decision={d.get('decision')}{_took(d)}  {_kv(d, 'reason')}")
        for key in ("plan_summary", "next_goal", "success_condition", "final_answer"):
            if d.get(key):
                print(f"  {key}: {d[key]}")
        for item in d.get("evidence") or []:
            print(f"  evidence: {item}")
        return

    if event.kind == "navigator_step":
        browser_ms = d.get("browser_ms") or 0
        print(
            f"  status={d.get('status')}  actions_used={d.get('actions')}{_took(d)}"
            f"{f' [browser {browser_ms / 1000:.1f}s]' if browser_ms else ''}  {_kv(d, 'interrupted')}"
        )
        if d.get("summary"):
            print(f"  summary: {d['summary']}")
        if d.get("reason"):
            print(f"  reason: {d['reason']}")
        for item in d.get("evidence") or []:
            print(f"  evidence: {item}")
        return

    if event.kind == "browser_action":
        icon = "SKIP" if d.get("skipped") else ("OK  " if d.get("success") else "FAIL")
        call = f"{d.get('tool')}.{d.get('operation')}" if d.get("operation") else str(d.get("tool"))
        line = f"{at}   [{icon}] {call}{_took(d)}"
        if d.get("args"):
            line += f"  ({d['args']})"
        print(line)
        if d.get("error"):
            print(f"          error: {d['error']}")
        if d.get("refs_invalidated"):
            print("          refs invalidated -> fresh observation next")
        return

    if event.kind == "final":
        print(f"\n{_RULE}\n{at} == FINAL  status={d.get('status')}  {_kv(d, 'reason')}\n{_RULE}", flush=True)
        return


def print_result(result: AxisResult) -> None:
    print(f"\n[{result.status}] {result.reason}")
    if result.answer:
        print(f"\n{result.answer}")
    for item in result.evidence:
        print(f"  - {item}")
    for limitation in result.limitations:
        print(f"Limitation: {limitation}")
    other_ms = max(0, result.duration_ms - result.model_ms - result.browser_ms)
    print(
        f"\nsteps={result.total_steps} planner_passes={result.planner_passes} "
        f"browser_actions={result.browser_actions} model_requests={result.model_requests}"
    )
    print(f"tokens: input={result.input_tokens} output={result.output_tokens}")
    print(
        f"time: total={result.duration_ms / 1000:.1f}s "
        f"model={result.model_ms / 1000:.1f}s browser={result.browser_ms / 1000:.1f}s "
        f"other={other_ms / 1000:.1f}s"
    )


def confirm(tool: str, operation: str | None, detail: dict[str, Any]) -> bool:
    target = f" target={detail.get('target')!r}" if detail.get("target") else ""
    origin = f" at {detail.get('origin')}" if detail.get("origin") else ""
    reason = f" ({detail.get('reason')})" if detail.get("reason") else ""
    answer = input(
        f"Approve {tool}.{operation}{origin}{target}{reason}? [y/N] "
    ).strip().lower()
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    config = AxisConfig.load(args.config)
    if args.max_steps is not None:
        config.run.max_total_steps = args.max_steps
    if args.capture:
        config.tools.capture_evidence = True
    if args.diagnose:
        config.tools.diagnose = True
    if getattr(args, "visual_mode", None) is not None:
        config.run.visual_mode = args.visual_mode

    on_event = print_debug_event if args.debug else (print_event if args.verbose else None)
    orchestrator = build_orchestrator(config, on_event=on_event, approval=confirm if args.approve else None)
    interactive = args.interactive or args.debug

    task = args.task
    if task is None:
        try:
            task = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not task:
            return 0

    result = await orchestrator.run(task)
    print_result(result)

    while interactive:
        try:
            follow_up = input("\nYou (blank to quit) > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not follow_up:
            break
        explicit_continue = follow_up.lower().startswith("/continue ")
        explicit_new = follow_up.lower().startswith("/new ")
        if explicit_continue or explicit_new:
            follow_up = follow_up.split(maxsplit=1)[1].strip()
        if explicit_continue or (not explicit_new and result.status in {"needs_user", "paused"}):
            result = await orchestrator.continue_task(follow_up)
        else:
            result = await orchestrator.start_task(follow_up)
        print_result(result)
    return 0 if result.status in {"completed", "needs_user"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", default=None, help="The task or question for AXIS (omit to be prompted).")
    parser.add_argument("--config", default=None, help="Path to axis.yaml (default: axis-agent/axis.yaml).")
    parser.add_argument("--interactive", action="store_true", help="Accept follow-ups after the first result.")
    parser.add_argument("--verbose", action="store_true", help="Print bounded orchestrator events.")
    parser.add_argument(
        "--debug", action="store_true",
        help="Full-trace interactive chat: every planner/navigator output, every browser tool "
             "call (including ones the runtime refused), its arguments, and pass/fail status. "
             "Implies --interactive.",
    )
    parser.add_argument("--approve", action="store_true", help="Ask before consequential actions.")
    parser.add_argument("--capture", action="store_true", help="Add browser_capture_evidence for this run.")
    parser.add_argument("--diagnose", action="store_true", help="Add browser_diagnose for this run.")
    parser.add_argument("--visual-mode", choices=("auto", "off"), help="Automatic visual recovery, or text-only browsing.")
    parser.add_argument("--max-steps", type=int, default=None, help="Override run.max_total_steps.")
    try:
        return asyncio.run(run(parser.parse_args(argv)))
    except KeyboardInterrupt:
        print("\n[cancelled] Interrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
