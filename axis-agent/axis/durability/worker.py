"""Temporal worker entry point for the shared AXIS agent architecture."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Iterable, Optional

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from axis.agent import AXIS_SAFETY_INSTRUCTIONS, build_model
from axis.config import AxisConfig, AxisConfigError, load_axis_config
from axis.durability.activities import (
    acquire_browser_lease, capture_version_manifest, emit_durable_event,
    get_worker_runtime_info, refresh_browser_binding, release_browser_lease,
    renew_browser_lease, select_browser_rebind_candidate, set_worker_runtime_info,
)
from axis.durability.leases import BrowserLeaseWorkflow
from axis.durability.models import DurableRuntimeSettings, WorkerRuntimeInfo
from axis.durability.runtime import build_durable_agent, set_durable_agent
from axis.durability.workflow import AxisJobWorkflow
from axis.models import AxisTaskResult
from provider_config import ProviderConfigError, load_provider_config

_error_logger = logging.getLogger("axis.errors")
# Bump on any implementation change to durable behavior (lease lifecycle,
# rebind flow, retry classification, continuation context, version
# enforcement, planning consistency) so a worker running old code is never
# mistaken for one that matches this build — see AxisVersionManifest and
# axis.durability.workflow._load_and_check_worker_runtime.
AGENT_VERSION = "axis-phase3.2"


def _resolve_version_manifest(config: AxisConfig):
    """Build the worker-owned manifest outside replayed workflow code."""
    import browser_contract

    committed = browser_contract.load_committed_snapshot("v2") or {}
    prompt_hash = hashlib.sha256(AXIS_SAFETY_INSTRUCTIONS.encode("utf-8")).hexdigest()[:16]
    policy_hash = hashlib.sha256(json.dumps(
        {"approvals": config.phase2.approvals.as_dict(), "effects": str(config.phase2.effects)},
        sort_keys=True,
    ).encode("utf-8")).hexdigest()[:16]
    config_hash = hashlib.sha256(str(config).encode("utf-8")).hexdigest()[:16]
    result_schema_hash = hashlib.sha256(json.dumps(
        AxisTaskResult.model_json_schema(), sort_keys=True,
    ).encode("utf-8")).hexdigest()[:16]
    return capture_version_manifest(
        agent_version=AGENT_VERSION,
        browser_contract_version=str(committed.get("browserContractVersion", "unknown")),
        browser_contract_hash=str(committed.get("contractHash", "unknown")),
        prompt_version=prompt_hash,
        policy_version=policy_hash,
        config_hash=config_hash,
        result_schema_version=result_schema_hash,
        workflow_schema_version="axis-job-state.v2",
    )


def _runtime_settings(config: AxisConfig) -> DurableRuntimeSettings:
    return DurableRuntimeSettings(
        leases_enabled=config.phase3.leases.enabled,
        lease_conflict_policy=config.phase3.leases.conflict_policy,
        lease_acquire_timeout_seconds=config.phase3.leases.acquire_timeout_seconds,
        lease_duration_seconds=config.phase3.leases.duration_seconds,
        max_run_segments=config.phase3.history.max_run_segments,
        reconcile_unknown_effects=config.phase3.recovery.reconcile_unknown_effects,
    )


def _unique_activities(activities: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for fn in activities:
        definition = getattr(fn, "__temporal_activity_definition", None)
        name = getattr(definition, "name", None) or getattr(fn, "__name__", repr(fn))
        if name in seen:
            raise RuntimeError(f"Duplicate activity registration: {name}")
        seen.add(name)
        result.append(fn)
    return result


def registered_components(durability: Any) -> tuple[list[Any], list[Any]]:
    workflows = [AxisJobWorkflow, BrowserLeaseWorkflow]
    activities = _unique_activities([
        *durability.temporal_activities,
        acquire_browser_lease, renew_browser_lease, release_browser_lease,
        refresh_browser_binding, select_browser_rebind_candidate,
        get_worker_runtime_info, emit_durable_event,
    ])
    return workflows, activities


async def run_worker(config: Optional[AxisConfig] = None, model: Optional[Any] = None) -> None:
    try:
        axis_config = config or load_axis_config()
    except AxisConfigError as exc:
        raise SystemExit(f"axis-agent worker cannot start: {exc}") from exc
    if not axis_config.phase3.temporal.enabled:
        raise SystemExit("axis-agent worker cannot start: phase3.temporal.enabled is false.")

    openai_client = None
    if model is None:
        try:
            provider_config = load_provider_config()
        except ProviderConfigError as exc:
            raise SystemExit(f"axis-agent worker cannot start: {exc}") from exc
        model, openai_client = build_model(provider_config)

    agent, durability = build_durable_agent(axis_config, model)
    set_durable_agent(agent)
    set_worker_runtime_info(WorkerRuntimeInfo(
        manifest=_resolve_version_manifest(axis_config), settings=_runtime_settings(axis_config),
    ))
    workflows, activities = registered_components(durability)
    client = await Client.connect(
        axis_config.phase3.temporal.target,
        namespace=axis_config.phase3.temporal.namespace,
        plugins=[PydanticAIPlugin()],
    )
    worker = Worker(
        client, task_queue=axis_config.phase3.temporal.task_queue,
        workflows=workflows, activities=activities,
    )
    try:
        await worker.run()
    finally:
        if openai_client is not None:
            try:
                await openai_client.close()
            except Exception as exc:  # noqa: BLE001
                _error_logger.error("Error closing the provider client: %s.", type(exc).__name__)


if __name__ == "__main__":
    asyncio.run(run_worker())
