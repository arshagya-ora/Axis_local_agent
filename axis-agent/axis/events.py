"""Minimal, safe structured run/tool/tab events for the Axis agent.

Deliberately not a telemetry framework: one event shape, one logger, an
optional in-memory sink for deterministic tests, and an optional live
`printer` callback the CLI uses to show execution as it happens (not only
after completion). Every field is either a stable correlation id, a short
enum-like string, a number, or an already-bounded/sanitized text value —
never a raw prompt, full tool arguments, page content, credential, or raw
tab/window/group/frame/bridge-session identifier by default. Logical AXIS
tab handles ("tab-xxxxxxxxxx") are safe to print (see
`browser_agent_tools.py` — they're already opaque) and are the only
tab-identifying value ever carried here.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("axis.events")


@dataclass
class AxisEvent:
    run_id: str
    event_type: str
    timestamp: float = field(default_factory=time.time)
    tool_name: Optional[str] = None
    duration_ms: Optional[float] = None
    success: Optional[bool] = None
    tab_handle: Optional[str] = None
    detail: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runId": self.run_id,
            "eventType": self.event_type,
            "timestamp": self.timestamp,
            "toolName": self.tool_name,
            "durationMs": self.duration_ms,
            "success": self.success,
            "tabHandle": self.tab_handle,
            "detail": self.detail,
            "data": self.data,
        }


class RunEventLogger:
    """Emits `AxisEvent` records as structured (JSON) log lines. Pass a
    `sink` list to also collect the raw event dicts in memory (what tests
    use instead of parsing log output), and/or a `printer` callback for
    live display (what `axis.cli` uses)."""

    def __init__(self, sink: Optional[List[Dict[str, Any]]] = None, printer: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._sink = sink
        self._printer = printer

    def _emit(self, event: AxisEvent) -> None:
        payload = event.to_dict()
        logger.info(json.dumps(payload))
        if self._sink is not None:
            self._sink.append(payload)
        if self._printer is not None:
            self._printer(payload)

    # -- run lifecycle --------------------------------------------------

    def run_started(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_started"))

    def run_completed(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_completed", duration_ms=duration_ms, success=True))

    def run_failed(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_failed", duration_ms=duration_ms, success=False))

    def run_cancelled(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_cancelled", duration_ms=duration_ms, success=False))

    # -- model turn -------------------------------------------------------

    def model_request_started(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="model_request_started"))

    def model_response_received(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="model_response_received", duration_ms=duration_ms))

    def model_text(self, run_id: str, text: str) -> None:
        """Text the model actually emitted (before/between tool calls) —
        never fabricated. See axis.agent's event_stream_handler wiring."""
        self._emit(AxisEvent(run_id=run_id, event_type="model_text", detail=text))

    def model_reasoning(self, run_id: str, summary: str) -> None:
        """An official provider-returned reasoning *summary* — only ever
        called with content the provider actually returned. Never a
        substitute for or a claim about hidden chain-of-thought."""
        self._emit(AxisEvent(run_id=run_id, event_type="model_reasoning", detail=summary))

    def retry(self, run_id: str, reason: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="retry", detail=reason))

    def usage(self, run_id: str, requests: int, tool_calls: int, total_tokens: int) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="usage", data={
            "requests": requests, "toolCalls": tool_calls, "totalTokens": total_tokens,
        }))

    def limit_reached(self, run_id: str, kind: str, used: Optional[int] = None, limit: Optional[int] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="limit_reached", data={"kind": kind, "used": used, "limit": limit}))

    # -- tools ------------------------------------------------------------

    def tool_started(self, run_id: str, tool_name: str, tab_handle: Optional[str] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tool_started", tool_name=tool_name, tab_handle=tab_handle))

    def tool_completed(
        self, run_id: str, tool_name: str, duration_ms: Optional[float] = None, success: bool = True,
        tab_handle: Optional[str] = None, error_code: Optional[str] = None,
    ) -> None:
        """`error_code` is the tool result's own `error.code` (e.g.
        `LEASE_NOT_HELD`, `VERSION_INCOMPATIBLE`, `BROWSER_REBIND_REQUIRED`,
        `STALE_OBSERVATION`) — safe to print (it's a stable enum-like
        string, never raw exception text or page content) and is what
        actually explains a near-instant `FAILED` that would otherwise look
        unexplained in the terminal."""
        self._emit(AxisEvent(
            run_id=run_id, event_type="tool_completed", tool_name=tool_name, duration_ms=duration_ms,
            success=success, tab_handle=tab_handle, detail=error_code,
        ))

    def assertion_result(self, run_id: str, passed: bool, tab_handle: Optional[str] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="assertion_result", success=passed, tab_handle=tab_handle))

    def evidence_saved(self, run_id: str, path: Optional[str]) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="evidence_saved", detail=path))

    # -- tabs and observations --------------------------------------------

    def tab_created(self, run_id: str, tab_handle: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tab_created", tab_handle=tab_handle))

    def tab_activated(self, run_id: str, tab_handle: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tab_activated", tab_handle=tab_handle))

    def tab_closed(self, run_id: str, tab_handle: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tab_closed", tab_handle=tab_handle))

    def observation_created(self, run_id: str, tab_handle: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="observation_created", tab_handle=tab_handle))

    def observation_invalidated(self, run_id: str, tab_handle: str, reason: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="observation_invalidated", tab_handle=tab_handle, detail=reason))

    # -- Phase 2: task intent -------------------------------------------

    def intent_created(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="intent_created"))

    def intent_revised(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="intent_revised"))

    # -- Phase 2: plan ----------------------------------------------------

    def plan_created(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="plan_created"))

    def plan_updated(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="plan_updated"))

    def plan_step_started(self, run_id: str, task_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="plan_step_started", data={"taskId": task_id}))

    def plan_step_completed(self, run_id: str, task_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="plan_step_completed", data={"taskId": task_id}))

    def plan_step_blocked(self, run_id: str, task_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="plan_step_blocked", data={"taskId": task_id}))

    # -- Phase 2: effects and approvals ------------------------------------

    def effect_prepared(self, run_id: str, risk: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_prepared", data={"risk": risk}))

    def effect_approval_requested(self, run_id: str, risk: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_approval_requested", data={"risk": risk}))

    def effect_approval_resolved(self, run_id: str, approved: bool) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_approval_resolved", success=approved))

    def effect_started(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_started"))

    def effect_executed_unverified(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_executed_unverified"))

    def effect_succeeded(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_succeeded", success=True))

    def effect_failed(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_failed", success=False))

    def effect_denied(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_denied", success=False))

    def effect_cancelled(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_cancelled"))

    def effect_superseded(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="effect_superseded"))

    def acceptance_passed(self, run_id: str, criterion_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="acceptance_passed", data={"criterionId": criterion_id}))

    def acceptance_failed(self, run_id: str, criterion_id: Optional[str] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="acceptance_failed", data={"criterionId": criterion_id}))


def build_console_event_printer(
    *, show_model_text: bool = True, show_tool_arguments: bool = True, show_usage: bool = True,
    stream: Optional[Any] = None,
) -> Callable[[Dict[str, Any]], None]:
    """A live, human-readable event printer — shared by `axis.cli` (verbose
    trace mode) and the Temporal worker (`axis.durability.worker`), so both
    the interactive CLI and a durable job print execution as it happens in
    the same format. Any event type without a dedicated branch below still
    prints (type + whatever safe detail/data it carries) — nothing is ever
    silently dropped, which matters most for durability-specific events
    (`job_started`, `browser_lease_acquired`, `job_waiting_for_approval`,
    `reconciliation_*`, `job_failed` with its `error_code` in `detail`,
    etc.) that `axis.cli`'s original printer never had branches for because
    they can only happen in a durable run."""

    def printer(event: Dict[str, Any]) -> None:
        kind = event["eventType"]
        prefix = f"[{kind}]"
        if kind == "tool_started":
            print(f"{prefix} {event.get('toolName')} (tab={event.get('tabHandle')})", file=stream)
        elif kind == "tool_completed":
            status = "ok" if event.get("success") else "FAILED"
            error_code = event.get("detail")
            suffix = f" [{error_code}]" if error_code else ""
            print(f"{prefix} {event.get('toolName')} {status}{suffix} ({event.get('durationMs')}ms)", file=stream)
        elif kind == "model_text" and show_model_text:
            print(f"{prefix} {event.get('detail')}", file=stream)
        elif kind == "model_reasoning" and show_model_text:
            print(f"[reasoning] {event.get('detail')}", file=stream)
        elif kind == "usage" and show_usage:
            data = event.get("data") or {}
            print(f"{prefix} requests={data.get('requests')} toolCalls={data.get('toolCalls')} totalTokens={data.get('totalTokens')}", file=stream)
        elif kind == "limit_reached":
            data = event.get("data") or {}
            print(f"{prefix} {data.get('kind')} used={data.get('used')} limit={data.get('limit')}", file=stream)
        elif kind in ("tab_created", "tab_activated", "tab_closed"):
            print(f"{prefix} {event.get('tabHandle')}", file=stream)
        elif kind in ("observation_created", "observation_invalidated"):
            print(f"{prefix} tab={event.get('tabHandle')} {event.get('detail') or ''}".rstrip(), file=stream)
        elif kind == "assertion_result":
            print(f"{prefix} passed={event.get('success')} tab={event.get('tabHandle')}", file=stream)
        elif kind == "evidence_saved":
            print(f"{prefix} {event.get('detail')}", file=stream)
        elif kind == "retry":
            print(f"{prefix} {event.get('detail')}", file=stream)
        elif kind in ("run_started", "run_completed", "run_failed", "run_cancelled", "model_request_started", "model_response_received"):
            print(prefix, file=stream)
        elif kind == "tool_started" and not show_tool_arguments:
            pass
        else:
            # Generic fallback for every durable-only / future event type —
            # run_id first so concurrent jobs in one worker log are still
            # distinguishable at a glance.
            extras = " ".join(
                f"{name}={value}" for name, value in (
                    ("detail", event.get("detail")), ("data", event.get("data")),
                    ("success", event.get("success")), ("tab", event.get("tabHandle")),
                ) if value is not None
            )
            print(f"{prefix} run={event.get('runId')} {extras}".rstrip(), file=stream)

    return printer
