"""Small transactional history store. No browser commands are persisted/replayed."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

TERMINAL = frozenset({"completed", "failed", "cancelled", "limit_reached", "interrupted"})


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, **detail):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ':memory:' else None
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.depth = 0
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, role TEXT NOT NULL, text TEXT NOT NULL,
                created_at REAL NOT NULL, client_request_id TEXT UNIQUE, intent TEXT, delivery TEXT);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, type TEXT NOT NULL,
                occurred_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id, sequence);
            CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, ordinal);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS retired_requests (id TEXT PRIMARY KEY);
        """)
        if 'attachments' not in {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}:
            self.db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()

    @staticmethod
    def decode_message(row):
        item = dict(row)
        item['attachments'] = json.loads(item.get('attachments') or '[]')
        return item

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            self.depth += 1
            try:
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self.depth -= 1

    def close(self):
        with self.lock:
            self.db.close()

    def cursor(self):
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return row[0] if row else 0

    def conversation(self, conversation_id):
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise ServiceError("not_found", "Conversation no longer exists.", 404)
        return dict(row)

    def create_conversation(self, conversation_id=None):
        conversation_id = conversation_id or uuid4().hex
        with self.transaction():
            now = time.time()
            self.db.execute("INSERT OR IGNORE INTO conversations VALUES (?,?,?,?)",
                            (conversation_id, "New conversation", now, now))
            return self.conversation(conversation_id)

    def conversations(self, search="", offset=0):
        with self.lock:
            rows = self.db.execute("""SELECT c.*,
                (SELECT CASE WHEN intent='extend_budget' THEN 'Budget extension requested'
                    ELSE substr(text,1,180) END FROM messages m
                    WHERE m.conversation_id=c.id ORDER BY ordinal DESC LIMIT 1) AS preview
                FROM conversations c WHERE instr(lower(c.title), lower(?))>0
                ORDER BY updated_at DESC, id LIMIT 51 OFFSET ?""", (search, offset)).fetchall()
            return {"items": [dict(r) for r in rows[:50]], "has_more": len(rows) > 50}

    def task(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ServiceError("not_found", "Task no longer exists.", 404)
        return json.loads(row[0])

    def save_task(self, task):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (task["id"], task["conversation_id"], json.dumps(task)))
        self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?",
                        (time.time(), task["conversation_id"]))

    def event(self, conversation_id, task_id, kind, payload):
        data = json.dumps(payload)
        if len(data.encode()) > 524288:
            raise ValueError("Public event exceeds 512 KiB.")
        now = time.time()
        cursor = self.db.execute("INSERT INTO events (conversation_id,task_id,type,occurred_at,payload) VALUES (?,?,?,?,?)",
                                 (conversation_id, task_id, kind, now, data))
        return {"conversation_id": conversation_id, "task_id": task_id, "sequence": cursor.lastrowid,
                "type": kind, "occurred_at": now, "payload": payload}

    def update_task(self, task, kind="status", payload=None):
        with self.transaction():
            self.save_task(task)
            return self.event(task["conversation_id"], task["id"], kind,
                              {"task": task, **(payload or {})})

    def accepted(self, request_id):
        if self.db.execute('SELECT 1 FROM retired_requests WHERE id=?', (request_id,)).fetchone():
            raise ServiceError('stale_state', 'This instruction was already accepted, and its history was deleted. A retry cannot execute it again.')
        row = self.db.execute("SELECT * FROM messages WHERE client_request_id=?", (request_id,)).fetchone()
        return self.decode_message(row) if row else None

    def message(self, conversation_id, task_id, text, request_id, intent, delivery="applied", role="user", attachments=None):
        message = dict(id=uuid4().hex, conversation_id=conversation_id, task_id=task_id, role=role, text=text,
                       created_at=time.time(), client_request_id=request_id, intent=intent, delivery=delivery)
        self.db.execute("""INSERT INTO messages (id,conversation_id,task_id,role,text,created_at,client_request_id,intent,delivery)
                        VALUES (:id,:conversation_id,:task_id,:role,:text,:created_at,:client_request_id,:intent,:delivery)""", message)
        message['attachments'] = attachments or []
        self.db.execute('UPDATE messages SET attachments=? WHERE id=?', (json.dumps(message['attachments']), message['id']))
        self.event(conversation_id, task_id, "message", {"message": message})
        return message

    def delivery(self, message_id, state):
        self.db.execute("UPDATE messages SET delivery=? WHERE id=?", (state, message_id))
        row = self.decode_message(self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
        self.event(row["conversation_id"], row["task_id"], "message", {"message": row})

    @staticmethod
    def decode_events(rows):
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def events(self, after, limit=100):
        with self.lock:
            return self.decode_events(self.db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                                                      (after, min(limit, 100))).fetchall())

    def activity(self, task_id, before=None):
        with self.lock:
            self.task(task_id)
            rows = self.db.execute("""SELECT * FROM events WHERE task_id=? AND sequence<?
                AND type IN ('planner_decision','navigator_step','browser_action','status','final')
                ORDER BY sequence DESC LIMIT 31""", (task_id, before or self.cursor()+1)).fetchall()
            return {"items": self.decode_events(list(reversed(rows[:30]))), "has_more": len(rows)>30}

    def snapshot(self, conversation_id, before=None):
        with self.lock:
            conversation = self.conversation(conversation_id)
            rows = self.db.execute("SELECT * FROM messages WHERE conversation_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT 51",
                                   (conversation_id, before or 2**63-1)).fetchall()
            messages = [self.decode_message(row) for row in reversed(rows[:50])]
            task_ids = list(dict.fromkeys(message["task_id"] for message in messages if message["task_id"]))
            tasks = [self.task(task_id) for task_id in task_ids]
            return dict(conversation=conversation, messages=messages, tasks=tasks,
                        activity={task["id"]: self.activity(task["id"]) for task in tasks},
                        cursor=self.cursor(), has_more=len(rows)>50)

    def interrupt(self):
        with self.transaction():
            rows = self.db.execute("SELECT data FROM tasks").fetchall()
            for row in rows:
                task = json.loads(row[0])
                if task["status"] not in TERMINAL or task.get('owns_runtime'):
                    task.update(status="interrupted", owns_runtime=False, current_activity="The AXIS process ended. Start a new task to continue from saved context.", ended_at=time.time())
                    if task.get('workflow', {}).get('counts'):
                        task['current_activity'] = 'The process ended. Recover the saved workflow to reconcile its last step and continue.'
                    self.update_task(task)
            for row in self.db.execute("SELECT id FROM messages WHERE delivery='accepted'").fetchall():
                self.delivery(row[0], "not_applied")

    def get_settings(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='run'").fetchone()
            return json.loads(row[0]) if row else {}

    def set_settings(self, value):
        with self.transaction():
            self.db.execute("INSERT INTO settings VALUES ('run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
