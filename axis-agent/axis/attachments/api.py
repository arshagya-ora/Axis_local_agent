"""Authenticated binary uploads; each request contains one file, streamed to disk."""
import asyncio
from pathlib import Path
import tempfile

from starlette.responses import JSONResponse
from starlette.routing import Route

from axis.ui_store import ServiceError
from .parsing import MAX_FILE_BYTES


def routes(attachments):
    async def collection(request):
        conversation_id = request.path_params['conversation_id']
        if request.method == 'GET':
            return JSONResponse({'items': attachments.list(conversation_id)})
        filename = request.query_params.get('filename', '')
        with attachments.history.lock:
            attachments.history.conversation(conversation_id)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=attachments.directory, prefix='upload-', suffix='.part', delete=False) as file:
                staged = Path(file.name)
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ServiceError('file_too_large', 'Files must be 25 MiB or smaller.', 413)
                    file.write(chunk)
            if not size:
                raise ServiceError('empty_file', 'Choose a nonempty file.', 422)
            item = await asyncio.to_thread(attachments.accept, conversation_id, filename, staged, size)
            return JSONResponse(item, status_code=201)
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    async def item(request):
        identifier = request.path_params['attachment_id']
        if request.method == 'DELETE':
            attachments.delete(identifier)
            return JSONResponse({'deleted': True})
        return JSONResponse(attachments.get(identifier))

    return [Route('/api/conversations/{conversation_id}/attachments', collection, methods=['GET', 'POST']),
            Route('/api/attachments/{attachment_id}', item, methods=['GET', 'DELETE'])]
