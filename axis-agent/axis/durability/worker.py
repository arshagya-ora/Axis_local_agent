"""Temporal worker entry point for the shared AXIS agent architecture."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

# `_resolve_version_manifest` below imports the `scripts/browser_contract.py`
# module by its bare name. Test runs get this for free from
# `tests/conftest.py`; a real worker process needs it added here instead.
AGENT_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = AGENT_DIR / "scripts"
for _path in (AGENT_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from axis.agent import AXIS_SAFETY_INSTRUCTIONS, build_model
from axis.config import AxisConfig, AxisConfigError, load_axis_config
from axis.durability.activities import (
    acquire_browser_lease, capture_version_manifest, configure_durable_event_printer, emit_durable_event,
    get_worker_runtime_info, refresh_browser_binding, release_browser_lease,
    renew_browser_lease, select_browser_rebind_candidate, set_worker_runtime_info,
)
from axis.durability.leases import BrowserLeaseWorkflow
from axis.durability.models import DurableRuntimeSettings, WorkerRuntimeInfo
from axis.durability.runtime import build_durable_agent, set_durable_agent
from axis.durability.workflow import AxisJobWorkflow
from axis.events import RunEventLogger, build_console_event_printer
from axis.models import AxisTaskResult
from provider_config import ProviderConfigError, load_provider_config

_error_logger = logging.getLogger("axis.errors")


def _configure_worker_logging() -> None:
    """A worker process has no interactive CLI trace config to read, and
    unlike `axis.cli` nothing else in this process ever calls
    `logging.basicConfig` — without this, every `logging.getLogger(...)`
    call anywhere in the codebase (including `axis.events`' own raw JSON
    line per event) is silently dropped, which is exactly why a durable
    job's execution has been invisible. Root-level INFO with a plain
    timestamped format is a debugging default, not a production log
    pipeline; adjust via the standard `logging` config if you need less.
    """
    if logging.getLogger().handlers:
        return  # already configured by the embedding process (e.g. a test)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
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


async def run_worker(
    config: Optional[AxisConfig] = None, model: Optional[Any] = None, *, verbose: bool = True,
) -> None:
    if verbose:
        _configure_worker_logging()
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

    # One live printer feeds BOTH the agent-run event logger (tool/model
    # events, emitted directly from inside activities running in this
    # process) and the durable event logger (job/lease/reconciliation/rebind
    # lifecycle events, emitted via the `emit_durable_event` activity) — so
    # every AXIS event this worker produces prints to this terminal, not
    # only a subset. Pass verbose=False to run silent (tests do this).
    printer = build_console_event_printer() if verbose else None
    event_logger = RunEventLogger(printer=printer)
    configure_durable_event_printer(printer)

    agent, durability = build_durable_agent(axis_config, model, event_logger=event_logger)
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
    if verbose:
        print(
            f"[axis.worker] ready — target={axis_config.phase3.temporal.target} "
            f"namespace={axis_config.phase3.temporal.namespace} "
            f"task_queue={axis_config.phase3.temporal.task_queue}"
        )
        print("[axis.worker] watching for jobs; every tool call, model turn, and durable lifecycle event prints below.")
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
