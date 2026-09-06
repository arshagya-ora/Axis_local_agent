"""Expiring fenced browser lease implemented as a tiny Temporal workflow."""
from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel, Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from axis.durability.models import LeaseAcquireResult, LeaseOperationResult, LeaseOwner

LEASE_WORKFLOW_ID_PREFIX = "axis-browser-lease-"


def browser_scope_key(*, bridge_host: str, bridge_port: int) -> str:
    digest = hashlib.sha256(f"{bridge_host}:{bridge_port}".encode("utf-8")).hexdigest()[:24]
    return f"{LEASE_WORKFLOW_ID_PREFIX}{digest}"


class LeaseWorkflowInput(BaseModel):
    generation: int = 0
    operation_count: int = 0


class _AcquireSignal(BaseModel):
    job_id: str
    token: str
    duration_seconds: float = Field(gt=0)


class _RenewSignal(BaseModel):
    job_id: str
    token: str
    generation: int
    duration_seconds: float = Field(gt=0)


class _ReleaseSignal(BaseModel):
    job_id: str
    token: str
    generation: int


@workflow.defn
class BrowserLeaseWorkflow:
    def __init__(self) -> None:
        self._owner: Optional[LeaseOwner] = None
        self._generation = 0
        self._change = 0
        self._operation_count = 0

    def _now(self) -> float:
        return workflow.now().timestamp()

    def _expire(self) -> None:
        if self._owner is not None and self._owner.expires_at <= self._now():
            self._owner = None

    @workflow.run
    async def run(self, state: Optional[LeaseWorkflowInput] = None) -> None:
        if state is not None:
            self._generation = state.generation
            self._operation_count = state.operation_count
        while True:
            self._expire()
            if self._owner is None and self._operation_count >= 500:
                workflow.continue_as_new(LeaseWorkflowInput(generation=self._generation))
            observed = self._change
            timeout = None if self._owner is None else max(0.001, self._owner.expires_at - self._now())
            try:
                await workflow.wait_condition(lambda: self._change != observed, timeout=timeout)
            except TimeoutError:
                self._expire()

    @workflow.signal
    def acquire(self, signal: _AcquireSignal) -> None:
        self._expire()
        self._operation_count += 1
        if self._owner is None:
            self._generation += 1
            self._owner = LeaseOwner(
                job_id=signal.job_id, token=signal.token, generation=self._generation,
                expires_at=self._now() + signal.duration_seconds,
            )
        elif self._owner.job_id == signal.job_id and self._owner.token == signal.token:
            self._owner = self._owner.model_copy(update={"expires_at": self._now() + signal.duration_seconds})
        self._change += 1

    @workflow.signal
    def renew(self, signal: _RenewSignal) -> None:
        self._expire()
        self._operation_count += 1
        if (self._owner is not None and self._owner.job_id == signal.job_id and
                self._owner.token == signal.token and self._owner.generation == signal.generation):
            self._owner = self._owner.model_copy(update={"expires_at": self._now() + signal.duration_seconds})
        self._change += 1

    @workflow.signal
    def release(self, signal: _ReleaseSignal) -> None:
        self._expire()
        self._operation_count += 1
        if (self._owner is not None and self._owner.job_id == signal.job_id and
                self._owner.token == signal.token and self._owner.generation == signal.generation):
            self._owner = None
        self._change += 1

    @workflow.query
    def get_owner(self) -> Optional[LeaseOwner]:
        if self._owner is not None and self._owner.expires_at <= self._now():
            return None
        return self._owner


def acquire_result(
    owner: Optional[LeaseOwner], requesting_job_id: str, token: str, now: Optional[float] = None,
) -> LeaseAcquireResult:
    if owner is not None and (now is None or owner.expires_at > now) and owner.job_id == requesting_job_id and owner.token == token:
        return LeaseAcquireResult(acquired=True, owner=owner)
    return LeaseAcquireResult(acquired=False, owner=owner, reason="busy")


def fenced_operation_result(
    owner: Optional[LeaseOwner], *, job_id: str, token: str, generation: int, now: float,
) -> LeaseOperationResult:
    accepted = bool(owner and owner.expires_at > now and owner.job_id == job_id and
                    owner.token == token and owner.generation == generation)
    return LeaseOperationResult(accepted=accepted, owner=owner)
