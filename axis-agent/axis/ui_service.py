"""Authenticated loopback UI API. Run: python -m axis.ui_service --help."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

from axis.models import AxisConfig, AttachmentRef
from axis.attachments.api import routes as attachment_routes
from axis.attachments.store import AttachmentStore
from axis.console import configure_console
from axis.ownership import RuntimeOwnership
from axis.ui_runner import Runner
from axis.ui_store import Store, ServiceError

DATA_DIR = Path(__file__).resolve().parents[1] / '.axis-ui'
EXTENSION_MANIFEST = Path(__file__).resolve().parents[2] / 'browser-agent-bridge-main' / 'extension' / 'manifest.json'


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class MessageInput(Input):
    text: str = Field(min_length=1, max_length=4000)
    intent: Literal['new_task', 'follow_up']
    client_request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    task_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    context_task_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=8)
    execution_mode: Literal['approval', 'automatic'] = 'approval'


class ModeInput(Input):
    execution_mode: Literal['approval', 'automatic']


class ApprovalInput(Input):
    decision: Literal['approve', 'deny']


class ConversationInput(Input):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')


class TitleInput(Input):
    title: str = Field(min_length=1, max_length=120)


class ControlInput(Input):
    action: Literal['stop', 'pause', 'resume', 'extend']
    requests: int | None = Field(default=None, ge=1, le=10000, strict=True)
    steps: int = Field(default=0, ge=0, le=10000, strict=True)
    actions: int = Field(default=0, ge=0, le=10000, strict=True)
    client_request_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{16,80}$')


class LimitsInput(Input):
    max_total_steps: int | None = Field(default=None, ge=1, le=10000, strict=True)
    max_browser_actions: int | None = Field(default=None, ge=1, le=10000, strict=True)
    max_model_requests: int | None = Field(default=None, ge=1, le=10000, strict=True)


class SettingsInput(Input):
    run: LimitsInput


async def read_input(request, model):
    try:
        return model.model_validate(json.loads(await request.body()))
    except (ValueError, ValidationError):
        # Avoid Pydantic's input dump, which may contain a credential pasted in error.
        raise ServiceError('invalid_input', 'Check the supplied fields and their allowed values.', 422) from None


def positive_query(request, key, default):
    try:
        value = int(request.query_params.get(key, default))
        if not 0 <= value <= 2**63-1:
            raise ValueError()
        return value
    except ValueError:
        raise ServiceError('invalid_input', f'{key} must be a nonnegative integer.', 422) from None


class LocalClientMiddleware:
    def __init__(self, app, *, token, origin, port):
        self.app, self.token, self.origin, self.host = app, token, origin, f'127.0.0.1:{port}'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        origin = headers.get(b'origin', b'').decode('latin-1')
        host = headers.get(b'host', b'').decode('latin-1')
        async def reply(status, code, message):
            await JSONResponse({'code': code, 'message': message}, status_code=status)(scope, receive, send)
        if host != self.host or (origin and origin != self.origin):
            return await reply(403, 'forbidden', 'This client origin or host is not allowed.')
        async def send_cors(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]
                if origin == self.origin:
                    message['headers'] += [(b'access-control-allow-origin', origin.encode()), (b'vary', b'Origin'),
                        (b'access-control-allow-methods', b'GET, POST, PATCH, DELETE, OPTIONS'),
                        (b'access-control-allow-headers', b'Authorization, Content-Type'),
                        (b'access-control-allow-private-network', b'true')]
            await send(message)
        if scope['method'] == 'OPTIONS':
            return await JSONResponse({}, status_code=200)(scope, receive, send_cors)
        credential = headers.get(b'authorization', b'').decode('latin-1')
        if not hmac.compare_digest(credential, f'Bearer {self.token}'):
            return await JSONResponse({'code': 'unauthorized', 'message': 'Pairing credential was rejected.'}, status_code=401)(scope, receive, send_cors)
        if scope['method'] == 'POST' and re.fullmatch(r'/api/conversations/[a-zA-Z0-9_-]{16,80}/attachments', scope['path']):
            # The attachment route enforces its own byte limit while streaming.
            return await self.app(scope, receive, send_cors)
        # Bound even chunked request bodies before parsing or entering routes.
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > 32768:
                return await reply(413, 'invalid_input', 'Request exceeds 32 KiB.')
            if not message.get('more_body'):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        return await self.app(scope, bounded_receive, send_cors)


def create_app(store, runner, *, token, origin, port=8766):
    commands = threading.RLock()
    import tempfile
    temporary = tempfile.TemporaryDirectory(prefix='axis-attachments-') if store.path is None else None
    attachments = AttachmentStore(store, Path(temporary.name) if temporary else store.path.parent / 'attachments')
    runner.attachments = attachments

    async def status(request):
        with store.lock:
            cursor = store.cursor()
        return JSONResponse(dict(ready=not runner.storage_error, storage_error=runner.storage_error,
                                 active_task=runner.active(), cursor=cursor,
                                 capabilities={'pause': True, 'follow_up': True, 'extend_budget': True, 'history': True, 'restart_recovery': False, 'attachments': True, 'workflow_recovery': True}))

    async def conversations(request):
        if request.method == 'POST':
            body = await read_input(request, ConversationInput)
            return JSONResponse(store.create_conversation(body.id), status_code=201)
        return JSONResponse(store.conversations(request.query_params.get('search', '')[:120], positive_query(request, 'offset', 0)))

    async def conversation(request):
        conversation_id = request.path_params['conversation_id']
        if request.method == 'GET':
            return JSONResponse(store.snapshot(conversation_id, positive_query(request, 'before', 0)))
        body = await read_input(request, TitleInput) if request.method == 'PATCH' else None
        with commands, runner.lock, store.transaction():
            store.conversation(conversation_id)
            if body:
                store.db.execute('UPDATE conversations SET title=? WHERE id=?', (body.title, conversation_id))
                store.event(conversation_id, None, 'conversation', {'title': body.title})
                return JSONResponse(store.conversation(conversation_id))
            active = runner.active()
            if active and active['conversation_id'] == conversation_id:
                raise ServiceError('busy', 'Stop the active or paused task before deleting this conversation.', active_task=active)
            store.db.execute('INSERT OR IGNORE INTO retired_requests SELECT client_request_id FROM messages WHERE conversation_id=? AND client_request_id IS NOT NULL', (conversation_id,))
            store.db.execute('DELETE FROM conversations WHERE id=?', (conversation_id,))
            store.db.execute('DELETE FROM embedding_cache')
        await asyncio.to_thread(attachments._collect_orphans)
        return JSONResponse({'deleted': True})

    async def submit(request):
        body = await read_input(request, MessageInput)
        if (body.intent == 'follow_up') != (body.task_id is not None) or (body.intent == 'follow_up' and body.context_task_id):
            raise ServiceError('invalid_input', 'Follow-ups require an explicit live task target.', 422)
        with commands:
            result = runner.submit(request.path_params['conversation_id'], body)
        return JSONResponse(result, status_code=200 if result['duplicate'] else 202)

    async def task(request):
        with store.lock:
            return JSONResponse(store.task(request.path_params['task_id']))

    async def request_status(request):
        # Lookup is independent of conversation paging and never starts work.
        with store.lock:
            try:
                message = store.accepted(request.path_params['request_id'])
            except ServiceError as error:
                if error.code != 'stale_state':
                    raise
                return JSONResponse({'status': 'retired'})
            if message is None:
                return JSONResponse({'status': 'unknown'})
            return JSONResponse({'status': 'accepted', 'message': message,
                                 'task': store.task(message['task_id'])})

    async def control(request):
        body = await read_input(request, ControlInput)
        with commands:
            if body.action == 'extend':
                if body.requests is None or body.client_request_id is None:
                    raise ServiceError('invalid_input', 'Budget extension requires additional requests and a request ID.', 422)
                return JSONResponse(runner.extend(request.path_params['task_id'], body))
            if body.requests is not None or body.steps or body.actions or body.client_request_id:
                raise ServiceError('invalid_input', 'Budget fields are only valid for an explicit extension.', 422)
            return JSONResponse(runner.control(request.path_params['task_id'], body.action))

    async def activity(request):
        return JSONResponse(store.activity(request.path_params['task_id'], positive_query(request, 'before', 0)))

    async def execution_mode(request):
        body = await read_input(request, ModeInput)
        with commands:
            return JSONResponse(runner.change_mode(request.path_params['task_id'], body.execution_mode))

    async def execution_approval(request):
        body = await read_input(request, ApprovalInput)
        with commands:
            return JSONResponse(runner.approvals.decide(request.path_params['task_id'], request.path_params['approval_id'], body.decision))

    async def events(request):
        after = positive_query(request, 'after', 0)
        async def stream():
            cursor = after
            while not runner.closed:
                with store.lock:
                    latest = store.cursor()
                    backlog = store.db.execute('SELECT count(*) FROM (SELECT sequence FROM events WHERE sequence>? LIMIT 1001)', (cursor,)).fetchone()[0]
                if cursor > latest or backlog > 1000:
                    yield 'data: ' + json.dumps({'type': 'resync', 'sequence': latest}) + '\n\n'
                    return
                batch = store.events(cursor)
                if batch:
                    for event in batch:
                        cursor = event['sequence']
                        yield f'id: {cursor}\ndata: {json.dumps(event)}\n\n'
                    await asyncio.sleep(0)
                else:
                    yield ': keepalive\n\n'
                    await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    async def settings(request):
        if request.method == 'PATCH':
            body = await read_input(request, SettingsInput)
            changes = body.run.model_dump(exclude_none=True)
            if not changes:
                raise ServiceError('invalid_input', 'Supply at least one run limit.', 422)
            with commands, runner.lock:
                raw = runner.config.model_dump()
                raw['run'].update(changes)
                config = AxisConfig.model_validate(raw)
                saved = store.get_settings()
                saved.update(changes)
                store.set_settings(saved)
                runner.config = config
        config = runner.config
        model = config.provider.model or os.environ.get('AXIS_MODEL') or os.environ.get('AXIS_OCI_GENAI_MODEL') or 'Configured provider'
        return JSONResponse({'model': model[:200], 'run': config.run.model_dump(), 'applies_to': 'future_tasks'})

    async def service_error(request, error):
        return JSONResponse({'code': error.code, 'message': str(error), **error.detail}, status_code=error.status)

    async def storage_error(request, error):
        # Database failure is visible independently of the optional event callback.
        with runner.lock:
            runner.storage_error = 'History storage is unavailable. Check disk access and restart AXIS.'
            runner.requested = 'stop'
            if runner.active_id:
                runner.loop.call_soon_threadsafe(runner._apply_control, runner.active_id, 'stop')
        return JSONResponse({'code': 'storage_error', 'message': runner.storage_error}, status_code=503)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if await asyncio.to_thread(runner.close):
            await asyncio.to_thread(attachments.close)
            store.close()
            if temporary:
                temporary.cleanup()

    app = Starlette(routes=[
        *attachment_routes(attachments),
        Route('/api/status', status), Route('/api/conversations', conversations, methods=['GET', 'POST']),
        Route('/api/requests/{request_id}', request_status),
        Route('/api/conversations/{conversation_id}', conversation, methods=['GET', 'PATCH', 'DELETE']),
        Route('/api/conversations/{conversation_id}/messages', submit, methods=['POST']),
        Route('/api/tasks/{task_id}', task), Route('/api/tasks/{task_id}/control', control, methods=['POST']),
        Route('/api/tasks/{task_id}/events', activity), Route('/api/events', events),
        Route('/api/tasks/{task_id}/execution-mode', execution_mode, methods=['PATCH']),
        Route('/api/tasks/{task_id}/approvals/{approval_id}', execution_approval, methods=['POST']),
        Route('/api/settings', settings, methods=['GET', 'PATCH'])
    ], exception_handlers={ServiceError: service_error, sqlite3.Error: storage_error}, lifespan=lifespan)
    app.add_middleware(LocalClientMiddleware, token=token, origin=origin, port=port)
    return app


def extension_id():
    import base64
    manifest = json.loads(EXTENSION_MANIFEST.read_text(encoding='utf-8'))
    digest = hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
    return ''.join(chr(ord('a')+int(char, 16)) for char in digest)


def pairing(path, origin):
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('origin') != origin or len(value.get('token', '')) < 32:
            raise ValueError('Pairing file does not match this extension. Use a new data directory or explicitly remove the old pairing file to rotate it.')
        return value['token']
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump({'origin': origin, 'token': token}, file)
    return token


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--extension-id', default=None)
    parser.add_argument('--config', default=None)
    parser.add_argument('--data-dir', type=Path, default=DATA_DIR)
    parser.add_argument('--verbose', action='store_true', help='Print compact agent events and final result totals in the terminal.')
    parser.add_argument('--debug', action='store_true', help='Print the full CLI-style agent trace, tool arguments and outcomes, evidence, results, usage and timing in the terminal. Includes unexpected runner tracebacks.')
    args = parser.parse_args(argv)
    identifier = args.extension_id or extension_id()
    if not re.fullmatch('[a-p]{32}', identifier) or not 1024 <= args.port <= 65535:
        parser.error('Use a valid Chrome extension ID and a port from 1024 to 65535.')
    origin = 'chrome-extension://' + identifier
    with RuntimeOwnership():
        token = pairing(args.data_dir / 'pairing.json', origin)
        config = AxisConfig.load(args.config)
        store = Store(args.data_dir / 'history.sqlite3')
        store.interrupt()
        raw = config.model_dump()
        raw['run'].update(store.get_settings())
        config = AxisConfig.model_validate(raw)
        runner = Runner(store, config, debug=args.debug, verbose=args.verbose)
        app = create_app(store, runner, token=token, origin=origin, port=args.port)
        print(f'AXIS UI service: http://127.0.0.1:{args.port}\nPairing file: {args.data_dir / "pairing.json"}\nExtension: {identifier}', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='debug' if args.debug else 'warning')


if __name__ == '__main__':
    main()
