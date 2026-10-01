"""Task-scoped document access and an ordered, durable workflow ledger."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from uuid import uuid4
from .progress import DocumentProgress
from .overview import DocumentOverview
from .scope import constraints, normalized, user_messages, overview_request

INSTRUCTIONS = Path(__file__).with_name('instructions.txt').read_text(encoding='utf-8')


class DocumentRestriction(ValueError):
    """A user restriction needs clarification, not another document attempt."""


class DocumentSession(DocumentOverview, DocumentProgress):
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id
        task = store.history.task(task_id)
        self.last_result = task.get('document_checkpoint', {}).get('last_result')
        with store.history.lock:
            saved = store.history.db.execute('SELECT last_result FROM document_checkpoints WHERE task_id=?', (task_id,)).fetchone()
            if saved:
                self.last_result = json.loads(saved[0])
        self.current_step = None
        self.refresh_scope()

    def configure_uses(self, uses, request):
        """Persist uses grounded in user text, never in attachment instructions."""
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            saved = task.get('attachment_uses', {})
            for use in uses:
                self.store.require(self.task_id, use.attachment_id)
                if normalized(use.user_evidence) not in normalized(request):
                    raise ValueError('Attachment use must quote the user request, not document content.')
                if 'execute' in use.operations and not constraints(self.store, self.task_id, use.attachment_id)['execute']:
                    raise ValueError('Following file instructions needs a user request delegating that work.')
                if 'execute' in use.operations and re.search(r'\b(all|every|entire|complete)\b', request, re.I):
                    use = use.model_copy(update={'coverage': 'all'})
                if self._reading_restricted(use.attachment_id) and any(op != 'upload' for op in use.operations):
                    raise DocumentRestriction('The current user instructions prohibit reading this attachment. Clarify the conflicting request.')
                if use.purpose in {'summary', 'overview'} and not constraints(self.store, self.task_id, use.attachment_id)['summarize']:
                    raise DocumentRestriction('The user requested extraction without a summary. Return the requested values instead.')
                data = use.model_dump()
                inferred = self.inferred_uses().get(use.attachment_id)
                if inferred and 'read' in inferred['operations']:
                    data['operations'] = sorted(set(data['operations']) | {'read'})
                    data['purpose'] = inferred['purpose']
                    data['coverage'] = inferred['coverage']
                data.update(self.scope(use.attachment_id, use.sheet, use.cell_range))
                if data['purpose'] in {'summary', 'enumerate', 'execute'} or data.get('cell_range'):
                    data['coverage'] = 'all'
                elif data['purpose'] == 'overview':
                    data['coverage'] = 'relevant'
                saved[use.attachment_id] = data
            task['attachment_uses'] = saved
            self.store.history.save_task(task)

    def inferred_uses(self):
        """Bootstrap clear document requests; ambiguity stays with the planner."""
        catalog = self.store.catalog(self.task_id)
        messages = user_messages(self.store, self.task_id)
        text = '\n'.join(messages)
        lower = text.casefold()
        selected = catalog if len(catalog) == 1 or re.search(r'\b(both|all|every|these|documents|files)\b', lower) else []
        named = [a for a in catalog if a['filename'].casefold() in lower]
        if named and not re.search(r'\b(both|all|every|these)\b', lower):
            selected = named
        summary = bool(re.search(r'\b(?:summari[sz]e|summary|describe)\b|what\b.*?\bpresent\b', lower))
        overview = overview_request(messages[-1]) if messages else False
        enumeration = bool(re.search(r'\b(?:all|every)\b.*?\b(?:scenarios?|names?|entries|items|rows|sections?|paragraphs?|pages?|cells)\b', lower, re.S))
        read = overview or summary or enumeration or bool(re.search(r'\b(?:read|inspect|extract)\b', lower))
        upload = bool(re.search(r'\b(?:upload|attach)\b', lower))
        from .scope import requested_range
        uses = {}
        for item in selected:
            policy = constraints(self.store, self.task_id, item['id'])
            bounds = requested_range(self.store, self.task_id, item['id'])
            operations = (['read'] if read and policy['read'] else []) + (['upload'] if upload and policy['upload'] else [])
            if policy['execute']:
                operations = sorted(set(operations) | {'read', 'execute'})
            if not operations:
                continue
            purpose = 'execute' if 'execute' in operations else 'answer' if bounds else 'enumerate' if enumeration else 'overview' if overview and policy['summarize'] else 'summary' if summary and policy['summarize'] else 'answer' if 'read' in operations else 'upload'
            uses[item['id']] = dict(attachment_id=item['id'], operations=operations,
                coverage='all' if purpose in {'summary', 'enumerate', 'execute'} or bounds else 'relevant',
                purpose=purpose, user_evidence=text[:1000], sheet=bounds.get('sheet'), cell_range=bounds.get('cell_range'))
        return uses

    def refresh_scope(self):
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            saved = task.get('attachment_uses', {})
            saved.update(self.inferred_uses())
            for identifier, use in saved.items():
                policy = constraints(self.store, self.task_id, identifier)
                use['operations'] = [op for op in use['operations'] if policy[op] and (op != 'execute' or policy['read'])]
                if use.get('purpose') in {'summary', 'overview'} and not policy['summarize']:
                    use['purpose'] = 'answer'
            task['attachment_uses'] = saved
            self.store.history.save_task(task)
            if self.last_result and self.last_result.get('attachment_id'):
                identifier = self.last_result['attachment_id']
                if self._reading_restricted(identifier) or self.last_result.get('scope') != self.scope(identifier):
                    self.last_result = None

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
        return not constraints(self.store, self.task_id, identifier)['read']

    def _check_read(self, identifier):
        if self._reading_restricted(identifier):
            raise DocumentRestriction('Reading is explicitly restricted by the current user instructions. Clarify only if the requested task requires reading this file.')
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
        workflow = self.workflow()
        current_source = None
        if self.current_step:
            step = next((x for x in workflow['next_steps'] if x['id'] == self.current_step), None)
            if step:
                self._check_read(step['attachment_id'])
                current_source = self.store.sections(self.task_id, step['attachment_id'], ids=[step['section_id']], **self.scope(step['attachment_id']))
        overviews = [self.saved_overview(identifier) for identifier, use in self._uses().items() if use.get('purpose') == 'overview']
        last_result = self.last_result
        if last_result and last_result.get('operation') == 'overview' and any(x and x['attachment_id'] == last_result['attachment_id'] for x in overviews):
            last_result = dict(operation='overview', attachment_id=last_result['attachment_id'], note='Source preview is retained in overviews. Answer the overview now.')
        return dict(attachments=catalog, uses=self._uses(), coverage=self.coverage(), findings=self.notes(),
                    overviews=[value for value in overviews if value],
                    prior_progress=self.store.history.task(self.task_id).get('prior_progress', []), workflow=workflow,
                    last_result=last_result, current_source=current_source,
                    user_followups=user_messages(self.store, self.task_id)[-5:], instructions=INSTRUCTIONS)

    def workflow(self, offset=0):
        with self.store.history.lock:
            db = self.store.history.db
            counts = dict(db.execute("SELECT status,count(*) FROM workflow_steps WHERE task_id=? GROUP BY status", (self.task_id,)).fetchall())
            exhaustive = [identifier for identifier, use in self._uses().items() if use['coverage'] == 'all' and 'execute' in use['operations']]
            rows = db.execute("SELECT w.*,s.location,s.attachment_id FROM workflow_steps w JOIN attachment_sections s ON s.id=w.section_id WHERE task_id=? AND status NOT IN ('verified','skipped') ORDER BY ordinal LIMIT 9 OFFSET ?", (self.task_id, offset)).fetchall()
            missing = []
            for identifier in exhaustive:
                where, args, _ = self.store.section_filter(identifier, **self.scope(identifier))
                missing.extend(db.execute(f'''SELECT id,attachment_id,location FROM attachment_sections WHERE {where}
                    AND NOT EXISTS (SELECT 1 FROM workflow_steps w WHERE w.task_id=? AND w.section_id=attachment_sections.id)
                    ORDER BY ordinal LIMIT ?''', (*args, self.task_id, 9-len(missing))).fetchall())
                if len(missing) >= 9:
                    break
        return dict(counts=counts, next_steps=[dict(r) for r in rows[:8]], has_more=len(rows) > 8,
                    uncovered_sections=[dict(r) for r in missing[:8]], more_uncovered=len(missing) > 8,
                    recovery="A running step may already have changed the website. Observe its postcondition before any retry; never replay a submission blindly.")

    def execute(self, request):
        operation, identifier = request.operation, request.attachment_id
        history = self.store.history
        if (identifier and operation in {'outline', 'read'} and not request.reread
                and not request.cell_range and not request.sheet
                and self._uses().get(identifier, {}).get('purpose') == 'overview'
                and not self.saved_overview(identifier)):
            operation = 'overview'
        if operation == 'overview':
            if not identifier:
                raise ValueError('Choose an attachment_id for the overview.')
            if request.sheet or request.cell_range:
                raise ValueError('Use read for an explicit sheet/cell range; overview uses the task-declared scope.')
            if not constraints(self.store, self.task_id, identifier)['summarize']:
                raise DocumentRestriction('An overview conflicts with the user request to avoid summarizing. Read the requested values instead.')
            result = self.overview(identifier)
        elif operation == "workflow":
            result = self.workflow(request.offset)
        elif operation == 'notes':
            result = self.notes(request.offset, identifier)
        elif operation == "search":
            if not request.query:
                raise ValueError("Search requires a query.")
            for item in self.store.catalog(self.task_id):
                if (identifier == item['id']) or (not identifier and not self._reading_restricted(item['id'])):
                    self._check_read(item['id'])
            result = self.store.search(self.task_id, request.query, identifier)
            # Ranked candidates are not exhaustive and are not read evidence.
            result.update(search_complete=False, note='Ranked candidates only. Use enumerate to exhaust the requested source scope.')
            scoped = []
            for item in result['items']:
                scope = self.scope(item['attachment_id'])
                selected = self.store.sections(self.task_id, item['attachment_id'], ids=[item['id']], **scope)
                if selected['items']:
                    scoped.append(selected['items'][0])
            result['items'] = scoped
        elif operation in {"outline", "read", "enumerate"}:
            if not identifier:
                raise ValueError("Choose an attachment_id from the catalog.")
            self._check_read(identifier)
            scope = self.scope(identifier, request.sheet, request.cell_range)
            key = self.scope_key(scope)
            advance = request.advance or operation == 'enumerate'
            # Repeated traversal requests advance without another planner repair.
            fingerprint = json.dumps([operation, identifier, request.offset, request.section_ids, scope], sort_keys=True)
            with history.lock:
                seen = history.task(self.task_id).get('document_requests', [])
            if fingerprint in seen and not request.reread:
                if operation == 'outline':
                    operation = 'read'
                advance = True
            result = self.store.sections(self.task_id, identifier, ids=request.section_ids, offset=request.offset,
                                         limit=40 if operation == 'outline' else 16, **scope,
                                         outline=operation == 'outline', unread=advance, scope=key) if not advance else self.store.sections(
                                             self.task_id, identifier, limit=16, **scope, unread=True, scope=key)
            result.update(attachment_id=identifier, scope=scope, new_sections=0)
            with history.transaction():
                task = history.task(self.task_id)
                task['document_requests'] = (seen + [fingerprint])[-128:]
                history.save_task(task)
            if operation != "outline":
                with history.transaction():
                    if not result['has_more']:
                        history.db.execute('INSERT OR IGNORE INTO document_scans VALUES(?,?,?)', (self.task_id, identifier, key))
                    for item in result["items"]:
                        cursor = history.db.execute('INSERT OR IGNORE INTO document_scope_reads VALUES(?,?,?)', (self.task_id, item['id'], key))
                        result['new_sections'] += cursor.rowcount
                        if not scope['cell_range']:
                            history.db.execute("INSERT OR IGNORE INTO document_reads VALUES(?,?)", (self.task_id, item["id"]))
                        # A bounded fallback preserves source anchors even if the
                        # planner omits findings. It never claims to be a summary.
                        if not history.db.execute('SELECT 1 FROM document_notes WHERE task_id=? AND section_id=?', (self.task_id, item['id'])).fetchone():
                            history.db.execute('INSERT OR IGNORE INTO document_notes VALUES(?,?,?)', (self.task_id, item['id'], item['text'][:300]))
                result['coverage'] = next(x for x in self.coverage() if x['attachment_id'] == identifier)
                result['search_complete'] = operation == 'enumerate' and result['coverage']['complete']
                if not result['items']:
                    result['note'] = 'No unread sections remain in this scope. Use findings for synthesis or reread=true for specific source verification.'
        elif operation in {"plan", "skip"}:
            task = self.store.history.task(self.task_id)
            delegated = any('execute' in use['operations'] for use in self._uses().values())
            if not delegated:
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
                    self.store.require(self.task_id, owner[0])
                    self._check_read(owner[0])
                    if not constraints(self.store, self.task_id, owner[0])['execute']:
                        raise ValueError('The current user instructions do not delegate execution from this file.')
                    if 'execute' not in self._uses().get(owner[0], {}).get('operations', []):
                        raise ValueError('This automatic attachment has not been delegated for instruction execution.')
                    scope = self.scope(owner[0])
                    source = self.store.sections(self.task_id, owner[0], ids=[item['section_id']], **scope)
                    if not source['items'] or not history.db.execute("SELECT 1 FROM document_reads WHERE task_id=? AND section_id=? UNION SELECT 1 FROM document_scope_reads WHERE task_id=? AND section_id=? AND scope=?", (self.task_id, item['section_id'], self.task_id, item['section_id'], self.scope_key(scope))).fetchone():
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
        step = workflow['next_steps'][0]
        self._check_read(step['attachment_id'])
        if not constraints(self.store, self.task_id, step['attachment_id'])['execute']:
            raise ValueError('The current user instructions do not delegate this workflow.')
        if not self.store.sections(self.task_id, step['attachment_id'], ids=[step['section_id']], **self.scope(step['attachment_id']))['items']:
            raise ValueError('This workflow step is outside the current requested source scope.')
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
        coverage = {item['attachment_id']: item for item in self.coverage()}
        for identifier, use in self._uses().items():
            if 'read' in use['operations'] or 'execute' in use['operations']:
                if use['coverage'] == 'all' and any('OCR is required' in warning for warning in self.store.get(identifier)['warnings']):
                    errors.append('Required attachment pages are unreadable. Request an OCR/text version: ' + identifier)
                item = coverage[identifier]
                if use.get('purpose') == 'overview' and not item['complete']:
                    if not self.saved_overview(identifier):
                        errors.append('Read a bounded overview for attachment ' + identifier + ' using operation=overview.')
                    continue
                if (not item['read'] and not item['complete']) or (use['coverage'] == 'all' and not item['complete']):
                    errors.append(f"Required attachment coverage is incomplete: {identifier}; {item['read']}/{item['total']} sections read. Next section IDs: {item['next_section_ids']}. Use read with advance=true or enumerate.")
        for item in self.store.catalog(self.task_id):
            if item['id'] not in self._uses() and not coverage[item['id']]['read']:
                errors.append('Declare the intended use and required coverage of attachment ' + item['id'])
        return errors

    def resolve_uploads(self, files):
        import hashlib
        from axis.ui_store import ServiceError
        resolved = []
        for identifier in files:
            self.store.require(self.task_id, identifier)
            if not constraints(self.store, self.task_id, identifier)['upload']:
                raise ValueError('Uploading this file conflicts with the current user instructions.')
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
