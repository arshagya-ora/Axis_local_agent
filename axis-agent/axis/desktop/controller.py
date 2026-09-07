"""The one `QObject` exposed to QML.

Owns UI state, command validation, asynchronous request scheduling (via a
single background asyncio loop — see `axis.desktop.backend.BackgroundLoop`)
and state projection (via `axis.desktop.presentation`). Contains no
Temporal, browser, or agent business logic itself — every durable
operation is delegated to a `DesktopBackend`.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (
    Property, QAbstractListModel, QModelIndex, QObject, QTimer, Qt, Signal, Slot,
)

from axis.config import AxisConfig
from axis.desktop.backend import DesktopBackend, DesktopBackendError
from axis.desktop.presentation import (
    DesktopJobSnapshot, diff_timeline_entries, project_job_state, status_presentation,
)
from axis.desktop.sessions import SessionStore
from axis.durability.models import AxisJobState

_MAX_TASK_SUMMARY_CHARS = 500
_MAX_ANSWER_CHARS = 2000
_MAX_TIMELINE_DEFAULT = 300
_MAX_SESSION_JOBS_DEFAULT = 20

# Every slot call, scheduled backend operation, and async result is logged
# here — local-terminal-only visibility into the UI's own side of a task,
# to sit alongside axis.desktop.backend's logging and the worker's own
# (axis.durability.worker) tool/model/durability event printing.
_logger = logging.getLogger("axis.desktop.controller")


class ListModel(QAbstractListModel):
    """One small, generic, reusable `QAbstractListModel` used for every
    bounded list the controller exposes (plan/effects/acceptance/timeline/
    session jobs/rebind candidates) — role names are declared once per
    instance rather than hand-writing six near-identical model classes."""

    def __init__(self, roles: List[str], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._role_names = {Qt.ItemDataRole.UserRole + i + 1: name for i, name in enumerate(roles)}
        self._items: List[Dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        name = self._role_names.get(role)
        if name is None:
            return None
        return self._items[index.row()].get(name)

    def roleNames(self) -> Dict[int, bytes]:  # noqa: N802
        return {role: name.encode("utf-8") for role, name in self._role_names.items()}

    def set_items(self, items: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def clear(self) -> None:
        self.set_items([])

    def items(self) -> List[Dict[str, Any]]:
        return list(self._items)

    def append_bounded(self, item: Dict[str, Any], max_items: int) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self.endInsertRows()
        overflow = len(self._items) - max_items
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._items[:overflow]
            self.endRemoveRows()


class AxisDesktopController(QObject):
    connectionStateChanged = Signal()
    currentJobStatusChanged = Signal()
    taskSummaryChanged = Signal()
    isBusyChanged = Signal()
    commandFlagsChanged = Signal()
    pendingApprovalChanged = Signal()
    pendingQuestionChanged = Signal()
    rebindChanged = Signal()
    resultChanged = Signal()
    safeErrorChanged = Signal()
    currentJobIdChanged = Signal()
    themeChanged = Signal()
    animationsEnabledChanged = Signal()
    sessionChanged = Signal()
    chatChanged = Signal()

    # Internal cross-thread delivery signal: (generation, op, payload-or-error).
    _asyncCompleted = Signal(int, str, object)

    def __init__(
        self, backend: DesktopBackend, config: AxisConfig, background_loop: Any,
        parent: Optional[QObject] = None, *, session_store: Optional[SessionStore] = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._loop = background_loop
        # In-memory by default so tests and smoke mode never touch disk;
        # the real app passes a disk-backed store (see axis.desktop.app).
        self._store = session_store if session_store is not None else SessionStore()
        self._session_id: Optional[str] = None
        desktop_cfg = config.phase4.desktop
        self._poll_interval_ms = desktop_cfg.poll_interval_ms
        self._reconnect_initial_delay_ms = desktop_cfg.reconnect_initial_delay_ms
        self._reconnect_max_delay_ms = desktop_cfg.reconnect_max_delay_ms
        self._max_timeline_items = desktop_cfg.max_timeline_items
        self._max_session_jobs = desktop_cfg.max_session_jobs
        self._theme = desktop_cfg.theme
        self._animations_enabled = desktop_cfg.animations_enabled

        self._generation = 0
        self._job_id: Optional[str] = None
        self._raw_state: Optional[AxisJobState] = None
        self._snapshot: Optional[DesktopJobSnapshot] = None
        self._query_in_flight = False
        self._command_in_flight = False
        self._start_in_flight = False
        self._connection_state = "disconnected"
        self._connection_message = ""
        self._safe_error_code: Optional[str] = None
        self._safe_error_message = ""
        self._reconnect_delay_ms = self._reconnect_initial_delay_ms
        self._task_summary_pending = ""

        self._plan_model = ListModel(["content", "status", "active"])
        self._effect_model = ListModel(["summary", "risk", "status", "mutationLabel"])
        self._acceptance_model = ListModel(["description", "state"])
        self._timeline_model = ListModel(["kind", "text"])
        self._session_job_model = ListModel(["jobId", "label", "statusLabel", "statusKind", "timeLabel", "active"])
        # The chat thread: accumulates across every job in the current
        # session (user prompts, observed events, results) and is the one
        # thing `_begin_tracking` deliberately does NOT reset.
        self._chat_model = ListModel(["kind", "text", "meta"])
        # One row per chat *session* (not per job) for the sidebar.
        self._session_list_model = ListModel(["sessionId", "title", "statusLabel", "timeLabel", "active"])
        self._rebind_candidate_model = ListModel(["label", "sanitizedUrl", "title", "active"])
        self._rebind_candidate_ids: List[str] = []

        # Session-local recall of each job's chat title and last-known
        # status/time, keyed by job_id — the durable job itself carries no
        # "recent tasks" list, so this is purely a desktop-session
        # projection built from what this window has actually observed
        # (never fabricated for jobs never seen this session).
        self._session_job_order: List[str] = []
        self._session_job_meta: Dict[str, Dict[str, Any]] = {}

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_interval_ms)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        self._asyncCompleted.connect(self._on_async_completed, Qt.ConnectionType.QueuedConnection)
        self._refresh_session_list()

    # -- internal scheduling -------------------------------------------------

    def _schedule(self, op: str, coro_factory: Any) -> None:
        generation = self._generation
        _logger.info("scheduling %s (generation=%d, job_id=%s)", op, generation, self._job_id)

        def _done(future: Any) -> None:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                _logger.exception("%s (generation=%d) raised", op, generation)
                result = exc
            self._asyncCompleted.emit(generation, op, result)

        future = self._loop.submit(coro_factory)
        future.add_done_callback(_done)

    def _on_async_completed(self, generation: int, op: str, payload: Any) -> None:
        if generation != self._generation:
            _logger.info(
                "ignoring stale %s result from generation=%d (current generation=%d)", op, generation, self._generation,
            )
            return  # A newer job/reconnect/clear superseded this request.
        is_error = isinstance(payload, BaseException)
        if is_error:
            _logger.warning("%s failed: %s: %s", op, type(payload).__name__, payload)
        else:
            _logger.info("%s completed", op)
        if op == "start_job":
            self._start_in_flight = False
            self.isBusyChanged.emit()
            if is_error:
                self._set_safe_error(payload)
                return
            label = self._task_summary_pending
            self._task_summary_pending = ""
            self.taskSummaryChanged.emit()
            job_id = str(payload)
            if self._session_id is not None:
                self._store.add_job(self._session_id, job_id)
                self._refresh_session_list()
            self._begin_tracking(job_id, label=label)
        elif op == "query":
            self._query_in_flight = False
            if is_error:
                self._set_safe_error(payload)
                if self._safe_error_code == "JOB_NOT_FOUND":
                    # Retrying cannot help — the job genuinely does not
                    # exist (or is not reachable under this identity), so
                    # this must surface as a controlled error, not spin
                    # forever in the connection-lost backoff loop.
                    self._poll_timer.stop()
                    return
                self._set_connection_state("disconnected", "AXIS lost the connection to Temporal.")
                self._schedule_backoff_retry()
                return
            self._set_connection_state("connected", "")
            self._reconnect_delay_ms = self._reconnect_initial_delay_ms
            self._apply_state(payload)
        elif op == "command":
            self._command_in_flight = False
            self.isBusyChanged.emit()
            self.commandFlagsChanged.emit()
            if is_error:
                self._set_safe_error(payload)
                return
            self._trigger_refresh()
        elif op == "candidates":
            if is_error:
                self._set_safe_error(payload)
                return
            self._apply_candidates(payload)

    def _set_safe_error(self, exc: BaseException) -> None:
        if isinstance(exc, DesktopBackendError):
            self._safe_error_code = exc.code
            self._safe_error_message = exc.message
        else:
            self._safe_error_code = "DESKTOP_INTERNAL_ERROR"
            self._safe_error_message = "AXIS could not complete the desktop operation."
        _logger.info("safe error surfaced to UI: code=%s message=%r", self._safe_error_code, self._safe_error_message)
        self.safeErrorChanged.emit()

    def _set_connection_state(self, state: str, message: str) -> None:
        if state == self._connection_state and message == self._connection_message:
            return
        _logger.info("connection state: %s -> %s (%s)", self._connection_state, state, message)
        self._connection_state = state
        self._connection_message = message
        self.connectionStateChanged.emit()

    # -- state application ----------------------------------------------------

    def _begin_tracking(self, job_id: str, label: Optional[str] = None) -> None:
        self._job_id = job_id
        self._raw_state = None
        self._snapshot = None
        # A stale in-flight query belongs to a superseded generation and
        # will be ignored on arrival (see `_on_async_completed`) — it must
        # not block this newly selected job's own first query. The same
        # applies to a stale in-flight *command*: its completion is dropped
        # by the generation guard before `_command_in_flight` would be
        # cleared, so it must be reset here or every later pause/resume/
        # cancel/approve would be silently swallowed forever.
        self._query_in_flight = False
        self._command_in_flight = False
        # Per-job models reset; the session-scoped chat thread deliberately
        # survives so history spans every job in the session.
        self._timeline_model.clear()
        self._rebind_candidate_model.clear()
        self._rebind_candidate_ids = []
        self._upsert_session_job(job_id, label=label)
        self.currentJobIdChanged.emit()
        self.isBusyChanged.emit()
        self.commandFlagsChanged.emit()
        self._set_connection_state("connecting", "")
        self._trigger_refresh()
        self._poll_timer.start()

    # -- session management ----------------------------------------------------

    def _refresh_session_list(self) -> None:
        sessions = list(reversed(self._store.list_sessions()))  # newest first
        self._session_list_model.set_items([
            {
                "sessionId": s.session_id, "title": s.title, "statusLabel": s.last_status,
                "timeLabel": time.strftime("%H:%M", time.localtime(s.updated_at)),
                "active": s.session_id == self._session_id,
            }
            for s in sessions
        ])
        self.sessionChanged.emit()

    def _load_session_chat(self, session_id: str) -> None:
        session = self._store.get(session_id)
        items = []
        if session is not None:
            items = [
                {"kind": str(m.get("kind", "event")), "text": str(m.get("text", "")), "meta": str(m.get("meta", ""))}
                for m in session.messages
            ]
        self._chat_model.set_items(items[-self._max_timeline_items:])
        self.chatChanged.emit()

    def _append_chat(self, kind: str, text: str, meta: str = "") -> None:
        self._chat_model.append_bounded({"kind": kind, "text": text, "meta": meta}, self._max_timeline_items)
        if self._session_id is not None:
            self._store.append_message(self._session_id, kind, text, meta)
        self.chatChanged.emit()

    def _clear_job_state(self) -> None:
        """Detach from any current job without touching session chat."""
        self._generation += 1
        self._poll_timer.stop()
        self._job_id = None
        self._raw_state = None
        self._snapshot = None
        self._query_in_flight = False
        self._command_in_flight = False
        self._timeline_model.clear()
        self._plan_model.clear()
        self._effect_model.clear()
        self._acceptance_model.clear()
        self._rebind_candidate_model.clear()
        self._rebind_candidate_ids = []
        self.currentJobIdChanged.emit()
        self.currentJobStatusChanged.emit()
        self.taskSummaryChanged.emit()
        self.isBusyChanged.emit()
        self.commandFlagsChanged.emit()
        self.pendingApprovalChanged.emit()
        self.pendingQuestionChanged.emit()
        self.rebindChanged.emit()
        self.resultChanged.emit()

    def _now_label(self) -> str:
        return time.strftime("%H:%M")

    def _upsert_session_job(
        self, job_id: str, *, label: Optional[str] = None,
        status_label: Optional[str] = None, status_kind: Optional[str] = None,
    ) -> None:
        meta = self._session_job_meta.get(job_id)
        if meta is None:
            meta = {
                "label": label or job_id, "statusLabel": status_label or "",
                "statusKind": status_kind or "neutral", "timeLabel": self._now_label(),
            }
            self._session_job_meta[job_id] = meta
            self._session_job_order.append(job_id)
            if len(self._session_job_order) > self._max_session_jobs:
                oldest = self._session_job_order.pop(0)
                del self._session_job_meta[oldest]
        else:
            if label:
                meta["label"] = label
            if status_label is not None:
                meta["statusLabel"] = status_label
            if status_kind is not None:
                meta["statusKind"] = status_kind
            self._session_job_order.remove(job_id)
            self._session_job_order.append(job_id)
        self._session_job_model.set_items([
            {
                "jobId": jid, "label": self._session_job_meta[jid]["label"],
                "statusLabel": self._session_job_meta[jid]["statusLabel"],
                "statusKind": self._session_job_meta[jid]["statusKind"],
                "timeLabel": self._session_job_meta[jid]["timeLabel"],
                "active": jid == self._job_id,
            }
            for jid in self._session_job_order
        ])

    def _trigger_refresh(self) -> None:
        if self._job_id is None or self._query_in_flight:
            return
        self._query_in_flight = True
        job_id = self._job_id
        self._schedule("query", lambda: self._backend.get_job_state(job_id))

    def _schedule_backoff_retry(self) -> None:
        delay = min(self._reconnect_delay_ms, self._reconnect_max_delay_ms)
        jitter = delay * random.uniform(0, 0.25)
        self._reconnect_delay_ms = min(self._reconnect_delay_ms * 2, self._reconnect_max_delay_ms)
        QTimer.singleShot(int(delay + jitter), self._trigger_refresh)

    def _apply_state(self, state: AxisJobState) -> None:
        self._raw_state = state
        previous = self._snapshot
        snapshot = project_job_state(state)
        self._snapshot = snapshot
        if previous is None or previous.status_code != snapshot.status_code:
            _logger.info(
                "job %s status: %s -> %s (error_code=%s)",
                self._job_id, previous.status_code if previous else "(none)", snapshot.status_code, snapshot.error_code,
            )
        for entry in diff_timeline_entries(previous, snapshot):
            _logger.info("timeline: [%s] %s", entry.kind, entry.text)
            self._timeline_model.append_bounded(
                {"kind": entry.kind, "text": entry.text}, self._max_timeline_items,
            )
            # First observation (previous None) only restates the current
            # status — appending it to the persistent chat on every
            # session reopen would duplicate history, so only genuine
            # transitions become chat log lines.
            if previous is not None:
                self._append_chat(entry.kind, entry.text)
        if snapshot.result is not None and (previous is None or previous.result is None):
            # Record the result exactly once per job. The transition guard
            # (previous had no result) covers the live case; for a first
            # observation that is already terminal (job finished between
            # start and the first query, or a session reopened after the
            # app missed the live transition) the store's last message
            # decides — if it already ends with this result, skip.
            if previous is not None or self._session_id is None \
                    or self._store.last_message_kind(self._session_id) != "result":
                self._append_chat("result", snapshot.result.summary, snapshot.result.status)
        if self._session_id is not None:
            self._store.set_last_status(self._session_id, snapshot.display_status)
            self._refresh_session_list()
        self._plan_model.set_items([
            {"content": p.content, "status": p.status, "active": p.active} for p in snapshot.plan_items
        ])
        self._effect_model.set_items([
            {"summary": e.summary, "risk": e.risk, "status": e.status,
             "mutationLabel": f"{e.mutation_count}/{e.max_mutations}" if e.max_mutations else ""}
            for e in snapshot.effects
        ])
        self._acceptance_model.set_items([
            {"description": a.description, "state": a.state} for a in snapshot.acceptance
        ])
        existing_label = self._session_job_meta.get(self._job_id, {}).get("label")
        label_hint = None
        if snapshot.task_summary and (not existing_label or existing_label == self._job_id):
            label_hint = snapshot.task_summary
        _, status_kind, _ = status_presentation(snapshot.status_code)
        self._upsert_session_job(
            self._job_id, label=label_hint, status_label=snapshot.display_status, status_kind=status_kind,
        )
        if snapshot.rebind_required and state.rebind_candidates:
            # Always resynced from the latest authoritative snapshot — a
            # stale candidate id from a prior refresh must never remain
            # selectable once binding generation moves on.
            self._apply_candidates(state.rebind_candidates)
        else:
            self._rebind_candidate_model.clear()
            self._rebind_candidate_ids = []
        self.currentJobStatusChanged.emit()
        self.commandFlagsChanged.emit()
        self.pendingApprovalChanged.emit()
        self.pendingQuestionChanged.emit()
        self.rebindChanged.emit()
        self.resultChanged.emit()
        if snapshot.is_terminal:
            self._poll_timer.stop()

    def _apply_candidates(self, candidates: List[Any]) -> None:
        from axis.desktop.presentation import DesktopRebindCandidate, _bound

        self._rebind_candidate_ids = [c.candidate_id for c in candidates]
        projected = [
            DesktopRebindCandidate(
                label=f"Tab {i}", sanitized_url=_bound(c.sanitized_url), title=_bound(c.title, 160),
                active=c.active,
            )
            for i, c in enumerate(candidates, start=1)
        ]
        self._rebind_candidate_model.set_items([
            {"label": c.label, "sanitizedUrl": c.sanitized_url, "title": c.title, "active": c.active}
            for c in projected
        ])
        self.rebindChanged.emit()

    def _on_poll_tick(self) -> None:
        self._trigger_refresh()

    # -- QML-facing properties --------------------------------------------------

    def _get_connection_state(self) -> str:
        return self._connection_state

    connectionState = Property(str, _get_connection_state, notify=connectionStateChanged)

    def _get_connection_message(self) -> str:
        return self._connection_message

    connectionMessage = Property(str, _get_connection_message, notify=connectionStateChanged)

    def _get_current_job_status(self) -> str:
        return self._snapshot.status_code if self._snapshot else ""

    currentJobStatus = Property(str, _get_current_job_status, notify=currentJobStatusChanged)

    def _get_current_job_status_label(self) -> str:
        return self._snapshot.display_status if self._snapshot else ""

    currentJobStatusLabel = Property(str, _get_current_job_status_label, notify=currentJobStatusChanged)

    def _get_current_job_status_kind(self) -> str:
        if self._snapshot is None:
            return "neutral"
        _, kind, _ = status_presentation(self._snapshot.status_code)
        return kind

    currentJobStatusKind = Property(str, _get_current_job_status_kind, notify=currentJobStatusChanged)

    def _get_current_job_id(self) -> str:
        return self._job_id or ""

    currentJobId = Property(str, _get_current_job_id, notify=currentJobIdChanged)

    def _get_task_summary(self) -> str:
        return self._task_summary_pending if self._job_id is None else (self._snapshot.task_summary if self._snapshot else "")

    taskSummary = Property(str, _get_task_summary, notify=taskSummaryChanged)

    def _get_is_busy(self) -> bool:
        return self._start_in_flight or self._command_in_flight

    isBusy = Property(bool, _get_is_busy, notify=isBusyChanged)

    def _get_can_start(self) -> bool:
        return not self._start_in_flight and (self._job_id is None or (self._snapshot is not None and self._snapshot.is_terminal))

    canStart = Property(bool, _get_can_start, notify=commandFlagsChanged)

    def _get_can_pause(self) -> bool:
        return bool(self._snapshot and self._snapshot.can_pause) and not self._command_in_flight

    canPause = Property(bool, _get_can_pause, notify=commandFlagsChanged)

    def _get_pause_pending(self) -> bool:
        return bool(self._snapshot and self._snapshot.pause_pending)

    pausePending = Property(bool, _get_pause_pending, notify=currentJobStatusChanged)

    def _get_can_resume(self) -> bool:
        return bool(self._snapshot and self._snapshot.can_resume) and not self._command_in_flight

    canResume = Property(bool, _get_can_resume, notify=commandFlagsChanged)

    def _get_can_cancel(self) -> bool:
        return bool(self._snapshot and self._snapshot.can_cancel) and not self._command_in_flight

    canCancel = Property(bool, _get_can_cancel, notify=commandFlagsChanged)

    def _get_has_pending_approval(self) -> bool:
        return bool(self._snapshot and self._snapshot.pending_approval is not None)

    hasPendingApproval = Property(bool, _get_has_pending_approval, notify=pendingApprovalChanged)

    def _get_approval_summary(self) -> str:
        return self._snapshot.pending_approval.summary if self._get_has_pending_approval() else ""

    approvalSummary = Property(str, _get_approval_summary, notify=pendingApprovalChanged)

    def _get_approval_risk(self) -> str:
        return self._snapshot.pending_approval.risk if self._get_has_pending_approval() else ""

    approvalRisk = Property(str, _get_approval_risk, notify=pendingApprovalChanged)

    def _get_approval_reason(self) -> str:
        return self._snapshot.pending_approval.reason if self._get_has_pending_approval() else ""

    approvalReason = Property(str, _get_approval_reason, notify=pendingApprovalChanged)

    def _get_has_pending_question(self) -> bool:
        return bool(self._snapshot and self._snapshot.pending_question is not None)

    hasPendingQuestion = Property(bool, _get_has_pending_question, notify=pendingQuestionChanged)

    def _get_question_text(self) -> str:
        return self._snapshot.pending_question.question if self._get_has_pending_question() else ""

    questionText = Property(str, _get_question_text, notify=pendingQuestionChanged)

    def _get_rebind_required(self) -> bool:
        return bool(self._snapshot and self._snapshot.rebind_required)

    rebindRequired = Property(bool, _get_rebind_required, notify=rebindChanged)

    def _get_has_result(self) -> bool:
        return bool(self._snapshot and self._snapshot.result is not None)

    hasResult = Property(bool, _get_has_result, notify=resultChanged)

    def _get_result_status(self) -> str:
        return self._snapshot.result.status if self._get_has_result() else ""

    resultStatus = Property(str, _get_result_status, notify=resultChanged)

    def _get_result_summary(self) -> str:
        return self._snapshot.result.summary if self._get_has_result() else ""

    resultSummary = Property(str, _get_result_summary, notify=resultChanged)

    def _get_safe_error_code(self) -> str:
        return self._safe_error_code or ""

    safeErrorCode = Property(str, _get_safe_error_code, notify=safeErrorChanged)

    def _get_safe_error_message(self) -> str:
        return self._safe_error_message

    safeErrorMessage = Property(str, _get_safe_error_message, notify=safeErrorChanged)

    def _get_theme(self) -> str:
        return self._theme

    def _set_theme_prop(self, value: str) -> None:
        self.setTheme(value)

    theme = Property(str, _get_theme, _set_theme_prop, notify=themeChanged)

    def _get_animations_enabled(self) -> bool:
        return self._animations_enabled

    animationsEnabled = Property(bool, _get_animations_enabled, notify=animationsEnabledChanged)

    planModel = Property(QObject, lambda self: self._plan_model, constant=True)
    effectModel = Property(QObject, lambda self: self._effect_model, constant=True)
    acceptanceModel = Property(QObject, lambda self: self._acceptance_model, constant=True)
    timelineModel = Property(QObject, lambda self: self._timeline_model, constant=True)
    sessionJobModel = Property(QObject, lambda self: self._session_job_model, constant=True)
    chatModel = Property(QObject, lambda self: self._chat_model, constant=True)
    sessionListModel = Property(QObject, lambda self: self._session_list_model, constant=True)
    rebindCandidateModel = Property(QObject, lambda self: self._rebind_candidate_model, constant=True)

    def _get_current_session_id(self) -> str:
        return self._session_id or ""

    currentSessionId = Property(str, _get_current_session_id, notify=sessionChanged)

    def _get_session_title(self) -> str:
        if self._session_id is None:
            return ""
        session = self._store.get(self._session_id)
        return session.title if session is not None else ""

    sessionTitle = Property(str, _get_session_title, notify=sessionChanged)

    def _get_chat_count(self) -> int:
        return self._chat_model.rowCount()

    chatCount = Property(int, _get_chat_count, notify=chatChanged)

    def _get_plan_count(self) -> int:
        return self._plan_model.rowCount()

    # Emitted with every applied snapshot (plan items only change there).
    planCount = Property(int, _get_plan_count, notify=currentJobStatusChanged)

    def _get_rebind_candidate_count(self) -> int:
        return self._rebind_candidate_model.rowCount()

    rebindCandidateCount = Property(int, _get_rebind_candidate_count, notify=rebindChanged)

    # -- QML-facing slots --------------------------------------------------

    @Slot(str)
    def startTask(self, text: str) -> None:
        text = (text or "").strip()
        _logger.info("startTask(len=%d)", len(text))
        if not text:
            _logger.info("startTask rejected: empty")
            return
        if len(text) > _MAX_TASK_SUMMARY_CHARS:
            _logger.info("startTask rejected: %d chars > max %d", len(text), _MAX_TASK_SUMMARY_CHARS)
            self._safe_error_code = "JOB_START_FAILED"
            self._safe_error_message = f"The task must be {_MAX_TASK_SUMMARY_CHARS} characters or fewer."
            self.safeErrorChanged.emit()
            return
        if not self._get_can_start():
            _logger.info("startTask rejected: an active nonterminal job already exists (job_id=%s)", self._job_id)
            return  # An active nonterminal job exists; the user must act on it explicitly.
        if self._session_id is None or self._store.get(self._session_id) is None:
            session = self._store.create_session(text)
            self._session_id = session.session_id
        self._append_chat("user", text)
        self._refresh_session_list()
        self._start_in_flight = True
        self._task_summary_pending = text
        self.isBusyChanged.emit()
        self.taskSummaryChanged.emit()
        self._schedule("start_job", lambda: self._backend.start_job(text))

    @Slot(str)
    def reconnectJob(self, job_id: str) -> None:
        job_id = (job_id or "").strip()
        _logger.info("reconnectJob(%r)", job_id)
        if not job_id:
            return
        session = self._store.find_by_job(job_id)
        if session is None:
            session = self._store.create_session(job_id)
            self._store.add_job(session.session_id, job_id)
        self._session_id = session.session_id
        self._load_session_chat(session.session_id)
        self._refresh_session_list()
        self._generation += 1
        self._poll_timer.stop()
        self._begin_tracking(job_id)

    @Slot()
    def newSession(self) -> None:
        """Start a fresh conversation. The session itself is created
        lazily on the first task so empty sessions never accumulate."""
        _logger.info("newSession()")
        self._session_id = None
        self._chat_model.clear()
        self.chatChanged.emit()
        self._clear_job_state()
        self._refresh_session_list()

    @Slot(str)
    def openSession(self, session_id: str) -> None:
        session = self._store.get((session_id or "").strip())
        _logger.info("openSession(%r) found=%s", session_id, session is not None)
        if session is None:
            return
        self._session_id = session.session_id
        self._load_session_chat(session.session_id)
        if session.job_ids:
            self._generation += 1
            self._poll_timer.stop()
            self._begin_tracking(session.job_ids[-1])
        else:
            self._clear_job_state()
        self._refresh_session_list()

    @Slot()
    def refreshCurrentJob(self) -> None:
        _logger.info("refreshCurrentJob() job_id=%s", self._job_id)
        self._trigger_refresh()

    @Slot()
    def pauseCurrentJob(self) -> None:
        _logger.info("pauseCurrentJob() job_id=%s", self._job_id)
        self._run_command(lambda: self._backend.pause_job(self._job_id))

    @Slot()
    def resumeCurrentJob(self) -> None:
        _logger.info("resumeCurrentJob() job_id=%s", self._job_id)
        self._run_command(lambda: self._backend.resume_job(self._job_id))

    @Slot(str)
    def requestCancel(self, reason: str) -> None:
        reason = (reason or "").strip()[:300] or None
        _logger.info("requestCancel(reason=%r) job_id=%s", reason, self._job_id)
        self._run_command(lambda: self._backend.cancel_job(self._job_id, reason))

    @Slot()
    def approveCurrentEffect(self) -> None:
        _logger.info("approveCurrentEffect() job_id=%s", self._job_id)
        self._run_command(lambda: self._backend.approve_current(self._job_id))

    @Slot()
    def denyCurrentEffect(self) -> None:
        _logger.info("denyCurrentEffect() job_id=%s", self._job_id)
        self._run_command(lambda: self._backend.deny_current(self._job_id))

    @Slot(str)
    def submitUserAnswer(self, text: str) -> None:
        text = (text or "").strip()
        _logger.info("submitUserAnswer(len=%d) job_id=%s", len(text), self._job_id)
        if not text or len(text) > _MAX_ANSWER_CHARS:
            _logger.info("submitUserAnswer rejected: length=%d", len(text))
            return
        if self._job_id is None or self._command_in_flight:
            # Mirror `_run_command`'s guard before recording the answer in
            # the chat, so a dropped command never fabricates a sent bubble.
            _logger.info("submitUserAnswer dropped: job_id=%s command_in_flight=%s", self._job_id, self._command_in_flight)
            return
        self._append_chat("user", text)
        self._run_command(lambda: self._backend.submit_user_input(self._job_id, text))

    @Slot()
    def requestBrowserRebind(self) -> None:
        _logger.info("requestBrowserRebind() job_id=%s", self._job_id)
        self._run_command(lambda: self._backend.rebind_browser(self._job_id, None))

    @Slot(int)
    def selectRebindCandidate(self, index: int) -> None:
        _logger.info("selectRebindCandidate(index=%d) job_id=%s", index, self._job_id)
        if not (0 <= index < len(self._rebind_candidate_ids)):
            _logger.info("selectRebindCandidate rejected: index %d out of range (%d candidates)", index, len(self._rebind_candidate_ids))
            return
        candidate_id = self._rebind_candidate_ids[index]
        self._run_command(lambda: self._backend.select_rebind_candidate(self._job_id, candidate_id))

    @Slot()
    def clearSafeError(self) -> None:
        if self._safe_error_code is None:
            return
        self._safe_error_code = None
        self._safe_error_message = ""
        self.safeErrorChanged.emit()

    @Slot(str)
    def setTheme(self, theme: str) -> None:
        if theme not in ("system", "light", "dark") or theme == self._theme:
            return
        self._theme = theme
        self.themeChanged.emit()

    @Slot(bool)
    def setAnimationsEnabled(self, enabled: bool) -> None:
        if enabled == self._animations_enabled:
            return
        self._animations_enabled = enabled
        self.animationsEnabledChanged.emit()

    def _run_command(self, coro_factory: Any) -> None:
        if self._job_id is None or self._command_in_flight:
            _logger.info(
                "command dropped: job_id=%s command_in_flight=%s", self._job_id, self._command_in_flight,
            )
            return
        self._command_in_flight = True
        self.isBusyChanged.emit()
        self.commandFlagsChanged.emit()
        self._schedule("command", coro_factory)

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._generation += 1
