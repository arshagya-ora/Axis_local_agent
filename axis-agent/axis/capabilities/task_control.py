"""The Phase 2 task-control Capability: exactly two AXIS tools,
``axis_set_task_intent`` and ``axis_prepare_effect``.

Neither tool executes a browser operation, grants authorization, or counts
as user approval. Both are the *runtime* authority for their own state
transitions — Task Intent structural validity comes from
``axis.effects.TaskIntent``'s pydantic validators, but revision timing,
effect/plan binding, and cardinality are enforced here against trusted
``ctx.deps`` state, never against anything the model merely asserts.

Both tools are declared ``sequential=True``: they mutate shared Task
Intent, effect-ledger, and (transitively, via the plan store) planning
state, and must not interleave with each other or with the browser tools.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from pydantic import ValidationError  # noqa: E402
from pydantic_ai import RunContext, Tool  # noqa: E402
from pydantic_ai.capabilities import Capability  # noqa: E402

from axis.effects import EffectError, EffectLedger, EffectRecord, TaskIntent  # noqa: E402
from axis.events import RunEventLogger  # noqa: E402
from axis.models import (  # noqa: E402
    AxisControlState,
    AxisRunDeps,
    BLOCKING_AMBIGUITY,
    CONTROL_STATE_ERROR,
    EFFECT_NOT_FOUND,
    PLAN_REQUIRED,
    PLAN_STEP_NOT_ACTIVE,
    PLAN_STEP_REQUIRED,
    TASK_INTENT_INVALID,
    TASK_INTENT_REQUIRED,
)
from axis.planning import read_plan_snapshot

CAPABILITY_ID = "axis.task_control"
CAPABILITY_DESCRIPTION = (
    "Two control tools for establishing typed intent and preparing a runtime-owned effect "
    "before any state-changing browser action."
)

_ASSERTION_SCHEMA: Dict[str, Any] = {"type": "object", "description": "A browser_assert-shaped assertion spec (no browserSessionId)."}

_ACCEPTANCE_CRITERION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "criterion_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "description": {"type": "string", "minLength": 1, "maxLength": 300},
        "required": {"type": "boolean", "default": True},
        "assertion": _ASSERTION_SCHEMA,
    },
    "required": ["criterion_id", "description", "assertion"],
    "additionalProperties": False,
}

_PROPOSED_EFFECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "effect_key": {"type": "string", "minLength": 1, "maxLength": 64},
        "summary": {"type": "string", "minLength": 1, "maxLength": 300},
        "risk": {"type": "string", "enum": ["read", "reversible_local", "external_effect", "destructive_high_impact"]},
        "allowed_tools": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        "allowed_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "max_browser_mutations": {"type": "integer", "minimum": 1, "maximum": 20},
        "acceptance_criterion_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
    },
    "required": ["effect_key", "summary", "risk", "allowed_tools", "max_browser_mutations", "acceptance_criterion_ids"],
    "additionalProperties": False,
}

_AMBIGUITY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 300},
        "blocking": {"type": "boolean", "default": True},
    },
    "required": ["question"],
    "additionalProperties": False,
}

SET_TASK_INTENT_DESCRIPTION = (
    "Establish or revise the typed interpretation of this task before any state-changing "
    "browser action: the goal, constraints, blocking ambiguities, bounded proposed effects, "
    "and machine-checkable browser_assert-based acceptance criteria. Optional for purely "
    "read-only tasks; required before any state-changing browser call. Does not execute a "
    "browser operation and does not grant authorization or approval — it only records intent. "
    "Calling this with a blocking ambiguity records the intent but prevents effect preparation "
    "until the ambiguity is resolved by a revision. Revisions are only allowed before any "
    "effect has started or is awaiting approval."
)

SET_TASK_INTENT_PARAMS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "minLength": 1, "maxLength": 500},
        "constraints": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 10},
        "ambiguities": {"type": "array", "items": _AMBIGUITY_SCHEMA, "maxItems": 10},
        "proposed_effects": {"type": "array", "items": _PROPOSED_EFFECT_SCHEMA, "maxItems": 10},
        "acceptance_criteria": {"type": "array", "items": _ACCEPTANCE_CRITERION_SCHEMA, "maxItems": 20},
    },
    "required": ["goal"],
    "additionalProperties": False,
}

PREPARE_EFFECT_DESCRIPTION = (
    "Select one proposed effect from the current Task Intent and bind it to an existing "
    "in_progress plan step, creating a runtime-owned prepared effect before any browser "
    "mutation. Does not execute or authorize the effect. Only one effect may be active at a "
    "time; a denied or completed effect cannot be prepared again automatically."
)

PREPARE_EFFECT_PARAMS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "effectKey": {"type": "string", "minLength": 1, "maxLength": 64},
        "planTaskId": {"type": "string", "minLength": 1},
    },
    "required": ["effectKey", "planTaskId"],
    "additionalProperties": False,
}


def _fail(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def apply_task_intent_transition(
    control: AxisControlState, *, goal: str, constraints: Optional[List[str]] = None,
    ambiguities: Optional[List[Dict[str, Any]]] = None,
    proposed_effects: Optional[List[Dict[str, Any]]] = None,
    acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    ledger = EffectLedger(control.effects)
    if ledger.has_started_or_awaiting():
        return _fail(CONTROL_STATE_ERROR, "Task Intent cannot be revised once an effect has started or is awaiting approval.")
    try:
        new_intent = TaskIntent(
            goal=goal, constraints=constraints or [], ambiguities=ambiguities or [],
            proposed_effects=proposed_effects or [], acceptance_criteria=acceptance_criteria or [],
        )
    except ValidationError as exc:
        return _fail(TASK_INTENT_INVALID, f"Task Intent is invalid: {exc.error_count()} field error(s).")
    is_revision = control.task_intent is not None
    if is_revision:
        old_keys = {e.effect_key for e in control.task_intent.proposed_effects}
        new_keys = {e.effect_key for e in new_intent.proposed_effects}
        for dropped_key in old_keys - new_keys:
            record = ledger.get_by_effect_key(dropped_key)
            if record is not None and record.status == "prepared":
                ledger.mark_superseded(record.effect_id)
    control.task_intent = new_intent
    return {
        "ok": True, "blockingAmbiguities": any(a.blocking for a in new_intent.ambiguities),
        "proposedEffectCount": len(new_intent.proposed_effects),
        "acceptanceCriterionCount": len(new_intent.acceptance_criteria),
        "_axis_intent": new_intent.model_dump(mode="json"), "_axis_revision": is_revision,
    }


def _make_set_task_intent(event_logger: RunEventLogger, *, persist: bool):
    async def axis_set_task_intent(
        ctx: RunContext[AxisRunDeps],
        goal: str,
        constraints: Optional[List[str]] = None,
        ambiguities: Optional[List[Dict[str, Any]]] = None,
        proposed_effects: Optional[List[Dict[str, Any]]] = None,
        acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        target = ctx.deps.control_state if persist else ctx.deps.control_state.model_copy(deep=True)
        result = apply_task_intent_transition(
            target, goal=goal, constraints=constraints, ambiguities=ambiguities,
            proposed_effects=proposed_effects, acceptance_criteria=acceptance_criteria,
        )
        if result.get("ok") and persist:
            event_logger.intent_revised(ctx.deps.run_id) if result.pop("_axis_revision") else event_logger.intent_created(ctx.deps.run_id)
            result.pop("_axis_intent", None)
        return result

    return axis_set_task_intent


def prepare_effect_transition(
    control: AxisControlState, *, effect_key: str, plan_task_id: str,
    id_factory: Optional[Callable[[str, str, int], str]] = None,
) -> Dict[str, Any]:
    intent = control.task_intent
    if intent is None:
        return _fail(TASK_INTENT_REQUIRED, "Call axis_set_task_intent before preparing an effect.")
    if any(a.blocking for a in intent.ambiguities):
        return _fail(BLOCKING_AMBIGUITY, "Resolve the blocking ambiguity before preparing an effect.")
    proposed = next((e for e in intent.proposed_effects if e.effect_key == effect_key), None)
    if proposed is None:
        return _fail(EFFECT_NOT_FOUND, f"No proposed effect named {effect_key!r} in the current Task Intent.")
    task = next((item for item in control.plan if item.task_id == plan_task_id), None)
    if task is None:
        return _fail(PLAN_STEP_REQUIRED, f"No plan step with id {plan_task_id!r}.")
    if task.status != "in_progress":
        return _fail(PLAN_STEP_NOT_ACTIVE, f"Plan step {plan_task_id!r} is not in_progress.")
    try:
        record = EffectLedger(control.effects, id_factory=id_factory).prepare(
            effect_key=effect_key, plan_task_id=plan_task_id, risk=proposed.risk, summary=proposed.summary,
            allowed_tools=proposed.allowed_tools, allowed_actions=proposed.allowed_actions,
            max_browser_mutations=proposed.max_browser_mutations,
            acceptance_criterion_ids=proposed.acceptance_criterion_ids,
        )
    except EffectError as exc:
        return _fail(exc.code, str(exc))
    return {
        "ok": True, "risk": record.risk, "maxBrowserMutations": record.max_browser_mutations,
        "_axis_effect": record.model_dump(mode="json"),
    }


def _make_prepare_effect(event_logger: RunEventLogger, *, persist: bool):
    async def axis_prepare_effect(ctx: RunContext[AxisRunDeps], effectKey: str, planTaskId: str) -> Dict[str, Any]:
        control = ctx.deps.control_state
        if persist:
            control.plan = await read_plan_snapshot(ctx.deps.plan_store)
        target = control if persist else control.model_copy(deep=True)
        result = prepare_effect_transition(
            target, effect_key=effectKey, plan_task_id=planTaskId,
            id_factory=getattr(ctx.deps, "effect_id_factory", None),
        )
        if result.get("ok") and persist:
            event_logger.effect_prepared(ctx.deps.run_id, EffectRecord.model_validate(result["_axis_effect"]).risk)
            result.pop("_axis_effect", None)
        return result

    return axis_prepare_effect


def build_task_control_capability(
    event_logger: RunEventLogger, *, persist_in_handler: bool = True,
    activity_metadata: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Capability[AxisRunDeps]:
    tools = [
        Tool.from_schema(
            function=_make_set_task_intent(event_logger, persist=persist_in_handler), name="axis_set_task_intent",
            description=SET_TASK_INTENT_DESCRIPTION, json_schema=SET_TASK_INTENT_PARAMS,
            takes_ctx=True, sequential=True,
        ),
        Tool.from_schema(
            function=_make_prepare_effect(event_logger, persist=persist_in_handler), name="axis_prepare_effect",
            description=PREPARE_EFFECT_DESCRIPTION, json_schema=PREPARE_EFFECT_PARAMS,
            takes_ctx=True, sequential=True,
        ),
    ]
    if activity_metadata:
        tools[0].metadata = activity_metadata("axis_set_task_intent")
        tools[1].metadata = activity_metadata("axis_prepare_effect")
    return Capability(id=CAPABILITY_ID, description=CAPABILITY_DESCRIPTION, tools=tools)
