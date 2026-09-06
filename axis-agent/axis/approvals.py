"""Safe approval summaries and deferred-result handling for the CLI/run
boundary. Not a second approval protocol — this is the one place a human
decision (approve/deny) is translated into Pydantic AI's own
``DeferredToolResults``/``ToolApproved``/``ToolDenied`` types, and the one
place an effect's ``mark_approved``/``mark_denied`` transition happens for
an *approval-required* call (a denied call never reaches the ToolGuardrail
or the browser handler at all — see ``axis.agent``'s docstring — so this is
the only code path that can record that transition truthfully)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional
from uuid import uuid4

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from axis.effects import EffectLedger, RiskClass  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402

_MAX_SUMMARY_CHARS = 300

_REASON_BY_RISK: Dict[RiskClass, str] = {
    "external_effect": "external_effect risk requires approval by default policy.",
    "destructive_high_impact": "destructive_high_impact risk always requires explicit approval.",
}


class ApprovalRequest(BaseModel):
    """Bounded, safe-to-print approval summary. Never carries browser IDs,
    raw tool arguments, URLs, refs, selectors, credentials, or exception
    text — only what ``EffectRecord`` itself already exposes."""

    approval_id: str
    effect_id: str
    risk: RiskClass
    summary: str = Field(max_length=_MAX_SUMMARY_CHARS)
    reason: str = Field(max_length=_MAX_SUMMARY_CHARS)
    decision_nonce: Optional[str] = Field(default=None, max_length=64)
    decided: Optional[bool] = None
    resolved: bool = False


def build_approval_request(effect_id: str, risk: RiskClass, summary: str) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=uuid4().hex[:12],
        effect_id=effect_id,
        risk=risk,
        summary=summary[:_MAX_SUMMARY_CHARS],
        reason=_REASON_BY_RISK.get(risk, "This effect's risk requires approval by policy."),
    )


class ApprovalState(BaseModel):
    """Run-local (one AXIS task) correlation between a deferred tool call
    and the effect/approval it belongs to. Populated by the ToolGuardrail
    using the call's real ``tool_call_id`` — never a return tuple, never
    attached to the event logger."""

    effect_id_by_tool_call_id: Dict[str, str] = Field(default_factory=dict)
    requests_by_tool_call_id: Dict[str, ApprovalRequest] = Field(default_factory=dict)


def approval_required(policy: object, risk: RiskClass) -> bool:
    """Shared policy decision used by normal and durable execution."""
    return getattr(policy, risk) == "require"


def record_approval_request(
    state: ApprovalState, *, tool_call_id: str, effect_id: str, risk: RiskClass, summary: str,
    approval_id: Optional[str] = None,
) -> ApprovalRequest:
    request = build_approval_request(effect_id, risk, summary)
    if approval_id is not None:
        request = request.model_copy(update={"approval_id": approval_id})
    state.effect_id_by_tool_call_id[tool_call_id] = effect_id
    state.requests_by_tool_call_id[tool_call_id] = request
    return request


def derive_durable_approval_id(job_id: str, tool_call_id: str, effect_id: str) -> str:
    """Deterministic opaque correlation ID safe for Temporal replay."""
    raw = f"{job_id}:{tool_call_id}:{effect_id}:approval"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def decide_approval(
    state: ApprovalState, *, tool_call_id: str, request_id: str, decision_nonce: str, approved: bool,
) -> bool:
    """Validate and record exactly one decision for a pending call."""
    request = state.requests_by_tool_call_id.get(tool_call_id)
    if request is None or request.approval_id != request_id or request.resolved:
        return False
    if request.decision_nonce is not None:
        return False
    state.requests_by_tool_call_id[tool_call_id] = request.model_copy(
        update={"decision_nonce": decision_nonce, "decided": approved}
    )
    return True


def resolve_recorded_approval(state: ApprovalState, tool_call_id: str) -> Optional[ApprovalRequest]:
    request = state.requests_by_tool_call_id.get(tool_call_id)
    if request is None or request.decided is None or request.resolved:
        return None
    resolved = request.model_copy(update={"resolved": True})
    state.requests_by_tool_call_id[tool_call_id] = resolved
    return resolved


def render_approval_prompt(request: ApprovalRequest) -> str:
    return (
        f"Approval required ({request.risk}): {request.summary}\n"
        f"Reason: {request.reason}\n"
        "Approve this action? [y/N]: "
    )


async def prompt_for_approval(request: ApprovalRequest, input_fn: Callable[[str], str] = input) -> bool:
    """Empty or invalid input defaults to denial."""
    try:
        response = input_fn(render_approval_prompt(request))
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().lower() in ("y", "yes")


def resolve_approval(
    deferred: DeferredToolRequests, decisions: Dict[str, bool], approval_state: ApprovalState,
    ledger: EffectLedger, logger: RunEventLogger, run_id: str,
) -> DeferredToolResults:
    """Build the framework's ``DeferredToolResults`` from human decisions,
    recording the corresponding effect transition for each call. This is
    the single place an approval-required effect's ``denied`` status is
    ever set."""
    approvals: Dict[str, object] = {}
    for call in deferred.approvals:
        call_id = call.tool_call_id
        approved = decisions.get(call_id, False)
        effect_id = approval_state.effect_id_by_tool_call_id.get(call_id)
        if effect_id is not None:
            if approved:
                ledger.mark_approved(effect_id)
            else:
                ledger.mark_denied(effect_id)
            logger.effect_approval_resolved(run_id, approved)
            if not approved:
                logger.effect_denied(run_id)
        approvals[call_id] = ToolApproved() if approved else ToolDenied("Denied by the user.")
    return DeferredToolResults(approvals=approvals)
