import uuid
import os
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from .context_manager import context_manager

logger = logging.getLogger(__name__)

# High-frequency paths that spam logs at INFO level — downgrade to DEBUG
SILENT_PATHS = {"/health", "/webhook/delivery"}

def get_request_id(request):
    return request.state.request_id

class ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        worker_pid = os.getpid()
        request_start = time.time()
        path = request.url.path
        log_fn = logger.debug if path in SILENT_PATHS else logger.info

        log_fn(f"[WORKER-{worker_pid}] [REQ-START] {request.method} {path} | Request ID: {request_id[:8]}")

        try:
            response = await call_next(request)
            request_duration = time.time() - request_start
            log_fn(f"[WORKER-{worker_pid}] [REQ-END] {request.method} {path} | Duration: {request_duration:.3f}s | Status: {response.status_code}")
            return response
        except Exception as e:
            request_duration = time.time() - request_start
            logger.error(f"[WORKER-{worker_pid}] [REQ-ERROR] {request.method} {path} | Duration: {request_duration:.3f}s | Error: {e}")
            raise
        finally:
            context_manager.clear(request_id)