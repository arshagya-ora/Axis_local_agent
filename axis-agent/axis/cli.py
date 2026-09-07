#!/usr/bin/env python3
"""Axis CLI / test harness (Phase 1.2: whole-browser, multi-tab).

A terminal chat loop wired to the one typed root agent
(`axis.agent.build_axis_agent`) and the eight frozen browser tools via the
browser Capability. Configuration comes from `axis.yaml` (see
`axis.config`) — not from hard-coded fallbacks or `/bind`-style
single-tab binding, which no longer exists: AXIS has whole-browser access
and the model itself calls `browser_tabs` to discover/create/activate/close
tabs during a run. `tests/_manual_workflows.py` and
`tests/fixtures/poc_app/` are untouched and still work directly against
`BrowserAgentTools` regardless of this CLI.

Usage:
    uv run python -m axis.cli
"""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai import DeferredToolRequests  # noqa: E402
from pydantic_ai.messages import ModelMessage  # noqa: E402

from axis.agent import (  # noqa: E402
    bind_axis_run_deps,
    build_axis_agent,
    build_model,
    next_run,
    no_managed_tab_result,
    run_axis_task,
)
from axis.approvals import prompt_for_approval, resolve_approval  # noqa: E402
from axis.config import AxisConfig, AxisConfigError, load_axis_config  # noqa: E402
from axis.events import RunEventLogger, build_console_event_printer  # noqa: E402
from axis.models import AxisRunDeps, AxisTaskResult  # noqa: E402
from browser_agent_tools import BrowserAgentTools, FirewallConfig  # noqa: E402
from browser_bridge_client import BrowserBridgeClient  # noqa: E402
from provider_config import ProviderConfigError, load_provider_config  # noqa: E402

_error_logger = logging.getLogger("axis.errors")

# Fields whose *values* must never reach the terminal even in verbose mode —
# mirrors browser_agent_tools._SENSITIVE_KEY_PATTERN's intent for anything
# printed here that didn't already go through that redaction (e.g. raw
# kwargs a future event might carry).
_SENSITIVE_ARG_NAMES = {"password", "token", "secret", "apikey", "api_key", "authorization", "cookie"}


def _redact_args_for_print(args: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(args, dict):
        return args
    return {k: ("<redacted>" if k.lower() in _SENSITIVE_ARG_NAMES else v) for k, v in args.items()}


def _make_printer(cli_config) -> Any:
    """A live event printer for axis.events.RunEventLogger — prints
    execution as it happens, not only after completion. Logical AXIS tab
    handles are safe to show even in verbose mode; nothing here ever prints
    a raw Chrome tabId/windowId/groupId (events never carry one). Delegates
    the actual formatting to the shared printer also used by the Temporal
    worker (`axis.durability.worker`), so CLI and durable-job output read
    the same way."""
    if cli_config.trace == "quiet":
        return None
    return build_console_event_printer(
        show_model_text=cli_config.show_model_text, show_tool_arguments=cli_config.show_tool_arguments,
        show_usage=cli_config.show_usage,
    )


def _print_result(result) -> None:
    print(f"\n[{result.status}]")
    for field_name, value in result.model_dump().items():
        if field_name == "status" or value in (None, [], ""):
            continue
        print(f"  {field_name}: {value}")


async def _run_axis_task_to_completion(
    agent: Any, run_deps: AxisRunDeps, user_input: str, message_history: Optional[List[ModelMessage]],
    axis_config: AxisConfig, event_logger: RunEventLogger,
) -> tuple[AxisTaskResult, List[ModelMessage]]:
    """Run one AXIS *task* to a terminal `AxisTaskResult`, transparently
    looping through every approval-required run segment: print a bounded,
    redacted approval summary (never raw browser arguments/identifiers),
    ask for an explicit approve/deny decision (empty/invalid input defaults
    to denial), and resume via the official `DeferredToolResults` mechanism
    — never by rebuilding or manually executing the deferred call."""
    result, message_history = await run_axis_task(
        agent, run_deps, user_input,
        message_history=message_history, limits=axis_config.limits, event_logger=event_logger,
        show_model_text=axis_config.cli.show_model_text, enable_live_model_events=axis_config.cli.trace == "verbose",
    )
    while isinstance(result, DeferredToolRequests):
        decisions: Dict[str, bool] = {}
        for call in result.approvals:
            request = run_deps.approval_state.requests_by_tool_call_id.get(call.tool_call_id)
            decisions[call.tool_call_id] = await prompt_for_approval(request) if request is not None else False
        deferred_results = resolve_approval(result, decisions, run_deps.approval_state, run_deps.effect_ledger, event_logger, run_deps.run_id)
        result, message_history = await run_axis_task(
            agent, run_deps, None,
            message_history=message_history, deferred_tool_results=deferred_results, limits=axis_config.limits,
            event_logger=event_logger, show_model_text=axis_config.cli.show_model_text,
            enable_live_model_events=axis_config.cli.trace == "verbose",
        )
    return result, message_history


def _print_job_state(state: Any) -> None:
    """Bounded, redacted durable-job summary — never raw browser ids/refs,
    prompts, or exception text (see axis.durability.models.AxisJobState,
    already built to this contract)."""
    print(f"\n[durable job {state.job_id}] status={state.status}")
    if state.plan:
        done = sum(1 for t in state.plan if t.status == "completed")
        print(f"  plan: {done}/{len(state.plan)} task(s) completed")
    if state.effects:
        print(f"  effects: {len(state.effects)} recorded (latest: {state.effects[-1].status})")
    if state.pending_approval is not None:
        print(f"  pending approval: risk={state.pending_approval.risk} — {state.pending_approval.summary}")
    if state.last_error_code:
        print(f"  last_error_code: {state.last_error_code}")


async def _handle_durable_command(
    user_input: str, axis_config: AxisConfig, durable: Dict[str, Any],
) -> bool:
    """Handles `/durable`, `/status`, `/pause`, `/resume`, `/cancel`, and
    `/reconnect`. `durable` is the chat loop's mutable durable-session
    state (`client`, `handle`, `job_id`) — a plain dict so this function
    can update it without a class. Returns True if `user_input` was a
    durable command (handled, whether it succeeded or not)."""
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from temporalio.client import Client

    from axis.durability import client as durability_client
    async def ensure_client() -> Any:
        if durable["client"] is None:
            durable["client"] = await Client.connect(
                axis_config.phase3.temporal.target,
                namespace=axis_config.phase3.temporal.namespace,
                # Same plugin the worker registers (axis/durability/worker.py) —
                # without it, values like AxisJobState round-trip through
                # Temporal's default (non-Pydantic-aware) converter, which
                # works but warns on every Pydantic v2 model it sees.
                plugins=[PydanticAIPlugin()],
            )
        return durable["client"]

    if user_input.startswith("/durable "):
        if durable["handle"] is not None:
            print("A durable job is already active. Use /cancel before starting another.")
            return True
        task_summary = user_input[len("/durable "):].strip()
        if not task_summary:
            print("Usage: /durable <task description>")
            return True
        try:
            client = await ensure_client()
            bridge_client = BrowserBridgeClient()
            handle = await durability_client.start_job(
                client, task_queue=axis_config.phase3.temporal.task_queue, task_summary=task_summary,
                bridge_host=bridge_client.host, bridge_port=bridge_client.port,
                allow_response_body=axis_config.browser.allow_response_body,
                max_open_tabs=axis_config.browser.max_open_tabs, firewall=axis_config.browser.firewall,
                workflow_execution_timeout_seconds=axis_config.phase3.temporal.workflow_execution_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never surface raw exception text to the terminal
            _error_logger.error("Could not start the durable job: %s.", type(exc).__name__)
            print("Could not start the durable job (worker unreachable or misconfigured). See logs.")
            return True
        durable["handle"] = handle
        durable["job_id"] = handle.id
        print(f"Durable job started: {handle.id}")
        return True

    if user_input == "/status":
        if durable["handle"] is None:
            print("No active durable job. Use /durable <task> or /reconnect <job_id>.")
            return True
        try:
            state = await durability_client.get_job_state(durable["handle"])
        except Exception as exc:  # noqa: BLE001
            _error_logger.error("Could not query the durable job: %s.", type(exc).__name__)
            print("Could not reach the durable job. See logs.")
            return True
        _print_job_state(state)
        return True

    if user_input == "/pause":
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        await durability_client.pause_job(durable["handle"])
        print("Pause requested.")
        return True

    if user_input == "/resume":
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        await durability_client.resume_job(durable["handle"])
        print("Resume requested.")
        return True

    if user_input in ("/approve", "/deny"):
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        accepted = await durability_client.decide_current_approval(
            durable["handle"], approved=user_input == "/approve",
        )
        print("Approval decision submitted." if accepted else "There is no pending approval to resolve.")
        return True

    if user_input.startswith("/input"):
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        answer = user_input[len("/input"):].strip()
        if not answer:
            print("Usage: /input <answer>")
            return True
        accepted = await durability_client.submit_user_input(durable["handle"], text=answer)
        print("Input submitted." if accepted else "There is no pending question to answer.")
        return True

    if user_input == "/rebind":
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        await durability_client.rebind_browser(durable["handle"])
        print("Browser rebind requested.")
        return True

    if user_input == "/result":
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        state = await durability_client.get_job_state(durable["handle"])
        if state.status not in ("completed", "failed", "cancelled"):
            print(f"Result is not ready; job status is {state.status}.")
            return True
        _print_result(await durability_client.get_result(durable["handle"]))
        return True

    if user_input.startswith("/cancel"):
        if durable["handle"] is None:
            print("No active durable job.")
            return True
        reason = user_input[len("/cancel"):].strip() or None
        await durability_client.cancel_job(durable["handle"], reason=reason)
        print("Cancellation requested.")
        return True

    if user_input.startswith("/reconnect"):
        job_id = user_input[len("/reconnect"):].strip() or durable["job_id"]
        if not job_id:
            print("Usage: /reconnect <job_id> (no prior job id to reuse).")
            return True
        try:
            client = await ensure_client()
            durable["handle"] = durability_client.get_job_handle(client, job_id)
            durable["job_id"] = job_id
            state = await durability_client.get_job_state(durable["handle"])
        except Exception as exc:  # noqa: BLE001
            _error_logger.error("Could not reconnect to job %s: %s.", job_id, type(exc).__name__)
            print("Could not reconnect to that job (not found, or worker/server unreachable). See logs.")
            durable["handle"] = None
            durable["job_id"] = None
            return True
        print(f"Reconnected to durable job {job_id}.")
        _print_job_state(state)
        return True

    return False


def _print_tabs(tabs: List[Dict[str, Any]]) -> None:
    if not tabs:
        print("(no tabs)")
        return
    for tab in tabs:
        marker = "*" if tab.get("active") else " "
        print(f" {marker} {tab['browserSessionId']}  {tab.get('title') or ''}  {tab.get('url') or ''}")


async def main() -> None:
    # Expected, safely-worded configuration errors get their own message
    # printed to the user; an unexpected failure while actually bootstrapping
    # the provider client (auth, transport, SDK construction) does not — only
    # its exception type is logged, since that failure could otherwise
    # surface things like auth/transport internals.
    try:
        provider_config = load_provider_config()
        axis_config = load_axis_config()
    except (ProviderConfigError, AxisConfigError) as exc:
        raise SystemExit(f"axis-agent CLI cannot start: {exc}") from exc

    openai_client = None  # only close it in `finally` if it actually gets built below
    try:
        model, openai_client = build_model(provider_config)
    except Exception as exc:  # noqa: BLE001 - provider bootstrap must never traceback
        _error_logger.error("Provider client bootstrap failed: %s.", type(exc).__name__)
        raise SystemExit("axis-agent CLI cannot start: the provider client could not be initialized.") from None

    try:
        await _run_chat_loop(model, axis_config)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nInterrupted.")
    finally:
        if openai_client is not None:
            try:
                await openai_client.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must never traceback either
                _error_logger.error("Error closing the provider client: %s.", type(exc).__name__)


async def _run_chat_loop(model: Any, axis_config: AxisConfig) -> None:
    from axis.durability import client as durability_client

    bridge_client = BrowserBridgeClient()
    browser_tools = BrowserAgentTools(
        bridge_client,
        allow_response_body=axis_config.browser.allow_response_body,
        firewall_config=axis_config.browser.firewall,
        max_open_tabs=axis_config.browser.max_open_tabs,
    )
    event_logger = RunEventLogger(printer=_make_printer(axis_config.cli))
    agent = build_axis_agent(model, event_logger=event_logger, max_retries=axis_config.max_retries)
    conversation_id = f"conv-{uuid.uuid4().hex[:12]}"

    deps: Optional[AxisRunDeps] = None
    message_history: Optional[List[ModelMessage]] = None

    def try_refresh(print_tabs: bool = True) -> None:
        nonlocal deps
        bound_deps, error = bind_axis_run_deps(browser_tools, conversation_id=conversation_id, event_logger=event_logger)
        if error is None:
            deps = bound_deps
            if print_tabs:
                print(f"{len(browser_tools.tab_registry)} tab(s) available.")
        else:
            deps = None
            print(f"Could not access the browser: {error.get('message', 'unknown error')}")

    durable: Dict[str, Any] = {"client": None, "handle": None, "job_id": None}

    print("Axis browsing agent — interactive chat (whole-browser, multi-tab).")
    print("Commands: /tabs (list), /refresh (reload tab registry), /reset (clear conversation), /exit")
    print("Durable jobs: /durable <task>, /status, /approve, /deny, /input <answer>, /pause, /resume, /rebind, /cancel [reason], /reconnect <job_id>, /result\n")
    try_refresh()

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/tabs":
            refreshed = browser_tools.refresh_tabs()
            _print_tabs(refreshed["data"]["tabs"] if refreshed["ok"] else [])
            continue
        if user_input == "/refresh":
            try_refresh()
            continue
        if user_input == "/reset":
            if durable["handle"] is not None:
                try:
                    state = await durability_client.get_job_state(durable["handle"])
                except Exception:
                    print("Could not confirm the durable job state; use /status or /reconnect.")
                    continue
                if state.status not in ("completed", "failed", "cancelled"):
                    print("A durable job is active. Use /cancel before /reset.")
                    continue
                durable["handle"] = None
                durable["job_id"] = None
            message_history = None
            print("Conversation history cleared. Browser tab registry retained.")
            continue
        if await _handle_durable_command(user_input, axis_config, durable):
            continue
        if user_input.startswith("/use "):
            # Optional convenience only — the agent itself never depends on
            # this; it can call browser_tabs(activate) during a run.
            handle = user_input[len("/use "):].strip()
            result = browser_tools.browser_tabs({"operation": "activate", "browserSessionId": handle})
            if result["ok"]:
                print(f"Activated {handle}.")
            else:
                print(f"Could not activate {handle}: {result['error']['message']}")
            continue

        if deps is None:
            _print_result(no_managed_tab_result({"message": "The browser has no tabs available."}))
            continue

        run_deps = next_run(deps, event_logger=event_logger)
        result, message_history = await _run_axis_task_to_completion(
            agent, run_deps, user_input, message_history, axis_config, event_logger,
        )
        _print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
