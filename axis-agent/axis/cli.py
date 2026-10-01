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

from axis.console import configure_console, print_event, print_debug_event, print_result

from browser_bridge_client import BrowserBridgeClient
from browser_tools import BrowserRuntime

from axis.agents import build_model, build_navigator, build_planner
from axis.models import AxisConfig, AxisEvent, AxisResult
from axis.orchestrator import AxisOrchestrator


from axis.bootstrap import build_runtime, build_orchestrator
from axis.ownership import RuntimeOwnership


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
    if getattr(args, "max_requests", None) is not None:
        config.run.max_model_requests = args.max_requests
    if getattr(args, "max_actions", None) is not None:
        config.run.max_browser_actions = args.max_actions
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
        if follow_up.lower().startswith("/extend"):
            try:
                amounts = [int(value) for value in follow_up.split()[1:]]
                if len(amounts) not in {1, 3}:
                    raise ValueError("Use /extend REQUESTS [STEPS ACTIONS].")
                result = await orchestrator.extend_budget(amounts[0],
                    steps=amounts[1] if len(amounts) == 3 else 0,
                    actions=amounts[2] if len(amounts) == 3 else 0)
                print_result(result)
            except ValueError as exc:
                print(str(exc))
            continue
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
    configure_console()
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
    parser.add_argument("--max-requests", type=int, default=None, help="Override the task's model request budget.")
    parser.add_argument("--max-actions", type=int, default=None, help="Override the task's browser action budget.")
    try:
        args = parser.parse_args(argv)
        with RuntimeOwnership():
            return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[cancelled] Interrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
