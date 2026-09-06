"""All hand-written Phase 3 I/O activities."""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy

from axis.durability.leases import (
    BrowserLeaseWorkflow, LeaseWorkflowInput, _AcquireSignal, _ReleaseSignal, _RenewSignal,
    acquire_result, fenced_operation_result,
)
from axis.durability.models import (
    AxisDurableDeps, AxisVersionManifest, BrowserRebindResult, BrowserRecoveryProjection,
    LeaseAcquireResult, LeaseOperationResult, WorkerRuntimeInfo,
)
from axis.events import AxisEvent, RunEventLogger

_durable_event_logger = RunEventLogger()
_worker_runtime_info: Optional[WorkerRuntimeInfo] = None


class EmitEventRequest(BaseModel):
    run_id: str
    event_type: str
    tool_name: Optional[str] = None
    tab_handle: Optional[str] = None
    detail: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    success: Optional[bool] = None


@activity.defn
async def emit_durable_event(request: EmitEventRequest) -> None:
    _durable_event_logger._emit(AxisEvent(
        run_id=request.run_id, event_type=request.event_type, tool_name=request.tool_name,
        tab_handle=request.tab_handle, detail=request.detail, data=request.data, success=request.success,
    ))


def set_worker_runtime_info(info: WorkerRuntimeInfo) -> None:
    global _worker_runtime_info
    _worker_runtime_info = info


@activity.defn
async def get_worker_runtime_info() -> WorkerRuntimeInfo:
    if _worker_runtime_info is None:
        raise RuntimeError("Worker runtime information is unavailable.")
    return _worker_runtime_info


def get_current_worker_manifest() -> Optional[AxisVersionManifest]:
    """The manifest of the worker process actually running this code right
    now — read in-process (no activity round trip) so a browser-handler
    activity can check it against the manifest the workflow stamped onto
    the deps it scheduled the call with (see
    ``axis.durability.runtime.durable_browser_handler``), even if a
    differently-versioned worker ends up picking the activity up from the
    task queue."""
    return _worker_runtime_info.manifest if _worker_runtime_info is not None else None


class LeaseRequest(BaseModel):
    scope_key: str
    job_id: str
    token: str
    task_queue: str
    duration_seconds: float


class LeaseRenewRequest(BaseModel):
    scope_key: str
    job_id: str
    token: str
    generation: int
    duration_seconds: float


class LeaseReleaseRequest(BaseModel):
    scope_key: str
    job_id: str
    token: str
    generation: int


@activity.defn
async def acquire_browser_lease(request: LeaseRequest) -> LeaseAcquireResult:
    client = activity.client()
    handle = await client.start_workflow(
        BrowserLeaseWorkflow.run, LeaseWorkflowInput(), id=request.scope_key,
        task_queue=request.task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        start_signal="acquire",
        start_signal_args=[_AcquireSignal(
            job_id=request.job_id, token=request.token, duration_seconds=request.duration_seconds,
        )],
    )
    owner = await handle.query(BrowserLeaseWorkflow.get_owner)
    return acquire_result(owner, request.job_id, request.token, time.time())


@activity.defn
async def renew_browser_lease(request: LeaseRenewRequest) -> LeaseOperationResult:
    handle = activity.client().get_workflow_handle(request.scope_key)
    await handle.signal(BrowserLeaseWorkflow.renew, _RenewSignal(
        job_id=request.job_id, token=request.token, generation=request.generation,
        duration_seconds=request.duration_seconds,
    ))
    owner = await handle.query(BrowserLeaseWorkflow.get_owner)
    return fenced_operation_result(
        owner, job_id=request.job_id, token=request.token,
        generation=request.generation, now=time.time(),
    )


@activity.defn
async def release_browser_lease(request: LeaseReleaseRequest) -> LeaseOperationResult:
    handle = activity.client().get_workflow_handle(request.scope_key)
    await handle.signal(BrowserLeaseWorkflow.release, _ReleaseSignal(
        job_id=request.job_id, token=request.token, generation=request.generation,
    ))
    owner = await handle.query(BrowserLeaseWorkflow.get_owner)
    accepted = owner is None
    return LeaseOperationResult(accepted=accepted, owner=owner)


class BrowserRebindRequest(BaseModel):
    deps: AxisDurableDeps
    previous_projection: BrowserRecoveryProjection


@activity.defn
async def refresh_browser_binding(request: BrowserRebindRequest) -> BrowserRebindResult:
    from axis.durability import runtime
    runtime.clear_browser_cache(request.deps.browser_scope_key)
    deps = request.deps.model_copy(deep=True)
    deps.browser_projection = request.previous_projection
    entry, projection = runtime._resolve_browser_entry(deps)
    candidates = runtime._build_rebind_candidates(entry, projection) if projection.rebind_required else []
    return BrowserRebindResult(projection=projection, candidates=candidates)


class BrowserRebindSelectionRequest(BaseModel):
    deps: AxisDurableDeps
    candidate_id: str


@activity.defn
async def select_browser_rebind_candidate(request: BrowserRebindSelectionRequest) -> BrowserRebindResult:
    """Reassociate with a candidate only if it came from the latest trusted
    refresh: candidate ids live only in the browser-tools cache entry the
    offering ``refresh_browser_binding`` call created, and that entry is
    replaced (never merely updated) on every subsequent refresh — so a
    candidate id from an earlier ambiguous refresh is simply absent here and
    safely falls back to requiring a fresh refresh instead of ever binding a
    stale target."""
    from axis.durability import runtime
    deps = request.deps
    cache_key = (deps.browser_scope_key, deps.job_id)
    with runtime._browser_tools_lock:
        entry = runtime._browser_tools_cache.get(cache_key)
    if entry is None:
        return BrowserRebindResult(projection=deps.browser_projection.model_copy(update={"rebind_required": True}))
    local_handle = entry.bindings.get(request.candidate_id)
    if local_handle is None or local_handle not in entry.tools.tab_registry:
        stale_projection = deps.browser_projection.model_copy(update={"rebind_required": True})
        return BrowserRebindResult(projection=stale_projection, candidates=runtime._build_rebind_candidates(entry, stale_projection))
    record = entry.tools.tab_registry[local_handle]
    new_durable_key = hashlib.sha256(f"{deps.job_id}:{request.candidate_id}:selected".encode("utf-8")).hexdigest()[:16]
    entry.bindings[new_durable_key] = local_handle
    resolved = deps.browser_projection.invalidated().model_copy(update={
        "durable_tab_key": new_durable_key,
        "last_known_sanitized_url": entry.tools._sanitize_url_for_model(record.get("url")),
        "last_known_title": record.get("title"),
        "active_tab_intent": bool(record.get("active")),
        "rebind_required": False,
    })
    return BrowserRebindResult(projection=resolved)


def capture_version_manifest(
    *, agent_version: str, browser_contract_version: str, browser_contract_hash: str,
    prompt_version: str, policy_version: str, config_hash: str, result_schema_version: str,
    workflow_schema_version: str,
) -> AxisVersionManifest:
    import importlib.metadata
    return AxisVersionManifest(
        agent_version=agent_version,
        pydantic_ai_version=importlib.metadata.version("pydantic-ai-slim"),
        harness_version=importlib.metadata.version("pydantic-ai-harness"),
        temporal_sdk_version=importlib.metadata.version("temporalio"),
        browser_contract_version=browser_contract_version,
        browser_contract_hash=browser_contract_hash,
        prompt_version=prompt_version,
        policy_version=policy_version,
        config_hash=config_hash,
        result_schema_version=result_schema_version,
        workflow_schema_version=workflow_schema_version,
    )
