"""Small CLI-facing methods for starting, signalling, querying, and
reconnecting to a durable AXIS job. Never runs a browser mutation directly
and never rebuilds a deferred call itself — every state change goes through
`AxisJobWorkflow`'s own signals."""
from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

AGENT_DIR = Path(__file__).parent.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from temporalio.client import Client, WorkflowHandle  # noqa: E402

from axis.durability.leases import browser_scope_key  # noqa: E402
from axis.durability.models import (  # noqa: E402
    AxisJobInput,
    AxisJobState,
    CancelSignal,
    PauseSignal,
    RebindBrowserSignal,
    RebindCandidate,
    ResumeSignal,
    SelectRebindCandidateSignal,
    SubmitApprovalSignal,
    SubmitUserInputSignal,
)
from axis.durability.workflow import AxisJobWorkflow  # noqa: E402
from browser_agent_tools import FirewallConfig  # noqa: E402


async def start_job(
    client: Client, *, task_queue: str, task_summary: str, bridge_host: str, bridge_port: int,
    allow_response_body: bool = False, max_open_tabs: int = 30, firewall: Optional[FirewallConfig] = None,
    job_id: Optional[str] = None, workflow_execution_timeout_seconds: Optional[float] = None,
) -> WorkflowHandle:
    job_id = job_id or f"axis-job-{uuid.uuid4().hex[:12]}"
    scope_key = browser_scope_key(bridge_host=bridge_host, bridge_port=bridge_port)
    job_input = AxisJobInput(
        job_id=job_id, task_summary=task_summary, browser_scope_key=scope_key,
        allow_response_body=allow_response_body, max_open_tabs=max_open_tabs,
        firewall=firewall or FirewallConfig(),
    )
    kwargs: dict[str, Any] = {"id": job_id, "task_queue": task_queue}
    if workflow_execution_timeout_seconds is not None:
        kwargs["execution_timeout"] = timedelta(seconds=workflow_execution_timeout_seconds)
    return await client.start_workflow(AxisJobWorkflow.run, job_input, **kwargs)


def get_job_handle(client: Client, job_id: str) -> WorkflowHandle:
    """Reconnect to an existing job after a CLI restart — never starts a
    new workflow."""
    return client.get_workflow_handle_for(AxisJobWorkflow.run, workflow_id=job_id)


async def submit_approval(handle: WorkflowHandle, *, request_id: str, approved: bool, decision_nonce: str) -> None:
    await handle.signal(AxisJobWorkflow.submit_approval, SubmitApprovalSignal(request_id=request_id, approved=approved, decision_nonce=decision_nonce))


async def decide_current_approval(handle: WorkflowHandle, *, approved: bool) -> bool:
    state = await get_job_state(handle)
    request = state.pending_approval
    if request is None or request.resolved:
        return False
    await submit_approval(
        handle, request_id=request.approval_id, approved=approved,
        decision_nonce=uuid.uuid4().hex,
    )
    return True


async def pause_job(handle: WorkflowHandle) -> None:
    await handle.signal(AxisJobWorkflow.pause_job, PauseSignal())


async def resume_job(handle: WorkflowHandle) -> None:
    await handle.signal(AxisJobWorkflow.resume_job, ResumeSignal())


async def cancel_job(handle: WorkflowHandle, *, reason: Optional[str] = None) -> None:
    await handle.signal(AxisJobWorkflow.cancel_job, CancelSignal(reason=reason))


async def submit_user_input(
    handle: WorkflowHandle, *, text: str, request_id: Optional[str] = None,
    answer_nonce: Optional[str] = None,
) -> bool:
    if request_id is None:
        state = await get_job_state(handle)
        if state.pending_question is None or state.pending_question.answered:
            return False
        request_id = state.pending_question.request_id
    await handle.signal(AxisJobWorkflow.submit_user_input, SubmitUserInputSignal(
        request_id=request_id, answer_nonce=answer_nonce or uuid.uuid4().hex, text=text,
    ))
    return True


async def rebind_browser(handle: WorkflowHandle, *, reason: Optional[str] = None) -> None:
    await handle.signal(AxisJobWorkflow.rebind_browser, RebindBrowserSignal(reason=reason))


async def get_rebind_candidates(handle: WorkflowHandle) -> list[RebindCandidate]:
    """Bounded, opaque candidate tabs to disambiguate a rebind with — never
    raw Chrome/bridge identifiers. Empty unless an ambiguous rebind is
    currently pending selection."""
    return await handle.query(AxisJobWorkflow.get_rebind_candidates)


async def select_rebind_candidate(handle: WorkflowHandle, *, candidate_id: str) -> None:
    await handle.signal(AxisJobWorkflow.select_rebind_candidate, SelectRebindCandidateSignal(candidate_id=candidate_id))


async def get_job_state(handle: WorkflowHandle) -> AxisJobState:
    return await handle.query(AxisJobWorkflow.get_job_state)


async def get_result(handle: WorkflowHandle) -> Any:
    return await handle.result()
