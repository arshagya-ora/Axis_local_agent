import asyncio
import os
import sys

import httpx
from openai import AsyncOpenAI
from oci_openai import OciUserPrincipalAuth

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

# axis-agent/ (the parent of this tests/ dir) holds the browser tool layer;
# add it to sys.path the same way tests/test_browser_agent_tools.py does.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_agent_tools import (  # noqa: E402
    BROWSER_AGENT_INSTRUCTIONS,
    BrowserAgentTools,
    get_tool_definitions,
    get_tool_handlers,
)
from browser_bridge_client import BrowserBridgeClient  # noqa: E402


oci_auth = OciUserPrincipalAuth(
    profile_name="DEFAULT",
)

http_client = httpx.AsyncClient(
    auth=oci_auth,
)

openai_client = AsyncOpenAI(
    base_url=(
        "https://inference.generativeai.us-ashburn-1."
        "oci.oraclecloud.com/openai/v1"
    ),
    api_key="unused",
    project=(
        "ocid1.generativeaiproject.oc1.iad."
        "amaaaaaah7afz4iawx4drs4qwgcvkbtlpxdd4i2n6ab2i3bszudd7titcp3a"
    ),
    http_client=http_client,
    max_retries=0,
)

provider = OpenAIProvider(
    openai_client=openai_client,
)

model = OpenAIResponsesModel(
    "openai.gpt-5.4-mini",
    provider=provider,
)


# =====================================================================
# Browser tool wiring
# =====================================================================
# browser_agent_tools.py already exposes the seven agent-facing tools as
# plain JSON-Schema dicts (get_tool_definitions()) plus a name -> callable
# dispatch table (get_tool_handlers()) — nothing in that module changes.
# Tool.from_schema() builds a pydantic-ai Tool straight from a JSON schema
# and a plain function, bypassing Python-signature introspection, which is
# exactly what's needed to hand these seven tools to the agent as-is.

browser_bridge_client = BrowserBridgeClient()
browser_tools = BrowserAgentTools(browser_bridge_client)


def _build_browser_agent_tools(tools: BrowserAgentTools) -> list[Tool]:
    handlers = get_tool_handlers(tools)
    built: list[Tool] = []
    for entry in get_tool_definitions():
        spec = entry["function"]
        tool_name = spec["name"]
        handler = handlers[tool_name]

        # Default-arg capture (handler=handler) so each closure binds its
        # own handler instead of all of them sharing the loop's last value.
        def call_handler(handler=handler, **kwargs):
            return handler(kwargs)

        built.append(
            Tool.from_schema(
                function=call_handler,
                name=tool_name,
                description=spec["description"],
                json_schema=spec["parameters"],
            )
        )
    return built


# =====================================================================
# Agent: a browsing agent equipped with the seven browser_* tools
# =====================================================================

agent = Agent(
    model=model,
    tools=_build_browser_agent_tools(browser_tools),
)

# bind_active_managed_tab() is Python-side setup, not an LLM tool (see
# browser_agent_tools.py's README) — the LLM can't discover a
# browserSessionId itself, so it's tracked here and injected into the
# agent's instructions every run.
_session_state: dict[str, str | None] = {"browserSessionId": None, "url": None}


def _try_bind_browser_tab() -> str:
    result = browser_tools.bind_active_managed_tab()
    if result["ok"]:
        _session_state["browserSessionId"] = result["data"]["browserSessionId"]
        _session_state["url"] = result["data"]["url"]
        return (
            f"Bound to the focused Agent-managed tab ({_session_state['url']}). "
            f"browserSessionId={_session_state['browserSessionId']}"
        )
    _session_state["browserSessionId"] = None
    _session_state["url"] = None
    error = result.get("error") or {}
    return (
        "No browser tab bound yet: "
        f"{error.get('message', 'unknown error')} "
        "(focus an Agent-managed Chrome tab and run /bind to retry)"
    )


PERSONA = (
    "You are Axis, an autonomous browsing agent. You help the user accomplish tasks in "
    "their web browser by driving one already-open, human-approved Chrome tab through the "
    "seven browser_* tools available to you. You are careful and methodical: you observe "
    "before you act, you confirm outcomes with browser_assert instead of assuming success, "
    "and you clearly tell the user what you saw and did after every step."
)


@agent.instructions
def browsing_instructions(ctx: RunContext) -> str:
    session_id = _session_state.get("browserSessionId")
    if session_id:
        binding_note = (
            f"Your bound browserSessionId for every browser_* tool call is exactly "
            f"{session_id!r}. Never use any other value and never invent one."
        )
    else:
        binding_note = (
            "No browser tab is bound yet, so every browser_* tool call will fail. Tell the "
            "user to focus an Agent-managed Chrome tab and ask them to run /bind, then wait "
            "for a successful binding before using any browser_* tool."
        )
    return f"{PERSONA}\n\n{BROWSER_AGENT_INSTRUCTIONS}\n\n{binding_note}"


# =====================================================================
# Terminal chatbot
# =====================================================================

async def main() -> None:
    print("Axis browsing agent — interactive chat.")
    print("Commands: /bind (rebind the focused tab), /reset (clear conversation), /exit\n")
    print(_try_bind_browser_tab())

    history = []
    try:
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except EOFError:
                break
            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                break
            if user_input == "/bind":
                print(_try_bind_browser_tab())
                continue
            if user_input == "/reset":
                history = []
                print("Conversation history cleared.")
                continue

            result = await agent.run(user_input, message_history=history)
            history = result.all_messages()
            print(f"\nAxis: {result.output}")
    finally:
        await openai_client.close()


if __name__ == "__main__":
    asyncio.run(main())
