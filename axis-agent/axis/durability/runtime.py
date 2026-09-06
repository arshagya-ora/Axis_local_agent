"""Small Temporal adaptation layer for the authoritative AXIS agent."""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from pydantic_ai import RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai_harness.guardrails import GuardrailResult, ToolGuardrail, ToolResultInfo
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem, Planning, TaskStatus
from pydantic_ai_harness.planning._toolset import PlanningToolset
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from axis.agent import _build_system_reminders, build_axis_agent, build_axis_guard
from axis.capabilities.browser import build_browser_capability
from axis.capabilities.task_control import build_task_control_capability
from axis.config import AxisConfig
from axis.effects import (
    EffectLedger, EffectRecord, classify_mutation_outcome, criterion_matches_assertion,
    is_non_idempotent_operation, requires_effect,
)
from axis.events import RunEventLogger
from axis.planning import AxisPlanItem
from axis.durability.activities import get_current_worker_manifest
from axis.durability.models import AxisDurableDeps, BrowserRebindResult, BrowserRecoveryProjection, RebindCandidate, derive_effect_id
from browser_agent_tools import BrowserAgentTools, get_tool_handlers

BROWSER_REBIND_REQUIRED = "BROWSER_REBIND_REQUIRED"
LEASE_NOT_HELD = "LEASE_NOT_HELD"


class _NoopEventLogger:
    """Deterministic logger used only by workflow-side shared guard code."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *args, **kwargs: None


@dataclass
class _PlanCacheEntry:
    store: InMemoryPlanStore
    revision: int


class _HydratedPlanStore(InMemoryPlanStore):
    def __init__(self, projection: list[AxisPlanItem]) -> None:
        super().__init__()
        # The pinned 0.29.0 implementation stores detached PlanItem values in
        # this list. We hydrate synchronously because Planning.resolve_store is
        # synchronous; every subsequent operation still uses its public API.
        self._items = [PlanItem(
            id=item.task_id, content=item.content, status=TaskStatus(item.status),
            active_form="", parent_id=None, depends_on=[],
        ) for item in projection]


_plan_store_cache: Dict[str, _PlanCacheEntry] = {}
_plan_store_lock = threading.Lock()


def _resolve_plan_store(ctx: RunContext[AxisDurableDeps]) -> InMemoryPlanStore:
    deps = ctx.deps
    revision = deps.control_state.plan_revision
    with _plan_store_lock:
        entry = _plan_store_cache.get(deps.job_id)
        if entry is None or entry.revision != revision:
            entry = _PlanCacheEntry(_HydratedPlanStore(deps.control_state.plan), revision)
            _plan_store_cache[deps.job_id] = entry
        return entry.store


def clear_plan_cache(job_id: Optional[str] = None) -> None:
    with _plan_store_lock:
        if job_id is None:
            _plan_store_cache.clear()
        else:
            _plan_store_cache.pop(job_id, None)


def _project_plan(items: list[PlanItem]) -> list[AxisPlanItem]:
    return [AxisPlanItem(
        task_id=item.id, content=item.content, status=item.status.value,
    ) for item in items]


def apply_plan_projection(control: Any, result: Dict[str, Any]) -> bool:
    """Apply only the next projection; stale/empty activity state cannot win."""
    if result.get("_axis_plan") is None:
        return False
    base = result.get("_axis_plan_base_revision")
    revision = result.get("_axis_plan_revision")
    if base != control.plan_revision or revision != base + 1:
        return False
    control.plan = [AxisPlanItem.model_validate(item) for item in result["_axis_plan"]]
    control.plan_revision = revision
    return True


class _DurablePlanningToolset(PlanningToolset[AxisDurableDeps]):
    def __init__(self, capability: Planning[AxisDurableDeps]) -> None:
        super().__init__(capability)
        # Every mutating Planning tool (everything but the pure-read
        # read_plan) must execute alone, never overlapping another tool
        # call — the compare-and-set write in _write_result below reads
        # deps.control_state.plan_revision as its base and would otherwise
        # be racy against a second concurrent mutating plan call in the
        # same run step.
        for name, tool in self.tools.items():
            if name != "read_plan":
                tool.sequential = True

    def _resolve(self, ctx: RunContext[AxisDurableDeps]) -> InMemoryPlanStore:
        return _resolve_plan_store(ctx)

    async def _write_result(self, ctx: RunContext[AxisDurableDeps], message: str) -> Dict[str, Any]:
        base = ctx.deps.control_state.plan_revision
        store = self._resolve(ctx)
        projection = _project_plan(await store.get_items())
        with _plan_store_lock:
            _plan_store_cache[ctx.deps.job_id] = _PlanCacheEntry(store, base + 1)
        return {"message": message, "_axis_plan": [p.model_dump(mode="json") for p in projection],
                "_axis_plan_base_revision": base, "_axis_plan_revision": base + 1}

    async def write_plan(self, ctx: RunContext[AxisDurableDeps], items: list[PlanItem]) -> Any:
        return await self._write_result(ctx, await super().write_plan(ctx, items))

    async def add_task(self, ctx: RunContext[AxisDurableDeps], content: str, active_form: str = "") -> Any:
        return await self._write_result(ctx, await super().add_task(ctx, content, active_form))

    async def update_task_status(self, ctx: RunContext[AxisDurableDeps], task_id: str, status: TaskStatus) -> Any:
        return await self._write_result(ctx, await super().update_task_status(ctx, task_id, status))

    async def update_task_statuses(self, ctx: RunContext[AxisDurableDeps], updates: list[Any]) -> Any:
        return await self._write_result(ctx, await super().update_task_statuses(ctx, updates))

    async def remove_task(self, ctx: RunContext[AxisDurableDeps], task_id: str) -> Any:
        return await self._write_result(ctx, await super().remove_task(ctx, task_id))


class DurablePlanning(Planning[AxisDurableDeps]):
    _cached_toolset: Any = None

    async def for_run(self, ctx: RunContext[AxisDurableDeps]) -> "DurablePlanning":
        # Harness normally resolves its store while preparing a run. Durable
        # tools execute as activities, so resolving here would consult a
        # process cache from workflow code. The activity-side toolset below
        # hydrates from serialized deps instead.
        return self

    def get_toolset(self) -> Any:
        if self._cached_toolset is None:
            object.__setattr__(self, "_cached_toolset", _DurablePlanningToolset(self))
        return self._cached_toolset


@dataclass
class _BrowserCacheEntry:
    tools: BrowserAgentTools
    binding_generation: int
    bindings: Dict[str, str]


_browser_tools_cache: Dict[tuple[str, str], _BrowserCacheEntry] = {}
_browser_tools_lock = threading.Lock()


class _DispatchTracker:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.stage = "authorized"

    def rpc(self, *args: Any, **kwargs: Any) -> Any:
        self.stage = "dispatch_started"
        result = self.delegate.rpc(*args, **kwargs)
        self.stage = "response_received"
        return result

    def save_data_url(self, *args: Any, **kwargs: Any) -> Any:
        self.stage = "dispatch_started"
        result = self.delegate.save_data_url(*args, **kwargs)
        self.stage = "response_received"
        return result


def seed_browser_tools(
    scope_key: str, tools: BrowserAgentTools, *, binding_generation: int = 0,
    job_id: Optional[str] = None,
) -> None:
    """Seed a disposable test/runtime cache entry.

    Production entries are isolated by job.  The wildcard key exists only
    so deterministic activity tests can seed a fake bridge before the
    activity dependencies (and therefore their generated job id) exist; it
    is consumed by the first resolving job.
    """
    with _browser_tools_lock:
        _browser_tools_cache[(scope_key, job_id or "*")] = _BrowserCacheEntry(tools, binding_generation, {})


def clear_browser_cache(scope_key: Optional[str] = None) -> None:
    with _browser_tools_lock:
        if scope_key is None:
            _browser_tools_cache.clear()
        else:
            for key in [key for key in _browser_tools_cache if key[0] == scope_key]:
                _browser_tools_cache.pop(key, None)


def _safe_error(code: str, message: str, session: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": False, "browserSessionId": session, "data": None,
            "error": {"code": code, "message": message, "retryable": False, "diagnostic": None}}


def _matching_handles(tools: BrowserAgentTools, projection: BrowserRecoveryProjection) -> list[str]:
    # Active-tab state by itself is not a durable identity.  Without a
    # trusted URL or title, choosing the sole/active tab after replacement
    # could silently bind a different page.
    if projection.last_known_sanitized_url is None and projection.last_known_title is None:
        return []
    matches = []
    for handle, record in tools.tab_registry.items():
        if record.get("closed"):
            continue
        if projection.last_known_sanitized_url is not None and tools._sanitize_url_for_model(record.get("url")) != projection.last_known_sanitized_url:
            continue
        if projection.last_known_title is not None and record.get("title") != projection.last_known_title:
            continue
        if projection.active_tab_intent and not record.get("active"):
            continue
        matches.append(handle)
    return matches


_MAX_REBIND_CANDIDATES = 10


def _build_rebind_candidates(entry: "_BrowserCacheEntry", projection: BrowserRecoveryProjection) -> list[RebindCandidate]:
    """Bounded, opaque candidates for a human to disambiguate a rebind with.

    Candidate ids are derived from this specific cache entry (created fresh
    by the refresh that called this) and recorded into that entry's own
    ``bindings`` map — never the raw Chrome/bridge tab id. A prior refresh's
    candidate ids live in a cache entry this call never touches, so they
    cannot be replayed against a newer refresh (see
    ``axis.durability.activities.select_browser_rebind_candidate``)."""
    matches = _matching_handles(entry.tools, projection) if (projection.last_known_sanitized_url or projection.last_known_title) else []
    if not matches:
        matches = [handle for handle, record in entry.tools.tab_registry.items() if not record.get("closed")]
    candidates: list[RebindCandidate] = []
    for handle in matches[:_MAX_REBIND_CANDIDATES]:
        record = entry.tools.tab_registry.get(handle, {})
        candidate_id = hashlib.sha256(f"{id(entry)}:{handle}".encode("utf-8")).hexdigest()[:16]
        entry.bindings[candidate_id] = handle
        candidates.append(RebindCandidate(
            candidate_id=candidate_id,
            sanitized_url=entry.tools._sanitize_url_for_model(record.get("url")),
            title=record.get("title"),
            active=bool(record.get("active")),
        ))
    return candidates


def _new_browser_tools(deps: AxisDurableDeps) -> BrowserAgentTools:
    from browser_bridge_client import BrowserBridgeClient
    return BrowserAgentTools(
        BrowserBridgeClient(), allow_response_body=deps.allow_response_body,
        firewall_config=deps.firewall, max_open_tabs=deps.max_open_tabs,
    )


def _resolve_browser_entry(deps: AxisDurableDeps) -> tuple[_BrowserCacheEntry, BrowserRecoveryProjection]:
    projection = deps.browser_projection.model_copy(deep=True)
    cache_key = (deps.browser_scope_key, deps.job_id)
    with _browser_tools_lock:
        entry = _browser_tools_cache.get(cache_key)
        if entry is None:
            entry = _browser_tools_cache.pop((deps.browser_scope_key, "*"), None)
            if entry is not None:
                _browser_tools_cache[cache_key] = entry
        cache_miss = entry is None or entry.binding_generation != projection.binding_generation
        if cache_miss:
            tools = _new_browser_tools(deps)
            refreshed = tools.refresh_tabs()
            entry = _BrowserCacheEntry(tools, projection.binding_generation, {})
            _browser_tools_cache[cache_key] = entry
            projection.observation_generation += 1
            projection.fresh_observation = False
            if not refreshed.get("ok"):
                projection.rebind_required = True
            elif projection.durable_tab_key:
                matches = _matching_handles(tools, projection)
                if len(matches) == 1:
                    entry.bindings[projection.durable_tab_key] = matches[0]
                    projection.rebind_required = False
                else:
                    projection.rebind_required = True
        return entry, projection


async def _lease_is_current(deps: AxisDurableDeps) -> bool:
    if not deps.leases_enabled:
        return True
    proof = deps.lease_proof
    if proof is None:
        return False
    from axis.durability.leases import BrowserLeaseWorkflow, _RenewSignal
    handle = activity.client().get_workflow_handle(deps.browser_scope_key)
    await handle.signal(BrowserLeaseWorkflow.renew, _RenewSignal(
        job_id=deps.job_id, token=proof.token, generation=proof.generation,
        duration_seconds=deps.lease_duration_seconds,
    ))
    owner = await handle.query(BrowserLeaseWorkflow.get_owner)
    return bool(owner and owner.job_id == deps.job_id and owner.token == proof.token and
                owner.generation == proof.generation and owner.expires_at > time.time())


def _current_activity_attempt() -> int:
    try:
        return activity.info().attempt
    except RuntimeError:
        # Called from a unit test outside a real activity context — treat as
        # a first attempt (there is nothing to guard against yet).
        return 1


async def durable_browser_handler(tool_name: str, ctx: RunContext[AxisDurableDeps], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    deps = ctx.deps
    current_manifest = get_current_worker_manifest()
    if deps.expected_manifest is not None and current_manifest is not None and not deps.expected_manifest.is_compatible_with(current_manifest):
        result = _safe_error(
            "VERSION_INCOMPATIBLE",
            "This worker's version is incompatible with the job's expected manifest.",
            kwargs.get("browserSessionId"),
        )
        result["_axis_internal"] = {"projection": deps.browser_projection.model_dump(mode="json"), "execution_stage": "validated"}
        return result

    if not await _lease_is_current(deps):
        result = _safe_error(LEASE_NOT_HELD, "The browser lease is not current.", kwargs.get("browserSessionId"))
        result["_axis_internal"] = {"projection": deps.browser_projection.model_dump(mode="json"), "execution_stage": "authorized"}
        return result

    entry, projection = _resolve_browser_entry(deps)

    if _current_activity_attempt() > 1 and is_non_idempotent_operation(tool_name, action=kwargs.get("action"), operation=kwargs.get("operation")):
        # Defense in depth beyond the tool-level Temporal retry policy (see
        # _activity_metadata): even if this activity were somehow attempted
        # again, the exact action/operation actually requested — only known
        # here, not at Tool-config time — is checked directly, and dispatch
        # is refused rather than ever repeating a non-idempotent operation.
        result = _safe_error(
            "NON_IDEMPOTENT_RETRY_BLOCKED",
            "This operation cannot be safely retried after a prior dispatch attempt.",
            kwargs.get("browserSessionId"),
        )
        result["_axis_internal"] = {"projection": projection.model_dump(mode="json"), "execution_stage": "authorized"}
        return result

    requested = kwargs.get("browserSessionId")
    if tool_name != "browser_tabs" or kwargs.get("operation") in ("activate", "close"):
        local = entry.bindings.get(requested, requested)
        if projection.rebind_required or local not in entry.tools.tab_registry:
            result = _safe_error(BROWSER_REBIND_REQUIRED, "The intended tab must be rebound safely.", requested)
            result["_axis_internal"] = {"projection": projection.model_dump(mode="json"), "execution_stage": "authorized"}
            return result
        call_args = dict(kwargs)
        call_args["browserSessionId"] = local
    else:
        call_args = dict(kwargs)

    original_client = entry.tools.bridge_client
    tracker = _DispatchTracker(original_client)
    entry.tools.bridge_client = tracker
    try:
        result = get_tool_handlers(entry.tools)[tool_name](call_args)
    finally:
        entry.tools.bridge_client = original_client

    local_result_handle = result.get("browserSessionId") if isinstance(result, dict) else None
    durable_handle = requested if isinstance(requested, str) else local_result_handle
    if tool_name == "browser_tabs" and kwargs.get("operation") == "list" and result.get("ok"):
        tabs = (result.get("data") or {}).get("tabs") or []
        for tab in tabs:
            local_handle = tab.get("browserSessionId")
            public_handle = local_handle
            if projection.durable_tab_key and local_handle in _matching_handles(entry.tools, projection):
                public_handle = projection.durable_tab_key
                entry.bindings[public_handle] = local_handle
            else:
                entry.bindings[local_handle] = local_handle
            tab["browserSessionId"] = public_handle
        active = [tab for tab in tabs if tab.get("active")]
        if projection.durable_tab_key is None and len(active) == 1:
            chosen = active[0]
            projection.durable_tab_key = chosen.get("browserSessionId")
            projection.last_known_sanitized_url = chosen.get("url")
            projection.last_known_title = chosen.get("title")
            projection.active_tab_intent = True
            projection.rebind_required = False
    elif isinstance(durable_handle, str) and result.get("ok"):
        local = entry.bindings.get(durable_handle, local_result_handle or durable_handle)
        record = entry.tools.tab_registry.get(local)
        if record:
            projection.durable_tab_key = durable_handle
            projection.last_known_sanitized_url = entry.tools._sanitize_url_for_model(record.get("url"))
            projection.last_known_title = record.get("title")
            projection.active_tab_intent = bool(record.get("active"))
            projection.rebind_required = False
            result["browserSessionId"] = durable_handle
        if tool_name == "browser_observe":
            projection.observation_generation += 1
            projection.fresh_observation = True
        elif tool_name in ("browser_navigate", "browser_act", "browser_tabs"):
            projection.fresh_observation = False

    result["_axis_internal"] = {"projection": projection.model_dump(mode="json"), "execution_stage": tracker.stage}
    return result


# The only tools proven read-only at every action/operation they offer.
# Temporal's per-tool ActivityConfig cannot branch on a specific call's
# action/operation (that isn't known until the model supplies arguments,
# after the tool's activity config is already resolved) — so this is the
# finest granularity a *tool name* can safely retry at; every other tool
# (browser_act, browser_navigate, browser_tabs, browser_capture_evidence)
# can perform a non-idempotent operation somewhere in its surface (a click,
# a navigation, a tab create/close, a trace start/stop or artifact write)
# and therefore gets exactly one attempt. The exact action/operation IS
# checked once it's known — see durable_browser_handler's attempt guard,
# which uses axis.effects.is_non_idempotent_operation for real per-call
# enforcement independent of this coarser, tool-level Temporal policy.
_RETRYABLE_READ_TOOLS = frozenset({"browser_observe", "browser_wait", "browser_assert", "browser_diagnose"})


def _activity_metadata(config: AxisConfig, tool_name: str) -> Dict[str, Any]:
    retryable = tool_name in _RETRYABLE_READ_TOOLS
    seconds = (config.phase3.temporal.browser_read_activity_timeout_seconds if retryable
               else config.phase3.temporal.browser_mutation_activity_timeout_seconds)
    attempts = (config.phase3.retries.browser_read_max_attempts if retryable
                else config.phase3.retries.browser_mutation_max_attempts)
    return {"temporal": ActivityConfig(
        start_to_close_timeout=timedelta(seconds=seconds),
        retry_policy=RetryPolicy(maximum_attempts=attempts),
    )}


def _apply_acceptance(deps: AxisDurableDeps, args: Dict[str, Any], result: Dict[str, Any]) -> None:
    ledger = EffectLedger(deps.control_state.effects)
    effect = (ledger.get(deps.reconciling_effect_id) if deps.reconciling_effect_id else ledger.get_active())
    if effect is None or deps.control_state.task_intent is None or not result.get("ok"):
        return
    trusted = result.get("browserSessionId")
    passed = ((result.get("data") or {}).get("passed") is True)
    for cid in effect.acceptance_criterion_ids:
        criterion = next((c for c in deps.control_state.task_intent.acceptance_criteria if c.criterion_id == cid), None)
        if criterion and trusted and criterion_matches_assertion(criterion, args, effect=effect, trusted_session_id=trusted):
            if passed:
                ledger.mark_acceptance_passed(effect.effect_id, cid)
            elif cid not in effect.failed_criterion_ids:
                ledger._set(effect.effect_id, failed_criterion_ids=[*effect.failed_criterion_ids, cid])
    updated = ledger.get(effect.effect_id)
    required = {c.criterion_id for c in deps.control_state.task_intent.acceptance_criteria
                if c.required and c.criterion_id in updated.acceptance_criterion_ids}
    if not deps.reconciling_effect_id and required and required.issubset(set(updated.passed_criterion_ids)):
        ledger.mark_succeeded(updated.effect_id)


def build_durable_tool_guardrail(config: AxisConfig, logger: RunEventLogger) -> ToolGuardrail[AxisDurableDeps]:
    shared_guard = build_axis_guard(config.phase2.approvals, _NoopEventLogger())

    async def result_guard(ctx: RunContext[AxisDurableDeps], call: ToolResultInfo) -> GuardrailResult:
        deps = ctx.deps
        result = call.result
        if not isinstance(result, dict):
            return GuardrailResult.allow()
        if call.name == "axis_set_task_intent" and result.get("_axis_intent"):
            from axis.effects import TaskIntent
            deps.control_state.task_intent = TaskIntent.model_validate(result["_axis_intent"])
        elif call.name == "axis_prepare_effect" and result.get("_axis_effect"):
            effect = EffectRecord.model_validate(result["_axis_effect"])
            effect = effect.model_copy(update={"effect_id": derive_effect_id(
                deps.job_id, effect.effect_key, effect.plan_task_id, effect.mutation_ordinal,
            )})
            if not any(item.effect_id == effect.effect_id for item in deps.control_state.effects):
                deps.control_state.effects.append(effect)
        if result.get("_axis_plan") is not None:
            if not apply_plan_projection(deps.control_state, result):
                # A stale base revision must never be silently discarded
                # while the tool call still reports success to the model —
                # that would lose whichever write actually did apply. Tell
                # the model plainly and have it re-read the current plan.
                return GuardrailResult.retry(
                    "PLAN_CONFLICT: the plan changed concurrently (stale base revision). "
                    "Call read_plan to see the current plan, then retry your change against it."
                )
        internal = result.get("_axis_internal")
        if internal:
            deps.browser_projection = BrowserRecoveryProjection.model_validate(internal["projection"])
            args = dict(call.args)
            action, operation = args.get("action"), args.get("operation")
            if requires_effect(call.name, action=action, operation=operation):
                ledger = EffectLedger(deps.control_state.effects)
                effect = ledger.get_active()
                if effect and effect.status in ("starting", "executing"):
                    outcome = classify_mutation_outcome(
                        execution_stage=internal["execution_stage"], result=result,
                    )
                    ledger.mark_outcome(effect.effect_id, outcome, internal["execution_stage"])
                    if outcome == "applied_unverified" and result.get("browserSessionId"):
                        ledger._set(effect.effect_id, target_browser_session_id=result["browserSessionId"])
            elif call.name == "browser_observe" and result.get("ok"):
                deps.control_state.observed_current_run = True
            elif call.name == "browser_navigate" and result.get("ok"):
                deps.control_state.observed_current_run = False
                deps.control_state.assertion_passed_after_effect = False
            elif call.name == "browser_assert":
                _apply_acceptance(deps, args, result)
                if result.get("ok") and ((result.get("data") or {}).get("passed") is True):
                    deps.control_state.assertion_passed_after_effect = True
        clean = {key: value for key, value in result.items() if not key.startswith("_axis_")}
        if set(clean) != set(result):
            if "message" in clean and len(clean) == 1:
                return GuardrailResult.replace(clean["message"])
            return GuardrailResult.replace(clean)
        return GuardrailResult.allow()

    return ToolGuardrail(guard=shared_guard, result_guard=result_guard, tools=None)


def build_durable_agent(config: AxisConfig, model: Any) -> tuple[Any, TemporalDurability]:
    durability = TemporalDurability(
        deps_type=AxisDurableDeps,
        model_activity_config=ActivityConfig(
            start_to_close_timeout=timedelta(seconds=config.phase3.temporal.model_activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=config.phase3.retries.model_max_attempts),
        ),
    )
    logger = RunEventLogger()
    browser = build_browser_capability(
        logger, handler_adapter=durable_browser_handler,
        activity_metadata=lambda name: _activity_metadata(config, name),
        persist_in_handler=False, catch_exceptions=False,
    )
    task_control = build_task_control_capability(
        logger, persist_in_handler=False,
        activity_metadata=lambda name: {"temporal": ActivityConfig(
            start_to_close_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=1),
        )},
    )
    # Durable workflow state, not Harness's per-run reminder cache, is the
    # plan source of truth. Keeping reminder injection off also avoids a
    # read-only durable operation before every model request; the genuine
    # Planning public tools remain fully available.
    planning = DurablePlanning(store_resolver=_resolve_plan_store, enable_subtasks=False, inject=False, id="planning")
    guardrail = build_durable_tool_guardrail(config, logger)
    reminders = _build_system_reminders(
        config.phase2.reminders.interval_requests, config.phase2.reminders.max_fires,
    )
    agent = build_axis_agent(
        model, deps_type=AxisDurableDeps, browser_capability=browser,
        planning_capability=planning, task_control_capability=task_control,
        tool_guardrail=guardrail, system_reminders=reminders,
        extra_capabilities=(durability,), approvals=config.phase2.approvals,
        max_retries=config.max_retries, name="axis_agent",
    )
    bound = next(c for c in agent.root_capability.capabilities if isinstance(c, TemporalDurability))
    return agent, bound


class DurableAgentNotInitialized(RuntimeError):
    pass


_agent_holder: Dict[str, Any] = {"agent": None}


def set_durable_agent(agent: Any) -> None:
    _agent_holder["agent"] = agent


def get_durable_agent() -> Any:
    agent = _agent_holder.get("agent")
    if agent is None:
        raise DurableAgentNotInitialized("The durable agent is not initialized.")
    return agent


def mark_active_effect_unknown(deps: AxisDurableDeps) -> Optional[str]:
    ledger = EffectLedger(deps.control_state.effects)
    effect = ledger.get_active()
    if effect is None or effect.status not in ("starting", "executing"):
        return None
    ledger.mark_outcome(effect.effect_id, "unknown", "dispatch_started")
    return effect.effect_id
