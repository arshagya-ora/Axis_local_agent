"""Thin Pydantic AI adapter around the existing `BrowserAgentTools` layer.

Builds Pydantic AI `Tool` objects straight from `browser_agent_tools.py`'s
own `get_tool_definitions()` / `get_tool_handlers()` — no schema is
duplicated, retyped, or modified here, so the frozen contract at
`contracts/browser_tool_contract.json` cannot drift because of this file.

The one behavior this layer adds on top of the existing tools: every call's
`browserSessionId` is forced to the trusted value from `AxisRunDeps` before
the existing handler ever runs, so the model cannot substitute a different
session no matter what it puts in that argument. Everything else — schemas,
validation, scope checks, redaction, error envelopes — is exactly what
`browser_agent_tools.py` already does; this file never reimplements any of
it.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai import RunContext, Tool  # noqa: E402

from axis_events import RunEventLogger  # noqa: E402
from axis_models import AxisRunDeps  # noqa: E402
from browser_agent_tools import (  # noqa: E402
    BROWSER_AGENT_INSTRUCTIONS,
    get_tool_definitions,
    get_tool_handlers,
)

_SESSION_ID_FIELD = "browserSessionId"
_security_logger = logging.getLogger("axis.security")


class AxisBrowserCapability:
    """Builds the exactly-seven browser tools once per agent. Each tool
    call resolves its handler from `ctx.deps.browser_tools` at call time
    (not a value captured when the tools were built), so the same built
    `Agent` can safely run many isolated `AxisRunDeps` — one run's
    `BrowserAgentTools` instance, and therefore its observations, is never
    shared with another run's."""

    def __init__(self, event_logger: Optional[RunEventLogger] = None):
        self.event_logger = event_logger or RunEventLogger()
        # get_tool_definitions() already returns a deep copy of the frozen
        # seven-tool contract — nothing here mutates or re-derives it.
        self._definitions = get_tool_definitions()

    @property
    def tool_names(self) -> List[str]:
        return [entry["function"]["name"] for entry in self._definitions]

    @property
    def instructions(self) -> str:
        # Reused verbatim — browser-specific operating rules live in
        # browser_agent_tools.py and are not restated or altered here.
        return BROWSER_AGENT_INSTRUCTIONS

    def build_tools(self) -> List[Tool]:
        return [
            Tool.from_schema(
                function=self._make_tool_function(entry["function"]["name"]),
                name=entry["function"]["name"],
                description=entry["function"]["description"],
                json_schema=entry["function"]["parameters"],
                takes_ctx=True,
            )
            for entry in self._definitions
        ]

    def _make_tool_function(self, tool_name: str) -> Callable[..., Dict[str, Any]]:
        def call_handler(ctx: RunContext[AxisRunDeps], **kwargs: Any) -> Dict[str, Any]:
            trusted_session_id = ctx.deps.browser_session_id
            requested_session_id = kwargs.get(_SESSION_ID_FIELD)
            if requested_session_id is not None and requested_session_id != trusted_session_id:
                # Never let the model act on a session it wasn't bound to.
                # Overriding rather than erroring means a hallucinated or
                # mis-copied id doesn't turn a recoverable mistake into a
                # hard failure — the call still runs against the one real,
                # human-approved tab.
                _security_logger.warning(
                    "Ignoring a model-supplied browserSessionId (%r) for %s; using the trusted bound session.",
                    requested_session_id, tool_name,
                )
            kwargs[_SESSION_ID_FIELD] = trusted_session_id

            handler = get_tool_handlers(ctx.deps.browser_tools)[tool_name]
            self.event_logger.tool_started(ctx.deps.run_id, tool_name)
            start = time.perf_counter()
            try:
                result = handler(kwargs)
                success = isinstance(result, dict) and bool(result.get("ok"))
            except Exception as exc:  # noqa: BLE001 - a handler bug must never crash the agent run
                success = False
                result = {
                    "ok": False,
                    "browserSessionId": trusted_session_id,
                    "data": None,
                    "error": {
                        "code": "TOOL_EXECUTION_ERROR",
                        "message": f"{type(exc).__name__}: {exc}",
                        "retryable": True,
                        "diagnostic": None,
                    },
                }
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            self.event_logger.tool_completed(ctx.deps.run_id, tool_name, duration_ms, success)
            return result

        return call_handler
