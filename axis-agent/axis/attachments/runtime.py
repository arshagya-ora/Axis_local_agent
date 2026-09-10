"""Task-scoped document access and an ordered, durable workflow ledger."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from uuid import uuid4

INSTRUCTIONS = Path(__file__).with_name('instructions.txt').read_text(encoding='utf-8')


class DocumentRestriction(ValueError):
    """A user restriction needs clarification, not another document attempt."""


class DocumentSession:
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id
        self.last_result = None
        self.current_step = None

    def configure_uses(self, uses, request):
        """Persist uses grounded in user text, never in attachment instructions."""
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            saved = task.get('attachment_uses', {})
            for use in uses:
                role = self.store.require(self.task_id, use.attachment_id)
                if use.user_evidence not in request:
                    raise ValueError('Attachment use must quote the user request, not document content.')
                if 'execute' in use.operations and not re.search(r'(?:^|\b(?:and|then|please)\s+)(?:follow|execute|configure|apply|perform|implement|carry out)\b|\buse\b.+\bto\s+(?:configure|apply|execute)\b', request, re.I):
                    raise ValueError('Following file instructions needs a user request delegating that work.')
                if 'execute' in use.operations and re.search(r'\b(all|every|entire|complete)\b', request, re.I):
                    use = use.model_copy(update={'coverage': 'all'})
                if self._reading_restricted(use.attachment_id) and any(op != 'upload' for op in use.operations):
                    raise ValueError('This attachment was explicitly restricted to upload only. Ask before reading it.')
                prior = saved.get(use.attachment_id, {})
                data = use.model_dump()
                data['operations'] = sorted(set(prior.get('operations', [])) | set(data['operations']))
                if prior.get('coverage') == 'all':
                    data['coverage'] = 'all'
                saved[use.attachment_id] = data
            task['attachment_uses'] = saved
            self.store.history.save_task(task)

    def _uses(self):
        return self.store.history.task(self.task_id).get('attachment_uses', {})

    def progress(self, attempts=None, signatures=None):
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            if attempts is not None:
                task['document_progress'] = {'attempts': attempts, 'signatures': sorted(signatures or [])[-128:]}
                self.store.history.save_task(task)
            return task.get('document_progress', {'attempts':0, 'signatures':[]})

    def _reading_restricted(self, identifier):
        role = self.store.require(self.task_id, identifier)
        task = self.store.history.task(self.task_id)
        instruction = task.get('instruction', '')
        catalog = self.store.catalog(self.task_id)
        filename = self.store.get(identifier)['filename'].lower()
        restricted = role == 'upload'
        for match in re.finditer(r"\b(?:do not|don't|never)\s+(?:read|inspect|summarize|analyse|analyze)\b.*?(?=[.!?](?:\s|$)|[;\n]|$)", instruction, re.I):
            clause = match.group().lower()
            named = [item['filename'].lower() for item in catalog if item['filename'].lower() in clause]
            if not named or filename in named:
                restricted = True
        # Only a later, explicit user grant can relax a persisted restriction.
        # Attachment/page text never enters the user-message table.
        with self.store.history.lock:
            rows = self.store.history.db.execute("SELECT text FROM messages WHERE task_id=? AND intent='follow_up' AND delivery='applied' ORDER BY ordinal", (self.task_id,)).fetchall()
        single_file = len(catalog) == 1
        for row in rows:
            text = row[0].lower()
            if filename not in text and not single_file:
                continue
            if re.search(r"\b(?:do not|don't|never)\s+(?:read|inspect)\b", text):
                restricted = True
            elif re.search(r'\b(?:allow|permit|you may|you can now|go ahead and)\s+(?:reading|read|inspect)\b', text):
                restricted = False
        return restricted

    def _check_read(self, identifier):
        if self._reading_restricted(identifier):
            raise DocumentRestriction('Reading is explicitly restricted. May AXIS read this file for the requested summary, or should it only upload the original?')
        item = self.store.get(identifier)
        deadline = time.monotonic() + 30
        while item['status'] == 'processing' and time.monotonic() < deadline:
            time.sleep(.2)
            item = self.store.get(identifier)
        if item['status'] != 'ready':
            raise ValueError('Attachment content is not readable yet: ' + item['status'] + '. Wait for processing or request a readable file; the original can still be uploaded.')

    def context(self):
        catalog = [{k: item[k] for k in ("id", "filename", "role", "status", "search_status", "sections", "warnings")}
                   for item in self.store.catalog(self.task_id)]
        for item in catalog:
            item['warnings'] = item['warnings'][:6]
        with self.store.history.lock:
            followups = self.store.history.db.execute("SELECT text FROM messages WHERE task_id=? AND intent='follow_up' AND delivery='applied' ORDER BY ordinal DESC LIMIT 5", (self.task_id,)).fetchall()
        workflow = self.workflow()
        current_source = None
        if self.current_step:
            step = next((x for x in workflow['next_steps'] if x['id'] == self.current_step), None)
            if step:
                current_source = self.store.sections(self.task_id, step['attachment_id'], ids=[step['section_id']])
        return dict(attachments=catalog, uses=self._uses(), prior_progress=self.store.history.task(self.task_id).get('prior_progress', []), workflow=workflow, last_result=self.last_result, current_source=current_source,
                    user_followups=[row[0] for row in reversed(followups)], instructions=INSTRUCTIONS)

    def workflow(self, offset=0):
        with self.store.history.lock:
            db = self.store.history.db
            counts = dict(db.execute("SELECT status,count(*) FROM workflow_steps WHERE task_id=? GROUP BY status", (self.task_id,)).fetchall())
            exhaustive = [identifier for identifier, use in self._uses().items() if use['coverage'] == 'all' and 'execute' in use['operations']]
            slots = ','.join('?' for _ in exhaustive) or 'NULL'
            rows = db.execute("SELECT w.*,s.location,s.attachment_id FROM workflow_steps w JOIN attachment_sections s ON s.id=w.section_id WHERE task_id=? AND status NOT IN ('verified','skipped') ORDER BY ordinal LIMIT 9 OFFSET ?", (self.task_id, offset)).fetchall()
            missing = db.execute(f"""SELECT s.id,s.attachment_id,s.location FROM attachment_sections s JOIN task_attachments a ON a.attachment_id=s.attachment_id
                WHERE a.task_id=? AND (a.role='instructions' OR a.attachment_id IN ({slots})) AND NOT EXISTS
                (SELECT 1 FROM workflow_steps w WHERE w.task_id=a.task_id AND w.section_id=s.id) ORDER BY s.attachment_id,s.ordinal LIMIT 9""", (self.task_id, *exhaustive)).fetchall()
        return dict(counts=counts, next_steps=[dict(r) for r in rows[:8]], has_more=len(rows) > 8,
                    uncovered_sections=[dict(r) for r in missing[:8]], more_uncovered=len(missing) > 8,
                    recovery="A running step may already have changed the website. Observe its postcondition before any retry; never replay a submission blindly.")

    def execute(self, request):
        operation, identifier = request.operation, request.attachment_id
        history = self.store.history
        if operation == "workflow":
            result = self.workflow(request.offset)
        elif operation == "search":
            if not request.query:
                raise ValueError("Search requires a query.")
            for item in self.store.catalog(self.task_id):
                if (identifier == item['id']) or (not identifier and item['role'] != 'upload'):
                    self._check_read(item['id'])
            result = self.store.search(self.task_id, request.query, identifier)
        elif operation in {"outline", "read"}:
            if not identifier:
                raise ValueError("Choose an attachment_id from the catalog.")
            self._check_read(identifier)
            result = self.store.sections(self.task_id, identifier, ids=request.section_ids, offset=request.offset,
                                         limit=8 if operation == "read" else 40, sheet=request.sheet, cell_range=request.cell_range)
            if operation == "outline":
                result["items"] = [{k: item[k] for k in ("id", "ordinal", "location", "sheet", "row_number")} for item in result["items"]]
            else:
                with history.transaction():
                    for item in result["items"]:
                        history.db.execute("INSERT OR IGNORE INTO document_reads VALUES(?,?)", (self.task_id, item["id"]))
        elif operation in {"plan", "skip"}:
            task = self.store.history.task(self.task_id)
            delegated = any('execute' in use['operations'] for use in self._uses().values())
            if not delegated and not any(a['role'] == 'instructions' for a in self.store.catalog(self.task_id)) and not re.search(r'\b(configure|follow|execute|apply|perform|implement)\b', task.get('instruction', ''), re.I):
                raise ValueError('The user has not delegated document instructions for execution.')
            if operation == "skip" and (not request.reason or not request.section_ids):
                raise ValueError("Skipping non-actionable source sections requires section_ids and an explanation.")
            if operation == "plan" and not request.steps:
                raise ValueError("Plan requires source-linked steps with expected results.")
            steps = [dict(section_id=key, instruction=request.reason, expected_result="Not applicable to the requested workflow") for key in request.section_ids] if operation == "skip" else [item.model_dump() for item in request.steps]
            with history.transaction():
                ordinal = history.db.execute("SELECT COALESCE(max(ordinal),0) FROM workflow_steps WHERE task_id=?", (self.task_id,)).fetchone()[0]
                for item in steps:
                    owner = history.db.execute('SELECT attachment_id FROM attachment_sections WHERE id=?', (item['section_id'],)).fetchone()
                    if not owner:
                        raise ValueError('Unknown source section.')
                    role = self.store.require(self.task_id, owner[0])
                    if role == 'auto' and 'execute' not in self._uses().get(owner[0], {}).get('operations', []):
                        raise ValueError('This automatic attachment has not been delegated for instruction execution.')
                    if not history.db.execute("SELECT 1 FROM document_reads WHERE task_id=? AND section_id=?", (self.task_id, item["section_id"])).fetchone():
                        raise ValueError("Read every referenced source section before planning or skipping it.")
                    if operation == "skip" and history.db.execute("SELECT 1 FROM workflow_steps WHERE task_id=? AND section_id=?", (self.task_id, item["section_id"])).fetchone():
                        raise ValueError("Cannot skip a section that already has workflow steps.")
                    ordinal += 1
                    history.db.execute("INSERT OR IGNORE INTO workflow_steps(id,task_id,section_id,ordinal,instruction,expected_result,status,evidence) VALUES(?,?,?,?,?,?,?,?)",
                                       (uuid4().hex, self.task_id, item["section_id"], ordinal, item["instruction"], item["expected_result"],
                                        "skipped" if operation == "skip" else "pending", request.reason if operation == "skip" else None))
            result = self.workflow()
        else:
            raise ValueError("Unknown document operation.")
        self.last_result = dict(operation=operation, **result)
        return self.last_result

    def begin(self, step_id, goal_id, verification=None):
        workflow = self.workflow()
        if not workflow["next_steps"] or workflow["next_steps"][0]["id"] != step_id:
            raise ValueError("Execute the first pending workflow step; completed steps cannot be replayed.")
        saved = workflow['next_steps'][0].get('verification')
        check = verification.model_dump_json() if verification else None
        if saved and saved != check:
            raise ValueError('Retain the saved verification postcondition when recovering this step: ' + saved)
        with self.store.history.transaction():
            self.store.history.db.execute("UPDATE workflow_steps SET status='running',goal_id=?,verification=COALESCE(verification,?) WHERE id=? AND task_id=?", (goal_id, check, step_id, self.task_id))
        self.current_step = step_id

    def verified(self, state, matches):
        if not self.current_step:
            return
        from axis.models import ActionRecord
        # Only runtime assertions of the predeclared postcondition can complete a
        # workflow step. Model prose and document reads are never action evidence.
        passed = [a for a in state.assertions if a.passed and not a.superseded_by and a.goal_id == state.current_goal_id
                  and matches(state.current_verification, ActionRecord(tool='browser_assert', operation=a.assertion,
                              target=a.target, result_data={'expected': a.expected, 'match': a.match}))]
        if not passed or any(p.goal_id == state.current_goal_id for p in state.pending_changes.values()):
            raise ValueError("Workflow step needs a passing browser_assert for its declared postcondition.")
        with self.store.history.transaction():
            self.store.history.db.execute("UPDATE workflow_steps SET status='verified',evidence=? WHERE task_id=? AND id=?",
                                         (json.dumps([a.model_dump(mode="json") for a in passed]), self.task_id, self.current_step))
        self.current_step = None

    def completion_errors(self):
        work = self.workflow()
        errors = []
        if work["next_steps"]:
            errors.append("Source-linked workflow steps remain unverified.")
        if work["uncovered_sections"]:
            errors.append("Instruction sections remain uncovered: read them and plan every action, or explain why a section is not applicable.")
        if any(a['role'] == 'instructions' and any('OCR is required' in warning for warning in a['warnings']) for a in self.store.catalog(self.task_id)):
            errors.append('Required instruction pages are unreadable. Ask for an OCR/text version before claiming full completion.')
        with self.store.history.lock:
            reads = self.store.history.db.execute("SELECT count(*) FROM document_reads WHERE task_id=?", (self.task_id,)).fetchone()[0]
        if any(a["role"] in {"reference", "instructions"} for a in self.store.catalog(self.task_id)) and not reads:
            errors.append("Read the relevant attachment contents before answering.")
        for identifier, use in self._uses().items():
            if 'read' in use['operations'] or 'execute' in use['operations']:
                if use['coverage'] == 'all' and any('OCR is required' in warning for warning in self.store.get(identifier)['warnings']):
                    errors.append('Required attachment pages are unreadable. Request an OCR/text version: ' + identifier)
                with self.store.history.lock:
                    count = self.store.history.db.execute('SELECT count(*) FROM document_reads d JOIN attachment_sections s ON s.id=d.section_id WHERE d.task_id=? AND s.attachment_id=?', (self.task_id, identifier)).fetchone()[0]
                    total = self.store.history.db.execute('SELECT count(*) FROM attachment_sections WHERE attachment_id=?', (identifier,)).fetchone()[0]
                if not count or (use['coverage'] == 'all' and count < total):
                    errors.append('Required attachment coverage is incomplete: ' + identifier)
        for item in self.store.catalog(self.task_id):
            if item['role'] == 'auto' and item['id'] not in self._uses():
                errors.append('Declare the intended use and required coverage of attachment ' + item['id'])
        return errors

    def resolve_uploads(self, files):
        import hashlib
        from axis.ui_store import ServiceError
        resolved = []
        for identifier in files:
            self.store.require(self.task_id, identifier)
            try:
                path = self.store.path(identifier)
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                if digest != self.store.get(identifier)['digest']:
                    raise ValueError('The original attachment changed on disk. Attach the intended file again before uploading.')
                resolved.append(str(path))
            except ServiceError as error:
                raise ValueError(str(error)) from error
        return resolved
