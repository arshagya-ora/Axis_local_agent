"""Desktop-local chat sessions.

The durable layer has no session/thread concept — every task is an
independent Temporal job (`AxisJobState` carries no parent or session
identifier). A *session* is therefore purely a desktop-side grouping: one
conversation that accumulates the user's task messages, the observed
timeline events, and each job's result across many jobs, so the chat
thread survives starting the next task.

Persistence is a single bounded JSON file under the OS per-user
application-data directory. Nothing here touches Temporal, the browser,
or the agent; the store only ever holds what the presentation layer
already deemed safe to render (bounded, redacted text).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("axis.desktop.sessions")

_MAX_SESSIONS = 100
_MAX_MESSAGES_PER_SESSION = 500
_MAX_TEXT = 2000
_MAX_TITLE = 120


def _bound(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class ChatSession:
    """One conversation: an ordered message log plus the job ids it spans."""

    def __init__(
        self, session_id: str, title: str, created_at: float,
        updated_at: Optional[float] = None, job_ids: Optional[List[str]] = None,
        last_status: str = "", messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.session_id = session_id
        self.title = title
        self.created_at = created_at
        self.updated_at = updated_at if updated_at is not None else created_at
        self.job_ids: List[str] = list(job_ids or [])
        self.last_status = last_status
        self.messages: List[Dict[str, Any]] = list(messages or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "title": self.title,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "job_ids": self.job_ids, "last_status": self.last_status,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ChatSession":
        return cls(
            session_id=str(raw.get("session_id", "")),
            title=str(raw.get("title", "")),
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", raw.get("created_at", 0.0))),
            job_ids=[str(j) for j in raw.get("job_ids", [])],
            last_status=str(raw.get("last_status", "")),
            messages=[m for m in raw.get("messages", []) if isinstance(m, dict)],
        )


class SessionStore:
    """Bounded, best-effort persistence for chat sessions.

    With a ``path`` the store loads on construction and rewrites the file
    after every mutation (the file is small and bounded, so a full atomic
    rewrite is simpler and safer than an append log). Without a ``path``
    it is purely in-memory — the mode every existing test and the
    ``--smoke-test`` entry point use, so tests never write real files.

    A corrupt or unreadable file is a controlled, logged degradation to an
    empty store — never a crash and never silent data *mis*reads.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._sessions: Dict[str, ChatSession] = {}
        self._order: List[str] = []  # oldest first
        if path is not None:
            self._load()

    # -- queries ---------------------------------------------------------

    def list_sessions(self) -> List[ChatSession]:
        """Newest-updated last (callers reverse for display)."""
        return [self._sessions[sid] for sid in self._order]

    def get(self, session_id: str) -> Optional[ChatSession]:
        return self._sessions.get(session_id)

    def last_message_kind(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if session is None or not session.messages:
            return ""
        return str(session.messages[-1].get("kind", ""))

    def find_by_job(self, job_id: str) -> Optional[ChatSession]:
        for session in self._sessions.values():
            if job_id in session.job_ids:
                return session
        return None

    # -- mutations -------------------------------------------------------

    def create_session(self, title: str) -> ChatSession:
        session = ChatSession(
            session_id=f"axis-session-{uuid.uuid4().hex[:12]}",
            title=_bound(title, _MAX_TITLE) or "New session",
            created_at=time.time(),
        )
        self._sessions[session.session_id] = session
        self._order.append(session.session_id)
        if len(self._order) > _MAX_SESSIONS:
            oldest = self._order.pop(0)
            del self._sessions[oldest]
        self._save()
        return session

    def add_job(self, session_id: str, job_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or job_id in session.job_ids:
            return
        session.job_ids.append(job_id)
        self._touch(session)

    def append_message(self, session_id: str, kind: str, text: str, meta: str = "") -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.messages.append({
            "kind": kind, "text": _bound(text, _MAX_TEXT), "meta": _bound(meta, 60), "ts": time.time(),
        })
        overflow = len(session.messages) - _MAX_MESSAGES_PER_SESSION
        if overflow > 0:
            del session.messages[:overflow]
        self._touch(session)

    def set_title(self, session_id: str, title: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.title = _bound(title, _MAX_TITLE) or session.title
        self._touch(session)

    def set_last_status(self, session_id: str, status_label: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.last_status == status_label:
            return
        session.last_status = status_label
        self._touch(session)

    def _touch(self, session: ChatSession) -> None:
        session.updated_at = time.time()
        # Keep most-recently-updated at the end of the order.
        if self._order and self._order[-1] != session.session_id:
            self._order.remove(session.session_id)
            self._order.append(session.session_id)
        self._save()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in raw.get("sessions", []):
                session = ChatSession.from_dict(entry)
                if session.session_id:
                    self._sessions[session.session_id] = session
                    self._order.append(session.session_id)
        except (OSError, ValueError) as exc:
            _logger.warning("Could not load the session store (%s); starting empty.", type(exc).__name__)
            self._sessions = {}
            self._order = []

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"sessions": [self._sessions[sid].to_dict() for sid in self._order]},
                ensure_ascii=False,
            )
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            _logger.warning("Could not persist the session store (%s).", type(exc).__name__)


def default_store_path() -> Optional[Path]:
    """Per-user application-data location for the real desktop app.

    Uses Qt's own writable app-data directory (org/app names are set in
    `axis.desktop.app` before this is called) so the path is correct per
    platform without any new dependency. Returns None when Qt cannot
    provide one — the caller then runs with an in-memory store rather
    than failing startup over a persistence nicety.
    """
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not base:
            return None
        return Path(base) / "sessions.json"
    except Exception:  # noqa: BLE001 - persistence must never block startup
        _logger.warning("Could not resolve a writable app-data location; sessions will not persist.")
        return None
