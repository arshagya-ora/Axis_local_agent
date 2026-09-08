"""AXIS: a minimal planner/navigator browser agent on Pydantic AI.

``browser_tools`` and ``browser_bridge_client`` are top-level modules in the
``axis-agent`` directory next to this package (same convention the existing
scripts and tests use), so make that directory importable before anything here
imports them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = str(Path(__file__).resolve().parent.parent)
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from .agents import (  # noqa: E402
    AxisDeps,
    StepGate,
    build_model,
    build_navigator,
    build_planner,
    navigator_tools,
)
from .models import (  # noqa: E402
    ActionRecord,
    AxisConfig,
    AxisEvent,
    AxisResult,
    BrowserState,
    NavigatorOutcome,
    PlanDecision,
    TabSummary,
    TaskMemory,
)
from .orchestrator import AxisOrchestrator  # noqa: E402

__all__ = [
    "ActionRecord", "AxisConfig", "AxisDeps", "AxisEvent", "AxisOrchestrator", "AxisResult",
    "BrowserState", "NavigatorOutcome", "PlanDecision", "StepGate", "TabSummary", "TaskMemory",
    "build_model", "build_navigator", "build_planner", "navigator_tools",
]
