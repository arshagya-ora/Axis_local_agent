"""One execution owner, on a dedicated thread so synchronous RPC cannot block HTTP.

Commands reserve/persist under a lock. Orchestrator mutations execute only on
its own loop. Pausing/stopping are requests until the entry point has returned.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future
import logging
import sqlite3
import threading
import time
from uuid import uuid4

from axis.bootstrap import build_orchestrator
from axis.console import print_debug_event, print_event, print_result
from axis.models import AxisConfig, AxisEvent, AxisResult
from axis.ui_store import Store, ServiceError, TERMINAL

log = logging.getLogger(__name__)


def public_activity(event: AxisEvent) -> str | None:
    """Only operation names and public outcome labels; no args or model reasoning."""
    detail = event.detail
    if event.kind == 'browser_action':
        name = str(detail.get('tool', 'Browser action'))[:80].replace('browser_', '').replace('_', ' ')
        operation = str(detail.get('operation') or '')[:60].replace('_', ' ')
        outcome = 'skipped' if detail.get('skipped') else 'completed' if detail.get('success') else 'did not succeed'
        return f'{name}{": " + operation if operation else ""} — {outcome}'
    if event.kind == 'planner_decision':
        return {'browse': 'Browser step planned', 'complete': 'Checking task completion',
                'ask_user': 'Input requested', 'fail': 'Task could not proceed'}.get(detail.get('decision'))
    if event.kind == 'navigator_step':
        return {'continue': 'Browser step finished', 'goal_reached': 'Checking browser result',
                'blocked': 'Browser step blocked', 'ask_user': 'Input requested', 'failed': 'Browser step failed'}.get(detail.get('status'))
    if event.kind == 'status':
        return {'planner_started': 'Planning the next step', 'navigator_step_started': 'Working in the browser',
                'observed': 'Read browser context', 'tab_selected': 'Selected a browser tab',
                'tab_create': 'Opened a browser tab', 'vision_unavailable': 'Visual input unavailable'}.get(detail.get('phase'))
    return None  # final results are persisted authoritatively after the runner returns


def safe_result(result: AxisResult) -> dict:
    return dict(status=result.status, answer=(result.answer or '')[:64000], reason=result.reason[:1000],
                answer_truncated=len(result.answer or '') > 64000,
                total_steps=result.total_steps, browser_actions=result.browser_actions,
                model_requests=result.model_requests, duration_ms=result.duration_ms,
                limitations=[str(x)[:600] for x in result.limitations[:6]])


class Runner:
    def __init__(self, store: Store, config: AxisConfig, factory=build_orchestrator, *, debug=False, verbose=False):
        self.store, self.config, self.factory = store, config, factory
        self.debug = debug
        self.event_printer = print_debug_event if debug else print_event if verbose else None
        self.result_printer = print_result if debug or verbose else None
        self.console_available = True
        self.lock = threading.RLock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='axis-ui-runner', daemon=True)
        self.thread.start()
        self.future: Future | None = None
        self.orchestrator = None
        self.active_id: str | None = None
        self.pending: dict | None = None
        self.requested: str | None = None
        self.storage_error: str | None = None
        self.closed = False

    def active(self):
        with self.lock, self.store.lock:
            return self.store.task(self.active_id) if self.active_id else None

    def _storage_failed(self):
        # _emit swallows callback errors. Maintain a separate health failure and
        # request cancellation here; never pretend an uncommitted event exists.
        self.storage_error = 'History could not be saved. Execution was asked to stop. Check disk access and free space, then restart AXIS.'
        self.requested = 'stop'
        if self.orchestrator:
            self.orchestrator.pause()
        log.error('AXIS UI history storage failed; execution requested to stop.')

    def submit(self, conversation_id, body):
        with self.lock, self.store.transaction():
            if self.storage_error or self.closed:
                raise ServiceError('unavailable', self.storage_error or 'Service is shutting down.', 503)
            self.store.conversation(conversation_id)
            previous = self.store.accepted(body.client_request_id)
            if previous:
                if (previous['conversation_id'], previous['text'], previous['intent']) != (conversation_id, body.text, body.intent) or (body.intent == 'follow_up' and previous['task_id'] != body.task_id):
                    raise ServiceError('invalid_input', 'This request ID was already used for a different instruction.')
                return dict(message=previous, task=self.store.task(previous['task_id']), duplicate=True)
            if body.intent == 'new_task':
                if self.active_id:
                    raise ServiceError('busy', 'A task already owns this browser. Resume it or stop it before starting another.',
                                       active_task=self.store.task(self.active_id))
                task_id = uuid4().hex
                task = dict(id=task_id, conversation_id=conversation_id, title=body.text[:80], status='active',
                            owns_runtime=True,
                            current_activity='Accepted; preparing the agent', created_at=time.time(), updated_at=time.time(),
                            ended_at=None, counters={'browser_actions': 0, 'total_steps': 0}, result=None, browser_context=None,
                            limits=self.config.run.model_dump())
                self.store.save_task(task)
                conversation = self.store.conversation(conversation_id)
                if conversation['title'] == 'New conversation':
                    self.store.db.execute('UPDATE conversations SET title=? WHERE id=?', (body.text[:80], conversation_id))
                instruction = body.text
                if body.context_task_id:
                    source = self.store.task(body.context_task_id)
                    if source['conversation_id'] != conversation_id:
                        raise ServiceError('invalid_input', 'Saved context must belong to this conversation.', 422)
                    outcome = (source.get('result') or {}).get('answer') or source.get('current_activity', '')
                    available = max(0, 4000-len(instruction)-60)
                    if available:
                        instruction += '\n\nSaved prior outcome (context only):\n' + outcome[:min(800, available)]
                message = self.store.message(conversation_id, task_id, body.text, body.client_request_id, body.intent)
                self.store.event(conversation_id, task_id, 'status', {'task': task, 'text': task['current_activity']})
                # Publish execution only after acceptance has committed (below).
                next_run = ('start', (instruction, self.config.model_copy(deep=True)), task_id)
            else:
                if body.task_id != self.active_id or self.orchestrator is None:
                    raise ServiceError('stale_state', 'This task has no live continuation. Start a new task using its saved outcome.')
                task = self.store.task(body.task_id)
                if task['status'] == 'limit_reached':
                    raise ServiceError('stale_state', 'Explicitly extend this task’s budget before adding a follow-up.')
                if task['conversation_id'] != conversation_id:
                    raise ServiceError('invalid_input', 'Follow-up target belongs to another conversation.', 422)
                if self.pending or self.requested == 'stop':
                    raise ServiceError('busy', 'An instruction or stop request is already pending. Keep this draft until it is applied.', active_task=task)
                message = self.store.message(conversation_id, body.task_id, body.text, body.client_request_id, body.intent, 'accepted')
                next_run = ('follow_up', message, body.task_id)
        # Reacquire only after transaction commit. API routes call submit under
        # the service command lock, so another request cannot reserve in this gap.
        with self.lock:
            mode, value, task_id = next_run
            if mode == 'start':
                self.active_id, self.requested, self.pending = task_id, None, None
                self.orchestrator = None
                self.future = asyncio.run_coroutine_threadsafe(self._execute('start', value), self.loop)
            else:
                self.pending = value
                if self.future and not self.future.done():
                    self.requested = 'pause'
                    self._request_status('pausing', 'Instruction accepted; waiting for the current step to finish')
                    self.loop.call_soon_threadsafe(self._apply_control, task_id, 'pause')
                else:
                    self.future = asyncio.run_coroutine_threadsafe(self._execute('pending', None), self.loop)
            return dict(message=message, task=self.store.task(task_id), duplicate=False)

    def _request_status(self, status, activity):
        task = self.store.task(self.active_id)
        task.update(status=status, current_activity=activity, updated_at=time.time())
        self.store.update_task(task, payload={'text': activity})

    def _apply_control(self, task_id, action):
        with self.lock:
            if self.active_id != task_id or not self.orchestrator:
                return
            if self.requested == 'stop':
                # Pydantic cancellation cannot drain a synchronous tool thread.
                # Close the existing action gate, then await normal loop return
                # before acknowledging Stop or allowing another task to run.
                self.orchestrator.pause()
            elif action == 'pause' and self.requested == 'pause':
                self.orchestrator.pause()

    def extend(self, task_id, body):
        with self.lock, self.store.transaction():
            task = self.store.task(task_id)
            amounts = dict(requests=body.requests, steps=body.steps, actions=body.actions)
            text = json.dumps(amounts, sort_keys=True)
            previous = self.store.accepted(body.client_request_id)
            if previous:
                if (previous['task_id'], previous['intent'], previous['text']) != (task_id, 'extend_budget', text):
                    raise ServiceError('invalid_input', 'This request ID was already used for a different operation.')
                return task
            if self.active_id != task_id or task['status'] != 'limit_reached' or (self.future and not self.future.done()):
                raise ServiceError('stale_state', 'Only an exhausted task with live state can receive more budget.')
            if not hasattr(self.orchestrator, 'extend_budget'):
                raise ServiceError('unavailable', 'This runtime does not support budget extension.', 503)
            message = self.store.message(task['conversation_id'], task_id, text, body.client_request_id, 'extend_budget', 'accepted')
            task.update(status='active', current_activity='Budget extension accepted', updated_at=time.time())
            self.store.update_task(task, payload={'text': 'Budget extension accepted'})
        with self.lock:
            self.requested = None
            self.pending_extension = message
            self.future = asyncio.run_coroutine_threadsafe(self._execute('extend', amounts), self.loop)
            return task

    def control(self, task_id, action):
        with self.lock, self.store.lock:
            task = self.store.task(task_id)
            if self.active_id != task_id:
                if action == 'stop' and task['status'] in TERMINAL:
                    return task
                raise ServiceError('stale_state', 'This task no longer has live execution state.')
            running = self.future is not None and not self.future.done()
            if action == 'resume':
                if running or task['status'] != 'paused' or self.requested == 'stop':
                    raise ServiceError('stale_state', 'Wait until the task acknowledges that it is paused.')
                self.requested = None
                self._request_status('active', 'Resuming the paused task')
                self.future = asyncio.run_coroutine_threadsafe(self._execute('resume', None), self.loop)
            elif action == 'pause':
                if self.requested == 'stop':
                    raise ServiceError('stale_state', 'The task is stopping.')
                if not running:
                    return task
                self.requested = 'pause'
                self._request_status('pausing', 'Pausing after the current operation')
                self.loop.call_soon_threadsafe(self._apply_control, task_id, 'pause')
            else:
                self.requested = 'stop'
                if self.pending:
                    with self.store.transaction():
                        self.store.delivery(self.pending['id'], 'not_applied')
                    self.pending = None
                self._request_status('stopping', 'Stopping; completed browser actions cannot be undone')
                self.loop.call_soon_threadsafe(self._apply_control, task_id, 'stop')
                if not running:
                    self.future = asyncio.run_coroutine_threadsafe(self._execute('stop', None), self.loop)
            return self.store.task(task_id)

    def _print_diagnostic(self, printer, value):
        if printer is None or not self.console_available:
            return
        try:
            printer(value)
        except (OSError, ValueError):
            # A closed output pipe must not prevent history writes or task control.
            self.console_available = False
            log.warning('AXIS terminal output is unavailable; task history remains enabled.')

    def _event(self, event):
        # Terminal diagnostics use the same full events as the CLI. Only the
        # independently allowlisted mapping below reaches SQLite/HTTP/SSE.
        self._print_diagnostic(self.event_printer, event)
        text = public_activity(event)
        if not text:
            return
        with self.lock:
            try:
                task = self.store.task(self.active_id)
                state = self.orchestrator.state
                if state and state.task_id != task['id']:
                    raise RuntimeError('Mismatched task identity.')
                if not self.requested:
                    task['current_activity'] = text
                task['updated_at'] = time.time()
                if state:
                    task['counters'] = {key: getattr(state.counters, key) for key in ('browser_actions', 'total_steps')}
                    task['duration_ms'] = int((time.time()-task['created_at'])*1000)
                    browser = state.last_browser_state
                    if browser:
                        task['browser_context'] = {'tab': browser.tab, 'title': (browser.title or 'Browser tab')[:200]}
                self.store.update_task(task, event.kind, {'text': text})
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                self._storage_failed()

    async def _execute(self, mode, value):
        task_id = self.active_id
        try:
            if mode == 'start':
                value, config = value
                self.orchestrator = self.factory(config, on_event=self._event)
            while True:
                with self.lock:
                    if self.requested == 'stop':
                        mode = 'stop'
                    if mode == 'pending':
                        pending = self.pending
                        self.pending = None
                        self.requested = None
                        value = pending['text']
                        with self.store.transaction():
                            self.store.delivery(pending['id'], 'applied')
                        self._request_status('active', 'Applying the accepted instruction')
                self._print_diagnostic(self.event_printer, AxisEvent(kind='status', detail={
                    'phase': 'ui_run_started', 'task_id': task_id,
                    'operation': 'follow_up' if mode == 'pending' else mode,
                    'instruction': value if mode in {'start', 'pending'} else None,
                    'extension': value if mode == 'extend' else None,
                }))
                if mode == 'stop':
                    if self.orchestrator:
                        self.orchestrator.cancel()
                    state = self.orchestrator.state if self.orchestrator else None
                    result = AxisResult(status='cancelled', reason='The user stopped the task. Completed browser actions are unchanged.',
                        browser_actions=state.counters.browser_actions if state else 0,
                        total_steps=state.counters.total_steps if state else 0,
                        model_requests=int(state.usage.requests) if state else 0)
                elif mode == 'start':
                    result = await self.orchestrator.start_task(value, task_id=task_id)
                elif mode == 'resume':
                    result = await self.orchestrator.resume_run()
                elif mode == 'extend':
                    with self.lock, self.store.transaction():
                        self.store.delivery(self.pending_extension['id'], 'applied')
                        self.pending_extension = None
                        task = self.store.task(task_id)
                        for field, addition in [('max_model_requests', value['requests']), ('max_total_steps', value['steps']), ('max_browser_actions', value['actions'])]:
                            task['limits'][field] += addition
                        self.store.save_task(task)
                    result = await self.orchestrator.extend_budget(value['requests'], steps=value['steps'], actions=value['actions'])
                else:
                    result = await self.orchestrator.continue_task(value)
                with self.lock:
                    if self.requested == 'stop':
                        if self.orchestrator:
                            self.orchestrator.cancel()
                        result = result.model_copy(update={'status': 'cancelled', 'reason': 'The user stopped the task. Completed browser actions are unchanged.'})
                    # A completion racing a requested pause may return completed.
                    # It is still safe to continue only here, after loop return.
                    if self.pending and self.requested != 'stop' and result.status not in {'failed', 'cancelled', 'limit_reached'}:
                        mode = 'pending'
                        continue
                    if self.pending:
                        with self.store.transaction():
                            self.store.delivery(self.pending['id'], 'not_applied')
                        self.pending = None
                    self._finish(task_id, result)
                    break
        except Exception as error:
            # Full unexpected exceptions are local, opt-in terminal diagnostics.
            # The persisted/client error below remains independent and concise.
            log.error('AXIS UI runner failed (%s).', type(error).__name__, exc_info=self.debug)
            try:
                with self.lock:
                    self._finish(task_id, AxisResult(status='failed', reason='AXIS could not finish this task. Check the browser bridge and local provider configuration, then start a new task.'))
            except (OSError, sqlite3.Error):
                self._storage_failed()

    def _finish(self, task_id, result):
        task = self.store.task(task_id)
        task.update(status=result.status, current_activity=result.reason[:1000], result=safe_result(result),
                    owns_runtime=result.status not in TERMINAL or result.status == 'limit_reached',
                    updated_at=time.time(), ended_at=time.time() if result.status in TERMINAL else None,
                    counters={'browser_actions': result.browser_actions, 'total_steps': result.total_steps}, duration_ms=int((time.time()-task['created_at'])*1000))
        state = self.orchestrator.state if self.orchestrator else None
        if state:
            outputs = [item for item in state.evidence if item.kind in {'screenshot', 'download'}]
            outputs += state.downloads
            task['artifacts'] = [{'label': item.detail[:600], 'kind': item.kind} for item in outputs[-6:]]
        with self.store.transaction():
            if getattr(self, 'pending_extension', None):
                self.store.delivery(self.pending_extension['id'], 'not_applied')
                self.pending_extension = None
            if self.pending:
                self.store.delivery(self.pending['id'], 'not_applied')
                self.pending = None
            self.store.update_task(task, 'final', {'text': result.reason[:1000]})
            if result.answer:
                text = result.answer[:64000]
                if len(result.answer) > 64000:
                    text += '\n[Display limited to 64,000 characters.]'
                self.store.message(task['conversation_id'], task_id, text, None, 'outcome', role='assistant')
        self._print_diagnostic(self.result_printer, result)
        self.requested = None
        if result.status in TERMINAL and result.status != 'limit_reached':
            self.active_id = None

    def close(self):
        with self.lock:
            self.closed = True
            if self.active_id:
                self.control(self.active_id, 'stop')
        if self.future:
            try:
                self.future.result(timeout=5)
            except TimeoutError:
                return False  # caller must leave storage open until process exits
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()
        return True
