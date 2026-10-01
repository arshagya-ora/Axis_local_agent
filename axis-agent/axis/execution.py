"""Durable, task-scoped authorization before browser effects (never replay RPCs)."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from axis.ui_store import ServiceError


def external_effect(tool, operation):
    return (tool in {'browser_act', 'browser_visual'} and operation not in {'capture', 'scroll', 'hover'}) or (tool == 'browser_tabs' and operation == 'close')


def automatic_scope(instruction, detail, operation):
    """Conservative scope filter; ambiguous effects still require approval."""
    request = instruction.lower()
    if re.match(r'\s*(what|why|how|summari[sz]e|explain|describe|review|analy[sz]e|read)\b', request) and not re.search(r'\b(?:and|then)\s+(?:please\s+)?(?:upload|attach|send|configure|apply|fill|create|update)\b', request):
        return False
    target = str(detail.get('target') or '').lower()
    if not target or detail.get('target_type') not in {'ref', 'locator'}:
        return False
    if re.search(r"\b(?:do not|don't|never)\s+(?:change|modify)\b|\bread.only\b", request):
        return False
    if operation == 'upload' and re.search(r"\b(?:do not|don't|never)\s+(?:upload|attach)\b", request):
        return False
    host = urlsplit(detail.get('current_url') or '').hostname
    named_hosts = {urlsplit(url.rstrip('.,)')).hostname for url in re.findall(r'https?://[^\s]+', instruction)}
    for name, domain in [('gmail', 'mail.google.com'), ('outlook', 'outlook.live.com')]:
        if name in request:
            named_hosts.add(domain)
    if not host or host not in named_hosts:
        return False
    observed = detail.get('observed_target') or {}
    for destination in (observed.get('href'), observed.get('formAction')):
        if destination and urlsplit(destination).hostname not in named_hosts:
            return False
    for effect, pattern in [('send', r'\bsend\b'), ('delete', r'\bdelete\b|\bremove\b'),
                            ('purchase', r'\bbuy\b|\bpurchase\b|\bcheckout\b')]:
        if re.search(pattern, target) and (not re.search(pattern, request) or
                (effect == 'send' and re.search(r'\bdraft\b|\bdo not send\b|\bdon.t send\b', request))):
            return False
    if operation in {'press', 'drag', 'type'}:
        return False  # keyboard/coordinate submission needs an explicit review
    if re.search(r'\b(to|cc|bcc|recipient)\b', target):
        value = str((detail.get('arguments') or {}).get('command', {}).get('value', ''))
        addresses = re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', value)
        if not addresses or any(address.lower() not in request for address in addresses):
            return False
    if operation == 'upload':
        return bool(re.search(r'\b(upload|attach)\b', request))
    if re.search(r'\b(draft|compose|gmail|outlook)\b', request):
        if operation == 'click':
            return bool(re.search(r'\b(compose|new (?:message|mail)|attach|save draft)\b', target))
        if operation == 'fill':
            return bool(re.search(r'\b(message|body|subject|to|cc|bcc|recipient)\b', target))
        return False
    return bool(re.search(r'\b(draft|compose|prepare|fill|configure|apply|update|send|attach|upload|create)\b', request))


class ExecutionApprovals:
    def __init__(self, runner):
        self.runner, self.store = runner, runner.store
        with self.store.transaction():
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS execution_approvals (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
                decision TEXT, created_at REAL NOT NULL)''')
            self.store.db.execute("UPDATE execution_approvals SET status='invalidated' WHERE status IN ('pending','approved')")
            for row in self.store.db.execute('SELECT data FROM tasks').fetchall():
                task = json.loads(row[0])
                if task.pop('pending_approval', None):
                    self.store.save_task(task)

    def _pause(self, task_id, reason):
        if self.runner.requested != 'stop' and self.runner.active_id == task_id:
            self.runner.requested = 'pause'
            self.runner._request_status('pausing', reason)
            self.runner.loop.call_soon_threadsafe(self.runner._apply_control, task_id, 'pause')

    def invalidate(self, task_id):
        with self.store.transaction():
            self.store.db.execute("UPDATE execution_approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')", (task_id,))
            task = self.store.task(task_id)
            task.pop('pending_approval', None)
            self.store.update_task(task)

    def decide(self, task_id, identifier, decision):
        with self.store.transaction():
            row = self.store.db.execute('SELECT * FROM execution_approvals WHERE id=? AND task_id=?', (identifier, task_id)).fetchone()
            if not row:
                raise ServiceError('not_found', 'Approval no longer exists.', 404)
            if row['status'] == 'invalidated':
                raise ServiceError('stale_state', 'This approval was invalidated. Review the current action.')
            if row['decision']:
                if row['decision'] != decision:
                    raise ServiceError('stale_state', 'This approval already has a different decision.')
                return {'id': identifier, 'status': row['status'], 'duplicate': True}
            self.store.db.execute('UPDATE execution_approvals SET status=?,decision=? WHERE id=?',
                                  ('approved' if decision == 'approve' else 'denied', decision, identifier))
            return {'id': identifier, 'status': decision, 'duplicate': False}

    def authorize(self, tool, operation, detail, mandatory=False):
        mandatory = mandatory or detail.get('mandatory_approval', False)
        runner = self.runner
        with runner.lock, self.store.transaction():
            if runner.closed or runner.requested or not runner.active_id:
                return False
            task = self.store.task(runner.active_id)
            navigation_change = tool in {'browser_navigate', 'browser_tabs'} and re.search(r'compose|[?&](?:action|do)=|/delete|/send|logout|signout', str(detail.get('target') or ''), re.I)
            if not mandatory and not external_effect(tool, operation) and not navigation_change:
                return True
            instruction = task.get('instruction', '')
            if operation == 'upload' and re.search(r"\b(?:do not|don't|never)\s+(?:upload|attach)\b", instruction, re.I):
                self._pause(task['id'], 'Uploading conflicts with an explicit user restriction.')
                return False
            if re.search(r"\b(?:do not|don't|never)\s+" + re.escape(operation or '') + r'\b', instruction, re.I):
                self._pause(task['id'], 'This action conflicts with an explicit user restriction.')
                return False
            command = (detail.get('arguments') or {}).get('command') or {}
            draft_only = bool(re.search(r'\bdraft\b', instruction, re.I) and not re.search(r'\b(?:and|then)\s+send\b', instruction, re.I))
            if draft_only and (operation in {'click', 'dblclick', 'press', 'type'} and
                               (detail.get('target_type') not in {'ref', 'locator'} or operation == 'press')):
                self._pause(task['id'], 'Draft-only work requires an identified control, without keyboard submission. Observe the page before continuing.')
                return False
            observed = detail.get('observed_target') or {}
            send_action = re.search(r'\bsend\b', str(detail.get('target') or ''), re.I) or (draft_only and operation == 'click' and observed.get('type') == 'submit' and observed.get('formAction')) or (operation == 'press' and re.search(r'(control|ctrl|meta|cmd).*enter', str(command.get('key', '')), re.I))
            if send_action and (not re.search(r'\bsend\b', instruction, re.I) or re.search(r'\bdo not send\b|\bdon.t send\b', instruction, re.I) or (re.search(r'\bdraft\b', instruction, re.I) and not re.search(r'\b(?:and|then)\s+send\b', instruction, re.I))):
                self._pause(task['id'], 'Sending is outside this draft-only request. Clarify the task before continuing.')
                return False
            revision = task.get('plan_revision', 0)
            files = []
            for identifier in detail.get('upload_filenames') or []:
                if runner.attachments:
                    runner.attachments.require(task['id'], identifier)
                    item = runner.attachments.get(identifier)
                    files.append({'id': identifier, 'filename': item['filename'], 'sha256': item.get('digest')})
            upload_in_scope = operation != 'upload' or (files and all(
                'upload' in task.get('attachment_uses', {}).get(item['id'], {}).get('operations', [])
                or runner.attachments.require(task['id'], item['id']) == 'upload' for item in files))
            if not mandatory and upload_in_scope and task.get('execution_mode', 'automatic') == 'automatic' and automatic_scope(instruction, detail, operation):
                return True
            data = {'tool': tool, 'operation': operation, **detail, 'plan_revision': revision,
                    'files': files,
                    'task_id': task['id'], 'mode_revision': task.get('mode_revision', 0),
                    'verification': getattr(getattr(runner.orchestrator, 'state', None), 'current_verification', None)}
            if hasattr(data['verification'], 'model_dump'):
                data['verification'] = data['verification'].model_dump()
            encoded = json.dumps(data, sort_keys=True, default=str)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            identifier = uuid4().hex
            self.store.db.execute('INSERT INTO execution_approvals VALUES (?,?,?,?,?,?,?)',
                                  (identifier, task['id'], fingerprint, encoded, 'pending', None, time.time()))
            task['pending_approval'] = {'id': identifier, 'fingerprint': fingerprint, 'actions': [data]}
            task['current_activity'] = 'Approval needed before changing the website'
            self.store.update_task(task, payload={'text': task['current_activity']})
        # This callback runs in the browser tool worker, not the service/event loop.
        while True:
            with runner.lock, self.store.transaction():
                current = self.store.task(task['id'])
                row = self.store.db.execute('SELECT status FROM execution_approvals WHERE id=?', (identifier,)).fetchone()
                status = row['status'] if row else 'invalidated'
                if runner.closed or runner.requested or runner.active_id != task['id'] or current.get('mode_revision', 0) != data['mode_revision'] or current.get('plan_revision', 0) != revision:
                    self.invalidate(task['id'])
                    return False
                if status != 'pending':
                    current.pop('pending_approval', None)
                    current['current_activity'] = 'Applying approved action' if status == 'approved' else 'Action not approved; task paused'
                    self.store.update_task(current)
                    if status == 'approved':
                        self.store.db.execute("UPDATE execution_approvals SET status='consumed' WHERE id=?", (identifier,))
                        return True
                    self._pause(task['id'], 'The proposed action was not approved. Task context is preserved.')
                    return False
            time.sleep(.1)
