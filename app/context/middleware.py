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
            content_type = request.headers.get("content-type", "")
            
            if "application/x-www-form-urlencoded" in content_type:
                # ICS webhook format: replytype, customernumber, replymessage, timestamp, wabanumber, mid, smsgid
                form_data = await request.form()
                customer_number = form_data.get("customernumber")
                if customer_number:
                    return customer_number
            elif "application/json" in content_type:
                # JSON webhook format (if any)
                json_data = await request.json()
                # Extract phone from JSON structure if present
                if "from" in json_data:
                    return json_data["from"]
        elif request.url.path == "/webhook/delivery":
            # ICS delivery callback: qStatus, qMobile, qMsgRef, qDTime, SMSMSGID, SENDERID, NOTES
            mobile = request.query_params.get("qMobile")
            if mobile:
                return mobile
    except Exception as e:
        logger.error(f"Phone extraction error: {e}")
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