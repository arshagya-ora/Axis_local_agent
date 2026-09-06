"""Safe, bounded desktop presentation projections.

Nothing here is a second AXIS business-output model or a second Temporal
model — every type in this module exists only to bound and redact
`axis.durability.models.AxisJobState` (and the models it embeds) into what
is safe to hand to QML. Internal-only identifiers an accepted signal
genuinely needs (`approval_id`, `effect_id`, candidate ids, nonces) are
deliberately NEVER copied onto these projections — the controller keeps
those privately and this module never sees or returns them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from axis.durability.models import AxisJobState

_MAX_TEXT = 500
_MAX_SHORT_TEXT = 160


def _bound(value: Optional[str], limit: int = _MAX_TEXT) -> str:
    if not value:
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class DesktopPlanItem(BaseModel):
    content: str = Field(max_length=_MAX_TEXT)
    status: str
    active: bool = False


class DesktopEffectItem(BaseModel):
    summary: str = Field(max_length=_MAX_TEXT)
    risk: str
    status: str
    mutation_count: int = 0
    max_mutations: int = 0


class DesktopAcceptanceItem(BaseModel):
    description: str = Field(max_length=_MAX_TEXT)
    state: str  # "pending" | "passed" | "failed"


class DesktopApproval(BaseModel):
    """No `approval_id`/`effect_id`/nonce — those stay private in the
    controller, which is the only thing that ever needs them to send the
    accepted signal."""

    summary: str = Field(max_length=_MAX_TEXT)
    risk: str
    reason: str = Field(max_length=_MAX_TEXT)


class DesktopQuestion(BaseModel):
    """No `request_id`/answer nonce — same reasoning as `DesktopApproval`."""

    question: str = Field(max_length=_MAX_TEXT)
    choices: List[str] = Field(default_factory=list, max_length=20)


class DesktopRebindCandidate(BaseModel):
    """No `candidate_id` — selection happens by bounded list index; the
    controller privately maps index -> the real opaque candidate id."""

    label: str = Field(max_length=_MAX_SHORT_TEXT)
    sanitized_url: str = Field(default="", max_length=_MAX_TEXT)
    title: str = Field(default="", max_length=_MAX_SHORT_TEXT)
    active: bool = False


class DesktopResult(BaseModel):
    status: str
    summary: str = Field(max_length=_MAX_TEXT)
    verification_summary: str = Field(default="", max_length=_MAX_TEXT)
    error_code: Optional[str] = None
    retryable: bool = False


class DesktopJobSnapshot(BaseModel):
    display_status: str
    status_code: str
    task_summary: str = Field(max_length=_MAX_TEXT)
    plan_items: List[DesktopPlanItem] = Field(default_factory=list)
    effects: List[DesktopEffectItem] = Field(default_factory=list)
    acceptance: List[DesktopAcceptanceItem] = Field(default_factory=list)
    pending_approval: Optional[DesktopApproval] = None
    pending_question: Optional[DesktopQuestion] = None
    rebind_required: bool = False
    rebind_candidates: List[DesktopRebindCandidate] = Field(default_factory=list)
    result: Optional[DesktopResult] = None
    error_code: Optional[str] = None
    can_pause: bool = False
    can_resume: bool = False
    can_cancel: bool = False
    is_terminal: bool = False


# status_code -> (display label, visual "kind" for QML styling, primary action hint)
STATUS_PRESENTATION: Dict[str, Tuple[str, str, str]] = {
    "created": ("Preparing", "neutral", "wait"),
    "running": ("Running", "accent", "pause_or_cancel"),
    "waiting_for_approval": ("Approval required", "attention", "review"),
    "waiting_for_user": ("Input required", "attention", "answer"),
    "paused": ("Paused", "muted", "resume"),
    "reconciling": ("Reconciling", "progress", "wait"),
    "waiting_for_browser": ("Browser attention required", "warning", "rebind"),
    "completed": ("Completed", "success", "view_result"),
    "failed": ("Failed", "error", "view_error"),
    "cancelled": ("Cancelled", "neutral_terminal", "start_another"),
}
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_PAUSABLE_STATUSES = frozenset({"running"})


def status_presentation(status_code: str) -> Tuple[str, str, str]:
    return STATUS_PRESENTATION.get(status_code, (status_code.replace("_", " ").title(), "neutral", "wait"))


def _plan_items(state: AxisJobState) -> List[DesktopPlanItem]:
    in_progress_seen = False
    items: List[DesktopPlanItem] = []
    for item in state.plan:
        active = item.status == "in_progress" and not in_progress_seen
        in_progress_seen = in_progress_seen or item.status == "in_progress"
        items.append(DesktopPlanItem(content=_bound(item.content), status=item.status, active=active))
    return items


_RISK_LABELS = {
    "read": "Read",
    "reversible_local": "Reversible local",
    "external_effect": "External effect",
    "destructive_high_impact": "Destructive/high impact",
}


def _risk_label(risk: str) -> str:
    return _RISK_LABELS.get(risk, risk.replace("_", " ").title())


_EFFECT_UNRESOLVED_STATUSES = frozenset({
    "prepared", "awaiting_approval", "approved", "starting", "executing",
    "executed_unverified", "unknown_after_crash", "reconciling",
})


def _effects(state: AxisJobState) -> List[DesktopEffectItem]:
    return [
        DesktopEffectItem(
            summary=_bound(effect.summary), risk=_risk_label(effect.risk),
            status=effect.status.replace("_", " "),
            mutation_count=effect.browser_mutation_count, max_mutations=effect.max_browser_mutations,
        )
        for effect in state.effects
    ]


def _acceptance(state: AxisJobState) -> List[DesktopAcceptanceItem]:
    intent = state.task_intent
    if intent is None:
        return []
    passed_by_criterion: Dict[str, bool] = {}
    failed_by_criterion: Dict[str, bool] = {}
    for effect in state.effects:
        for cid in effect.passed_criterion_ids:
            passed_by_criterion[cid] = True
        for cid in effect.failed_criterion_ids:
            failed_by_criterion[cid] = True
    items = []
    for criterion in intent.acceptance_criteria:
        if passed_by_criterion.get(criterion.criterion_id):
            state_label = "passed"
        elif failed_by_criterion.get(criterion.criterion_id):
            state_label = "failed"
        else:
            state_label = "pending"
        items.append(DesktopAcceptanceItem(description=_bound(criterion.description), state=state_label))
    return items


def _approval(state: AxisJobState) -> Optional[DesktopApproval]:
    request = state.pending_approval
    if request is None:
        return None
    return DesktopApproval(summary=_bound(request.summary), risk=_risk_label(request.risk), reason=_bound(request.reason))


def _question(state: AxisJobState) -> Optional[DesktopQuestion]:
    question = state.pending_question
    if question is None:
        return None
    return DesktopQuestion(question=_bound(question.question))


def _rebind_candidates(state: AxisJobState) -> List[DesktopRebindCandidate]:
    candidates = []
    for i, candidate in enumerate(state.rebind_candidates, start=1):
        candidates.append(DesktopRebindCandidate(
            label=f"Tab {i}", sanitized_url=_bound(candidate.sanitized_url, _MAX_TEXT),
            title=_bound(candidate.title, _MAX_SHORT_TEXT), active=candidate.active,
        ))
    return candidates


def _result(state: AxisJobState) -> Optional[DesktopResult]:
    result = state.result
    if result is None:
        return None
    return DesktopResult(
        status=result.status, summary=_bound(result.summary),
        verification_summary=_bound(result.verification_summary),
        error_code=result.error_code, retryable=result.retryable,
    )


def project_job_state(state: AxisJobState) -> DesktopJobSnapshot:
    """The one, and only, translation from authoritative durable state into
    what the desktop UI is allowed to render. Never called with anything
    other than a genuine `AxisJobState` returned by the durable client."""
    label, _kind, _action = status_presentation(state.status)
    is_terminal = state.status in _TERMINAL_STATUSES
    return DesktopJobSnapshot(
        display_status=label, status_code=state.status, task_summary=_bound(state.task_summary),
        plan_items=_plan_items(state), effects=_effects(state), acceptance=_acceptance(state),
        pending_approval=_approval(state), pending_question=_question(state),
        rebind_required=state.rebind_required, rebind_candidates=_rebind_candidates(state),
        result=_result(state), error_code=state.last_error_code,
        can_pause=(state.status in _PAUSABLE_STATUSES and not state.paused),
        can_resume=(state.status == "paused"),
        can_cancel=(not is_terminal and not state.cancel_requested),
        is_terminal=is_terminal,
    )


class DesktopTimelineEntry(BaseModel):
    kind: str
    text: str = Field(max_length=_MAX_TEXT)


def diff_timeline_entries(previous: Optional[DesktopJobSnapshot], current: DesktopJobSnapshot) -> List[DesktopTimelineEntry]:
    """Truthful, session-local transitions derived only from two
    consecutive authoritative snapshots — never a fabricated event, and
    never a reconstruction of history that happened before this session
    observed it (``previous=None`` yields no synthetic backfill, only a
    single "status is X" entry for the first observation)."""
    entries: List[DesktopTimelineEntry] = []
    if previous is None:
        entries.append(DesktopTimelineEntry(kind="status", text=f"Status: {current.display_status}"))
        return entries
    if previous.status_code != current.status_code:
        entries.append(DesktopTimelineEntry(kind="status", text=f"Status changed to {current.display_status}"))
    if len(current.plan_items) != len(previous.plan_items):
        entries.append(DesktopTimelineEntry(kind="plan", text="Plan updated"))
    else:
        for old_item, new_item in zip(previous.plan_items, current.plan_items):
            if old_item.status != new_item.status:
                entries.append(DesktopTimelineEntry(kind="plan", text=f"Plan step {new_item.status}: {new_item.content}"))
    if previous.pending_approval is None and current.pending_approval is not None:
        entries.append(DesktopTimelineEntry(kind="approval", text="Approval requested"))
    if previous.pending_approval is not None and current.pending_approval is None:
        entries.append(DesktopTimelineEntry(kind="approval", text="Approval resolved"))
    if previous.pending_question is None and current.pending_question is not None:
        entries.append(DesktopTimelineEntry(kind="question", text="The agent is asking a question"))
    if not previous.rebind_required and current.rebind_required:
        entries.append(DesktopTimelineEntry(kind="rebind", text="Browser rebind required"))
    if previous.rebind_required and not current.rebind_required:
        entries.append(DesktopTimelineEntry(kind="rebind", text="Browser rebound"))
    for old_acc, new_acc in zip(previous.acceptance, current.acceptance):
        if old_acc.state != new_acc.state and new_acc.state in ("passed", "failed"):
            entries.append(DesktopTimelineEntry(kind="acceptance", text=f"Acceptance {new_acc.state}: {new_acc.description}"))
    return entries
