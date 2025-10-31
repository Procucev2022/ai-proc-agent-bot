import uuid
import os
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from .context_manager import context_manager
from app.utils.logging_utils import set_user_phone_context, clear_user_phone_context

logger = logging.getLogger(__name__)

def get_request_id(request):
    return request.state.request_id

async def extract_phone_from_request(request):
    """Extract phone number from webhook request for logging context"""
    try:
        if request.url.path == "/webhook/whatsapp" and request.method == "POST":
            # Try to get phone from form data (ICS webhook format)
            if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                body = await request.body()
                from urllib.parse import parse_qs
                form_data = parse_qs(body.decode())
                customer_number = form_data.get("customernumber", [None])[0]
                if customer_number:
                    return customer_number
        elif request.url.path == "/webhook/delivery":
            # Extract from delivery callback
            mobile = request.query_params.get("qMobile")
            if mobile:
                return mobile
    except Exception:
        pass
    return None

class ContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Extract phone number for logging context
        phone_number = await extract_phone_from_request(request)
        if phone_number:
            set_user_phone_context(phone_number)

        # Track worker and timestamp
        worker_pid = os.getpid()
        request_start = time.time()

        # Log request start with worker info
        logger.info(f"[WORKER-{worker_pid}] [REQ-START] {request.method} {request.url.path} | Request ID: {request_id[:8]}")

        try:
            response = await call_next(request)
            request_duration = time.time() - request_start
            logger.info(f"[WORKER-{worker_pid}] [REQ-END] {request.method} {request.url.path} | Duration: {request_duration:.3f}s | Status: {response.status_code}")
            return response
        except Exception as e:
            request_duration = time.time() - request_start
            logger.error(f"[WORKER-{worker_pid}] [REQ-ERROR] {request.method} {request.url.path} | Duration: {request_duration:.3f}s | Error: {e}")
            raise
        finally:
            # cleanup after request
            context_manager.clear(request_id)
            clear_user_phone_context()