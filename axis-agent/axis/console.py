"""Shared terminal output for the CLI and opt-in UI service diagnostics."""
from __future__ import annotations

import sys
from typing import Any

from axis.models import AxisEvent, AxisResult


def configure_console() -> None:
    """Keep web text readable on Windows and flush redirected traces line by line."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


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
        if phase == "ui_run_started":
            print(f"\n{_RULE}\nAXIS task={d.get('task_id')}  operation={d.get('operation')}", flush=True)
            if d.get("instruction"):
                print(f"  instruction: {d['instruction']}")
            if d.get("extension"):
                print(f"  additional budget: {d['extension']}")
        elif phase == "planner_started":
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
        for key in ("plan_summary", "next_goal", "success_condition", "verification", "remaining_work", "final_answer"):
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
