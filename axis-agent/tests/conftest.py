"""Shared deterministic-test fixtures for the axis-agent test suite.

`pydantic_ai.models.ALLOW_MODEL_REQUESTS` is forced off at import time so an
accidentally-real model can never be reached from any test file that imports
this module (pytest always imports `conftest.py` first).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

AGENT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = AGENT_DIR / "scripts"
for path in (AGENT_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pydantic_ai.models as pai_models  # noqa: E402

pai_models.ALLOW_MODEL_REQUESTS = False

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo  # noqa: E402

import axis.agent as aa  # noqa: E402
import browser_agent_tools as bat  # noqa: E402
from browser_bridge_client import BrowserBridgeClient  # noqa: E402


def bind_tools(tab_id: int = 456, url: str = "https://application.example.com/profile", title: str = "Profile"):
    """Create a fresh BrowserAgentTools + trusted AxisRunDeps with one
    registered tab. Each call returns its own BrowserAgentTools instance."""
    client = MagicMock(spec=BrowserBridgeClient)
    client.rpc.side_effect = lambda method, params=None, **_: (
        {"tabs": [{"id": tab_id, "windowId": 1, "active": True, "url": url, "title": title}]} if method == "tabs.list"
        else (_ for _ in ()).throw(AssertionError(f"unexpected rpc call during bind: {method}"))
    )
    tools = bat.BrowserAgentTools(client)
    deps, error = aa.bind_axis_run_deps(tools)
    assert deps is not None, error
    return deps, client, tools


def last_tool_result(messages: List[ModelMessage], tool_name: str) -> Optional[Dict[str, Any]]:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                return part.content
    return None


def final_result_call(info: AgentInfo, **kwargs: Any) -> ModelResponse:
    name = info.output_tools[0].name
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=kwargs)])
