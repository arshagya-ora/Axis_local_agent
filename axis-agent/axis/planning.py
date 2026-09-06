"""Thin AXIS adapter around the real Harness ``Planning`` capability.

No plan toolset, plan model, or plan engine is reimplemented here — every
tool the model calls (``write_plan``, ``read_plan``, ``add_task``,
``update_task_status``, ``update_task_statuses``, ``remove_task``) is the
genuine ``pydantic_ai_harness.planning.PlanningToolset``. This module only:

1. Builds one explicit ``InMemoryPlanStore`` per AXIS *task* (not per run
   segment — see ``axis.agent``'s docstring on why ``Planning(store=None)``
   would silently lose the plan across an approval-resume run segment), with
   a ``PlanEventEmitter`` wired to AXIS's own ``RunEventLogger`` so granular
   step transitions produce truthful, AXIS-timestamped events.
2. Projects the harness's ``PlanItem`` into a bounded ``AxisPlanItem`` for
   the guardrail/output-validator/CLI to read, so nothing outside this
   module depends on the harness's own model shape.
3. Answers the small deterministic questions Phase 2's gates need: is there
   a plan at all, which single task is ``in_progress``, which required
   items are still incomplete.
4. Diffs a plan snapshot across run-segment boundaries and emits
   ``plan_created``/``plan_updated`` — ``write_plan`` (bulk replace) is
   event-silent by design in the harness store, so this is the only place
   AXIS can truthfully report a bulk change; it never fabricates per-step
   events for a change it didn't actually observe granularly.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanEvent, PlanEventEmitter, PlanItem, Planning, TaskStatus

from axis.events import RunEventLogger


class AxisPlanItem(BaseModel):
    """Bounded projection of a harness ``PlanItem`` — the only plan shape
    Phase 2 code outside this module ever sees."""

    task_id: str
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]


_MAX_CONTENT_CHARS = 300


def _project(item: PlanItem) -> AxisPlanItem:
    return AxisPlanItem(
        task_id=item.id,
        content=item.content[:_MAX_CONTENT_CHARS],
        status=item.status.value if item.status != TaskStatus.blocked else "pending",
    )


def create_task_plan_store(logger: RunEventLogger, run_id: str) -> InMemoryPlanStore:
    """One fresh store per AXIS task, with events wired to ``logger``.

    Granular tools (``add_task``/``update_task_status(es)``/``remove_task``)
    emit through this store's ``PlanEventEmitter`` and so map to
    ``plan_step_started``/``plan_step_completed``/``plan_step_blocked``
    here. ``write_plan`` does not use ``set_items`` through an emitting
    path that fires granular events (confirmed in the harness source: bulk
    replacement is event-silent) — its effect is instead picked up by
    ``diff_and_emit_plan_events`` at the next run-segment boundary.
    """
    emitter = PlanEventEmitter()

    def _on_status_changed(event: PlanEvent) -> None:
        new_status = event.item.status
        if new_status == TaskStatus.in_progress:
            logger.plan_step_started(run_id, event.item.id)
        elif new_status == TaskStatus.completed:
            logger.plan_step_completed(run_id, event.item.id)
        elif new_status == TaskStatus.blocked:
            logger.plan_step_blocked(run_id, event.item.id)

    emitter.on_status_changed(_on_status_changed)
    return InMemoryPlanStore(event_emitter=emitter)


def build_planning_capability(store: InMemoryPlanStore) -> Planning:
    """The real Harness ``Planning`` capability bound to an explicit,
    task-scoped store. ``enable_subtasks=False`` (the task's default) keeps
    ``blocked`` unreachable and the six-tool surface exactly as documented."""
    return Planning(store=store, tools=None, enable_subtasks=False, inject=True, id="planning")


async def read_plan_snapshot(store: InMemoryPlanStore) -> List[AxisPlanItem]:
    items = await store.get_items()
    return [_project(item) for item in items]


async def single_in_progress_task(store: InMemoryPlanStore) -> Optional[AxisPlanItem]:
    """The one ``in_progress`` item, or ``None`` if there are zero or more
    than one — "more than one" is treated the same as "none" because the
    plan invariant is exactly one active step at a time."""
    items = await read_plan_snapshot(store)
    in_progress = [item for item in items if item.status == "in_progress"]
    return in_progress[0] if len(in_progress) == 1 else None


async def required_items_incomplete(store: InMemoryPlanStore) -> List[AxisPlanItem]:
    items = await read_plan_snapshot(store)
    return [item for item in items if item.status in ("pending", "in_progress")]


async def diff_and_emit_plan_events(
    logger: RunEventLogger, run_id: str, previous: List[AxisPlanItem], store: InMemoryPlanStore,
) -> List[AxisPlanItem]:
    """Compare ``previous`` against the store's current content; emit
    exactly one truthful ``plan_created``/``plan_updated`` if it changed.
    Returns the new snapshot for the caller to cache as the next
    ``previous``. Never called from inside a tool/guard hook — only from
    the run-segment boundary in ``axis.agent.run_axis_task``."""
    current = await read_plan_snapshot(store)
    if current == previous:
        return current
    if not previous and current:
        logger.plan_created(run_id)
    else:
        logger.plan_updated(run_id)
    return current
