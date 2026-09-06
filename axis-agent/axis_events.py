"""Minimal structured run/tool events for the Axis Phase 1 agent.

Deliberately not a telemetry framework. This project has no OpenTelemetry
SDK configured anywhere (only the no-op `opentelemetry-api` package is
present, pulled in transitively by something else — there is no exporter or
provider wired up), so per the Phase 1 brief this uses structured logging
only rather than adding spans nothing collects. One event shape, one
logger, an optional in-memory sink for deterministic tests.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("axis.events")


@dataclass
class AxisEvent:
    run_id: str
    event_type: str
    timestamp: float = field(default_factory=time.time)
    tool_name: Optional[str] = None
    duration_ms: Optional[float] = None
    success: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        # Only ever these safe correlation fields — never a prompt, page
        # content, credential, or raw tab/window/group/frame/bridge-session
        # identifier.
        return {
            "runId": self.run_id,
            "eventType": self.event_type,
            "timestamp": self.timestamp,
            "toolName": self.tool_name,
            "durationMs": self.duration_ms,
            "success": self.success,
        }


class RunEventLogger:
    """Emits `AxisEvent` records as structured (JSON) log lines. Pass a
    `sink` list to also collect the raw event dicts in memory, which is
    what tests use instead of parsing log output."""

    def __init__(self, sink: Optional[List[Dict[str, Any]]] = None):
        self._sink = sink

    def _emit(self, event: AxisEvent) -> None:
        payload = event.to_dict()
        logger.info(json.dumps(payload))
        if self._sink is not None:
            self._sink.append(payload)

    def run_started(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_started"))

    def run_completed(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_completed", duration_ms=duration_ms, success=True))

    def run_failed(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_failed", duration_ms=duration_ms, success=False))

    def run_cancelled(self, run_id: str, duration_ms: Optional[float] = None) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="run_cancelled", duration_ms=duration_ms, success=False))

    def turn_started(self, run_id: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="turn_started"))

    def turn_completed(self, run_id: str, duration_ms: Optional[float] = None, success: bool = True) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="turn_completed", duration_ms=duration_ms, success=success))

    def tool_started(self, run_id: str, tool_name: str) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tool_started", tool_name=tool_name))

    def tool_completed(self, run_id: str, tool_name: str, duration_ms: Optional[float] = None, success: bool = True) -> None:
        self._emit(AxisEvent(run_id=run_id, event_type="tool_completed", tool_name=tool_name, duration_ms=duration_ms, success=success))
