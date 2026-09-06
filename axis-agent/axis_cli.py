#!/usr/bin/env python3
"""Axis Phase 1 CLI / test harness.

A terminal chat loop wired to the one typed root agent
(`axis_agent.build_axis_agent`) and the seven frozen browser tools. This
replaces `tests/pydandic_agents_test.py` as the supported entry point —
that file used an untyped, ungoverned agent wiring; keeping both would mean
two agent implementations to maintain. `tests/_manual_workflows.py` and
`tests/fixtures/poc_app/` are untouched and still work directly against
`BrowserAgentTools` regardless of this CLI.

Usage:
    uv run python axis_cli.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import List, Optional

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai.messages import ModelMessage  # noqa: E402

from axis_agent import bind_axis_run_deps, build_axis_agent, build_model, next_run, no_managed_tab_result, run_axis_task  # noqa: E402
from axis_events import RunEventLogger  # noqa: E402
from axis_models import AxisRunDeps, AxisRunLimits  # noqa: E402
from browser_agent_tools import BrowserAgentTools  # noqa: E402
from browser_bridge_client import BrowserBridgeClient  # noqa: E402
from provider_config import ProviderConfigError, load_provider_config  # noqa: E402


def _print_result(result) -> None:
    print(f"\n[{result.status}]")
    for field_name, value in result.model_dump().items():
        if field_name == "status":
            continue
        if value in (None, [], ""):
            continue
        print(f"  {field_name}: {value}")


async def main() -> None:
    try:
        provider_config = load_provider_config()
    except ProviderConfigError as exc:
        raise SystemExit(f"axis-agent CLI cannot start: {exc}") from exc

    model, openai_client = build_model(provider_config)
    bridge_client = BrowserBridgeClient()
    browser_tools = BrowserAgentTools(bridge_client)
    limits = AxisRunLimits.from_env()
    event_logger = RunEventLogger()
    agent = build_axis_agent(model, event_logger=event_logger)
    conversation_id = f"conv-{uuid.uuid4().hex[:12]}"

    deps: Optional[AxisRunDeps] = None
    message_history: Optional[List[ModelMessage]] = None

    def try_bind() -> None:
        nonlocal deps
        bound_deps, error = bind_axis_run_deps(browser_tools, conversation_id=conversation_id)
        if error is None:
            deps = bound_deps
            print(f"Bound to the focused Agent-managed tab. browserSessionId={deps.browser_session_id}")
        else:
            deps = None
            print(
                f"No browser tab bound: {error.get('message', 'unknown error')} "
                "(focus an Agent-managed Chrome tab and run /bind to retry)"
            )

    print("Axis Phase 1 browsing agent — interactive chat.")
    print("Commands: /bind (rebind the focused tab), /reset (clear conversation), /exit\n")
    try_bind()

    try:
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                break
            if user_input == "/bind":
                try_bind()
                continue
            if user_input == "/reset":
                message_history = None
                print("Conversation history cleared.")
                continue

            if deps is None:
                _print_result(no_managed_tab_result({"message": "No Agent-managed Chrome tab is bound."}))
                continue

            run_deps = next_run(deps)
            result, message_history = await run_axis_task(
                agent, run_deps, user_input,
                message_history=message_history, limits=limits, event_logger=event_logger,
            )
            _print_result(result)
    finally:
        await openai_client.close()


if __name__ == "__main__":
    asyncio.run(main())
