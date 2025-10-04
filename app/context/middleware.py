import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from .context_manager import context_manager

def get_request_id(request):
    return request.state.request_id

class ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        finally:
            # cleanup after request
            context_manager.clear(request_id)

        return response