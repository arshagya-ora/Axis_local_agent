"""Thin asynchronous adapter over `axis.durability.client`.

This module owns no browser, agent, policy, or workflow logic — it is a
narrow, replaceable boundary between the desktop controller and the
accepted Phase 3 durable-client operations. It never imports or
instantiates the bridge client class, never dispatches a browser tool, and
never builds/rebuilds a Pydantic AI deferred call itself.

`DesktopBackend` is a `Protocol` so the controller can be tested against a
deterministic fake without a live Temporal server; `TemporalDesktopBackend`
is the one production implementation, delegating every operation to
`axis.durability.client`'s existing functions.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, TypeVar

from axis.config import AxisConfig
from axis.durability.models import AxisJobState, RebindCandidate

T = TypeVar("T")

# Full detail (exception type AND message, via logger.exception for a full
# traceback) — deliberately more than the "type only, never the message"
# policy for what ships in a distributed log: this logger only ever writes
# to whatever local terminal launched `axis.desktop`, is never shipped, and
# exists specifically so a real root cause is visible while debugging.
# `DesktopBackendError.message` (bounded, safe) is still the only thing
# that ever reaches the controller/QML.
_logger = logging.getLogger("axis.desktop.backend")


def _logged(op: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Log every call into `TemporalDesktopBackend` and its outcome — the
    single place this happens, so no individual method needs its own
    logging boilerplate. Logs the full exception (via `logger.exception`,
    a real traceback) on failure, *before* the caller sees only the bounded
    `DesktopBackendError` message — this is how you find the real root
    cause of a failing task from the terminal running `axis.desktop`."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            _logger.info("-> %s%r", op, args)
            try:
                result = await fn(self, *args, **kwargs)
            except Exception:
                _logger.exception("<- %s FAILED", op)
                raise
            _logger.info("<- %s OK", op)
            return result

        return wrapper

    return decorator

# Same environment-variable names/defaults the bridge client module reads —
# duplicated here as plain addressing constants only, so this package never
# imports that module (or the class it defines) at all.
_DEFAULT_BRIDGE_HOST = "127.0.0.1"
_DEFAULT_BRIDGE_PORT = 8765


class DesktopBackendError(Exception):
    """A safe, stable-coded backend failure. `message` is always bounded
    and traceback-free; the original exception (if any) is chained only
    for internal logging, never rendered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DesktopBackend(Protocol):
    """The one narrow interface the controller depends on. QML never sees
    this; it exists only so a fake implementation can stand in for tests
    and so a future narrowly-scoped gateway could replace
    `TemporalDesktopBackend` without changing the controller or QML."""

    async def start_job(self, task_summary: str) -> str: ...

    async def get_job_state(self, job_id: str) -> AxisJobState: ...

    async def pause_job(self, job_id: str) -> None: ...

    async def resume_job(self, job_id: str) -> None: ...

    async def cancel_job(self, job_id: str, reason: Optional[str]) -> None: ...

    async def approve_current(self, job_id: str) -> None: ...

    async def deny_current(self, job_id: str) -> None: ...

    async def submit_user_input(self, job_id: str, text: str) -> None: ...

    async def rebind_browser(self, job_id: str, reason: Optional[str]) -> None: ...

    async def get_rebind_candidates(self, job_id: str) -> List[RebindCandidate]: ...

    async def select_rebind_candidate(self, job_id: str, candidate_id: str) -> None: ...

    async def close(self) -> None: ...


class TemporalDesktopBackend:
    """Production `DesktopBackend`. Reuses the accepted Phase 3 Temporal
    connection configuration (`axis.yaml`'s `phase3.temporal`) — the same
    endpoint/namespace/task queue the CLI and worker already use — and the
    same narrow application-level operations as the CLI
    (`axis.durability.client`). Never constructs its own approval/denial
    protocol, never touches lease/fencing/binding-generation state
    directly, and never queries raw Temporal workflow history."""

    def __init__(self, config: AxisConfig) -> None:
        self._config = config
        self._client: Any = None
        self._handles: Dict[str, Any] = {}
        self._bridge_host = os.environ.get("BROWSER_AGENT_BRIDGE_HOST", _DEFAULT_BRIDGE_HOST)
        self._bridge_port = int(os.environ.get("BROWSER_AGENT_BRIDGE_PORT", str(_DEFAULT_BRIDGE_PORT)))

    async def _ensure_client(self) -> Any:
        if self._client is None:
            from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
            from temporalio.client import Client

            temporal = self._config.phase3.temporal
            try:
                self._client = await Client.connect(
                    temporal.target, namespace=temporal.namespace,
                    # Same plugin the worker registers (axis/durability/worker.py)
                    # — gives this client the Pydantic-aware data converter so
                    # AxisJobState and friends round-trip without a runtime warning.
                    plugins=[PydanticAIPlugin()],
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Could not connect to Temporal at %s (namespace=%s)", temporal.target, temporal.namespace)
                raise DesktopBackendError("TEMPORAL_UNAVAILABLE", "AXIS could not reach the Temporal server.") from exc
            _logger.info("Connected to Temporal at %s (namespace=%s)", temporal.target, temporal.namespace)
        return self._client

    async def _handle_for(self, job_id: str) -> Any:
        handle = self._handles.get(job_id)
        if handle is not None:
            return handle
        from axis.durability.client import get_job_handle

        client = await self._ensure_client()
        try:
            handle = get_job_handle(client, job_id)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Could not resolve a workflow handle for job_id=%s", job_id)
            raise DesktopBackendError("JOB_NOT_FOUND", "AXIS could not find that job.") from exc
        self._handles[job_id] = handle
        return handle

    @_logged("start_job")
    async def start_job(self, task_summary: str) -> str:
        from axis.durability.client import start_job

        client = await self._ensure_client()
        temporal = self._config.phase3.temporal
        try:
            handle = await start_job(
                client, task_queue=temporal.task_queue, task_summary=task_summary,
                bridge_host=self._bridge_host, bridge_port=self._bridge_port,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("start_job: Temporal rejected the start request")
            raise DesktopBackendError("JOB_START_FAILED", "AXIS could not start the task.") from exc
        self._handles[handle.id] = handle
        _logger.info("start_job: job_id=%s task_queue=%s", handle.id, temporal.task_queue)
        return handle.id

    @_logged("get_job_state")
    async def get_job_state(self, job_id: str) -> AxisJobState:
        from axis.durability.client import get_job_state

        handle = await self._handle_for(job_id)
        try:
            state = await get_job_state(handle)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("get_job_state: query failed for job_id=%s", job_id)
            raise DesktopBackendError("JOB_QUERY_FAILED", "AXIS could not read the job's current state.") from exc
        _logger.info(
            "get_job_state: job_id=%s status=%s pending_approval=%s pending_question=%s rebind_required=%s error_code=%s",
            job_id, state.status, state.pending_approval is not None, state.pending_question is not None,
            state.rebind_required, state.last_error_code,
        )
        return state

    @_logged("pause_job")
    async def pause_job(self, job_id: str) -> None:
        from axis.durability.client import pause_job

        handle = await self._handle_for(job_id)
        await self._signal("JOB_SIGNAL_FAILED", pause_job(handle), op="pause_job", job_id=job_id)

    @_logged("resume_job")
    async def resume_job(self, job_id: str) -> None:
        from axis.durability.client import resume_job

        handle = await self._handle_for(job_id)
        await self._signal("JOB_SIGNAL_FAILED", resume_job(handle), op="resume_job", job_id=job_id)

    @_logged("cancel_job")
    async def cancel_job(self, job_id: str, reason: Optional[str]) -> None:
        from axis.durability.client import cancel_job

        handle = await self._handle_for(job_id)
        await self._signal("JOB_SIGNAL_FAILED", cancel_job(handle, reason=reason), op="cancel_job", job_id=job_id)

    @_logged("approve_current")
    async def approve_current(self, job_id: str) -> None:
        from axis.durability.client import decide_current_approval

        handle = await self._handle_for(job_id)
        await self._signal(
            "APPROVAL_SUBMISSION_FAILED", decide_current_approval(handle, approved=True),
            op="approve_current", job_id=job_id,
        )

    @_logged("deny_current")
    async def deny_current(self, job_id: str) -> None:
        from axis.durability.client import decide_current_approval

        handle = await self._handle_for(job_id)
        await self._signal(
            "APPROVAL_SUBMISSION_FAILED", decide_current_approval(handle, approved=False),
            op="deny_current", job_id=job_id,
        )

    @_logged("submit_user_input")
    async def submit_user_input(self, job_id: str, text: str) -> None:
        from axis.durability.client import submit_user_input

        handle = await self._handle_for(job_id)
        await self._signal(
            "USER_INPUT_SUBMISSION_FAILED", submit_user_input(handle, text=text),
            op="submit_user_input", job_id=job_id,
        )

    @_logged("rebind_browser")
    async def rebind_browser(self, job_id: str, reason: Optional[str]) -> None:
        from axis.durability.client import rebind_browser

        handle = await self._handle_for(job_id)
        await self._signal("REBIND_FAILED", rebind_browser(handle, reason=reason), op="rebind_browser", job_id=job_id)

    @_logged("get_rebind_candidates")
    async def get_rebind_candidates(self, job_id: str) -> List[RebindCandidate]:
        from axis.durability.client import get_rebind_candidates

        handle = await self._handle_for(job_id)
        try:
            return await get_rebind_candidates(handle)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("get_rebind_candidates: query failed for job_id=%s", job_id)
            raise DesktopBackendError("JOB_QUERY_FAILED", "AXIS could not read the browser rebind candidates.") from exc

    @_logged("select_rebind_candidate")
    async def select_rebind_candidate(self, job_id: str, candidate_id: str) -> None:
        from axis.durability.client import select_rebind_candidate

        handle = await self._handle_for(job_id)
        await self._signal(
            "REBIND_FAILED", select_rebind_candidate(handle, candidate_id=candidate_id),
            op="select_rebind_candidate", job_id=job_id,
        )

    async def _signal(self, code: str, coro: Awaitable[Any], *, op: str, job_id: str) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            _logger.exception("%s: signal delivery failed for job_id=%s", op, job_id)
            raise DesktopBackendError(code, "AXIS could not deliver that command to the job.") from exc

    async def close(self) -> None:
        self._handles.clear()
        self._client = None


class BackgroundLoop:
    """One dedicated background thread running one asyncio event loop.

    The Qt UI thread must never block on Temporal/provider/browser/
    filesystem I/O, and PySide6 6.8's asyncio integration is not something
    this pin verifies as production-suitable for a long-lived Temporal
    client, so this is the one controlled concurrency mechanism: every
    backend coroutine is scheduled here via `run_coroutine_threadsafe` and
    its result/exception is delivered back to the caller's own thread
    through a plain `concurrent.futures.Future`. Nothing here spins up a
    thread or an event loop per call."""

    def __init__(self) -> None:
        self._loop: Any = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="axis-desktop-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def submit(self, coro_factory: Callable[[], Awaitable[T]]) -> "Future[T]":
        import asyncio

        if self._loop is None:
            raise DesktopBackendError("DESKTOP_INTERNAL_ERROR", "The background execution context is not ready.")
        return asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)

    def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
