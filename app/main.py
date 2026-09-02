"""
FastAPI application entry point and initialization.

This module serves as the main entry point for the AI Procurement Agent application.
It initializes the FastAPI application, configures middleware, and includes API routes
for handling WhatsApp webhook endpoints and health checks.

Key responsibilities:
- FastAPI application setup and configuration
- CORS middleware configuration for web requests
- API route registration (webhook endpoints)
- Application lifecycle management
- Health check endpoint for monitoring
"""

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
import asyncio
from contextlib import asynccontextmanager
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from typing import Dict, Any, Optional, Union
from app.config import get_settings
from app.api.webhook import router as webhook_router
from app.api.dashboard import (
    router as dashboard_router,
    DASHBOARD_SESSION_COOKIE,
    DASHBOARD_SESSION_MAX_AGE,
    build_dashboard_session_token,
    is_valid_dashboard_credential,
)
from app.database import init_database, get_db_session_context
from app.services.chat_service import ChatService
from app.services.global_error_handler import handle_server_error
from app.context.middleware import ContextMiddleware
from app.utils.logging_utils import setup_basic_logging, CustomFormatter
from app.utils.loop_monitor import get_loop_monitor
from app.utils.turn_trace import start_turn
from unittest.mock import patch
import gc
import time

# Get settings and configure logging with custom formatter
settings = get_settings()
setup_basic_logging(level=settings.log_level)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter_storage = settings.redis_url if (settings.redis_url and getattr(settings, "redis_session_storage_enabled", True)) else "memory://"
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=limiter_storage,
    default_limits=[settings.rate_limit_default]
)



class IPRestrictionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: list):
        super().__init__(app)
        self.allowed_ips = allowed_ips

    async def dispatch(self, request: Request, call_next):
        if self.allowed_ips:
            # request.client is None for connections with no peer address (ASGI
            # test transports, some proxies). Reading .host unguarded would raise
            # here and reject the request with a 500 instead of an IP decision.
            client_ip = request.client.host if request.client else ""
            x_forwarded_for = request.headers.get("x-forwarded-for")
            x_real_ip = request.headers.get("x-real-ip")

            real_ip = x_real_ip or (x_forwarded_for.split(",")[0] if x_forwarded_for else client_ip)

            if real_ip not in self.allowed_ips:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access forbidden: IP not allowed"}
                )

        response = await call_next(request)
        return response


async def _warmup_request_path() -> None:
    """
    Open the connections the first reply needs, before a user is waiting on them.

    Several clients on the message path build themselves on first use: the Redis
    pool, the pooled HTTP session to the WhatsApp gateway, and the database
    connection pool. Left alone, the first user after a restart pays for all of
    those handshakes inside their own turn -- and a restart is exactly when a
    first "Hi" arrives, because a scaled-to-zero replica is started *by* that
    message.

    Each step is independent and best-effort: warmup is an optimisation, so a
    failure here is logged and the lazy path still runs normally. Timings are
    logged individually so a slow dependency at boot is attributable.
    """
    async def _warm(label: str, coro_factory) -> None:
        started = time.time()
        try:
            await coro_factory()
            logger.info(f"[BOOT] [WARMUP] {label} ready in {(time.time() - started) * 1000:.0f}ms")
        except Exception as exc:
            logger.warning(
                f"[BOOT] [WARMUP] {label} failed after {(time.time() - started) * 1000:.0f}ms "
                f"({type(exc).__name__}: {exc}); it will initialise lazily on first use"
            )

    async def warm_redis():
        from app.redis_db import get_redis_service
        await get_redis_service().set("warmup:ping", "1", ex=30)

    async def warm_whatsapp_http():
        from app.services.whatsapp_service import get_gateway_session
        await get_gateway_session()

    # The database pool is deliberately not warmed here: init_database() above
    # already opens a session and runs a query, so the pool and its TLS handshake
    # are done by the time this runs.
    logger.info("[BOOT] [WARMUP] Priming the request path so the first reply is not slower than the rest...")
    await _warm("Redis connection", warm_redis)
    await _warm("WhatsApp gateway HTTP pool", warm_whatsapp_http)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("=" * 60)
    logger.info(f"[BOOT] Starting App: {settings.app_name} ({settings.environment})")
    logger.info(f"[BOOT] Database Mode: {settings.database_mode}")
    logger.info("[BOOT] Redis configured")
    logger.info(f"[BOOT] Chroma Host: {settings.chroma_host}:{settings.chroma_port}")
    logger.info(f"[BOOT] Azure OpenAI Endpoint: {settings.azure_openai_base_url}")
    logger.info("=" * 60)

    # Initialize database on startup
    try:
        logger.info("[BOOT] Initializing database...")
        init_database()
        logger.info("[BOOT] ✓ Database initialized successfully")
    except Exception as e:
        logger.critical(f"[BOOT] Database initialization failed: {e}", exc_info=True)
        raise

    # Initialize global ProcucevAPIClient
    try:
        logger.info("[BOOT] Initializing ProcucevAPIClient...")
        from app.procucev_apis.procucev_api_client import init_procucev_api_client
        await init_procucev_api_client()
        logger.info("[BOOT] ✓ ProcucevAPIClient initialized successfully")
    except Exception as e:
        logger.warning(f"[BOOT] ⚠️ Failed to initialize ProcucevAPIClient: {e}")

    # Start message queue background tasks (only if Redis is enabled)
    message_queue_tasks = []
    if getattr(settings, "redis_session_storage_enabled", True):
        try:
            logger.info("[BOOT] Starting message queue background tasks...")
            from app.api.webhook import message_queue_service

            # Start batch poller (creates batches from incoming messages)
            poller_task = asyncio.create_task(message_queue_service.run_batch_poller(), name="batch_poller")
            message_queue_tasks.append(poller_task)
            if hasattr(message_queue_service, "_background_tasks"):
                message_queue_service._background_tasks.append(poller_task)
            logger.info("[BOOT] ✓ Message queue batch poller started")

            # Start monitoring loop (acknowledgments and please-wait messages)
            monitor_task = asyncio.create_task(message_queue_service.run_monitoring_loop(), name="monitoring_loop")
            message_queue_tasks.append(monitor_task)
            if hasattr(message_queue_service, "_background_tasks"):
                message_queue_service._background_tasks.append(monitor_task)
            logger.info("[BOOT] ✓ Message queue monitoring loop started")

        except Exception as e:
            logger.warning(f"[BOOT] ⚠️ Failed to start message queue background tasks: {e}")

    # Start webhook health monitoring
    webhook_monitor_task = None
    webhook_monitor = None
    if settings.webhook_health_monitoring_enabled:
        try:
            from app.services.webhook_health_monitor_service import WebhookHealthMonitorService
            webhook_monitor = WebhookHealthMonitorService()
            webhook_monitor_task = asyncio.create_task(webhook_monitor.start_monitoring())
            logger.info("Webhook health monitoring started")
        except Exception as e:
            logger.error(f"Failed to start webhook health monitoring: {e}")
            # Continue without monitoring rather than failing startup

    # Start inactivity timeout monitoring (optimized for multi-worker, only if Redis is enabled)
    timeout_service = None
    if getattr(settings, "redis_session_storage_enabled", True):
        try:
            from app.services.inactivity_timeout_service import get_timeout_service
            timeout_service = get_timeout_service()  # Use singleton

            # Optimized: Only start monitor if not already running on another worker
            if await timeout_service.try_start_monitoring_if_available():
                logger.info("Inactivity timeout monitoring started on this worker")
            else:
                logger.info("Inactivity timeout monitoring already running on another worker")
        except Exception as e:
            logger.error(f"Failed to start timeout monitoring: {e}")
            # Continue without timeout monitoring rather than failing startup

    # NOTE: AutoCategorizationService preload removed to reduce memory usage
    # Service now lazy-loads on first use per worker (registration or Celery tasks)
    # This saves ~100-120MB RAM per worker during startup
    logger.info("AutoCategorizationService will lazy-load on first use")

    # Watch for anything blocking the event loop. Every user's turn, the batch
    # poller and the webhook all share one thread, so a single synchronous call
    # stalls all of them at once - a failure mode that is otherwise invisible
    # because the stalled code never gets to report its own slowness.
    loop_monitor = get_loop_monitor()
    try:
        loop_monitor.start()
    except Exception as e:
        logger.warning(f"[BOOT] Could not start the event loop monitor: {e}")

    # Warm the connections a reply needs, while the container is still starting
    # rather than inside the first user's turn.
    await _warmup_request_path()

    yield

    logger.info("Shutting down AI Procurement Agent application")

    # Shutdown ProcucevAPIClient gracefully
    try:
        from app.procucev_apis.procucev_api_client import close_procucev_api_client
        await close_procucev_api_client()
        logger.info("ProcucevAPIClient shut down successfully")
    except Exception as e:
        logger.error(f"Error shutting down ProcucevAPIClient: {e}")

    # Shutdown message queue service gracefully
    try:
        from app.api.webhook import message_queue_service
        await message_queue_service.shutdown()
        logger.info("Message queue service shut down successfully")
    except Exception as e:
        logger.error(f"Error shutting down message queue service: {e}")

    # Stop webhook health monitoring gracefully
    if webhook_monitor_task and webhook_monitor:
        try:
            await webhook_monitor.stop_monitoring()
            await asyncio.wait_for(webhook_monitor_task, timeout=5.0)
            logger.info("Webhook health monitoring stopped")
        except asyncio.TimeoutError:
            logger.warning("Health monitoring shutdown timeout")
            webhook_monitor_task.cancel()
            # Extra safety: ensure session is closed even after timeout
            try:
                await webhook_monitor._close_session()
                logger.info("Webhook monitor session closed after timeout")
            except Exception as session_error:
                logger.warning(f"Error closing webhook monitor session: {session_error}")
        except Exception as e:
            logger.error(f"Error stopping health monitoring: {e}")
            # Ensure session cleanup even on exception
            try:
                await webhook_monitor._close_session()
            except Exception:
                pass

    # Stop timeout monitoring gracefully
    if timeout_service:
        try:
            await timeout_service.stop_monitoring()
            logger.info("Inactivity timeout monitoring stopped")
        except Exception as e:
            logger.error(f"Error stopping timeout monitoring: {e}")

    # Stop the event loop monitor
    try:
        await get_loop_monitor().stop()
    except Exception as e:
        logger.warning(f"Error stopping event loop monitor: {e}")

    # Close the pooled WhatsApp gateway session explicitly, before the sweep below
    # so it is shut down cleanly rather than collected by the generic scan.
    try:
        from app.services.whatsapp_service import close_gateway_session
        await close_gateway_session()
    except Exception as e:
        logger.warning(f"Error closing WhatsApp gateway session: {e}")

    # Cleanup any remaining aiohttp sessions
    import aiohttp
    for obj in gc.get_objects():
        if isinstance(obj, aiohttp.ClientSession) and not obj.closed:
            try:
                await obj.close()
                logger.info("Closed remaining aiohttp session")
            except Exception as e:
                logger.warning(f"Error closing session: {e}")


# Initialize FastAPI application
app = FastAPI(
    title="AI Procurement Agent",
    description="WhatsApp-based AI procurement assistant",
    version="1.0.0",
    lifespan=lifespan,
    debug=settings.DEBUG
)

# Add rate limiting error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


# Add global exception handler for unhandled server errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unhandled exceptions globally."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    # Extract user info from request if available
    user_phone = None
    try:
        if hasattr(request, 'json'):
            body = await request.json()
            user_phone = body.get('phone') or body.get('from')
    except:
        pass

    # Notify support team about server error
    try:
        await handle_server_error(
            error_message=f"Unhandled exception: {str(exc)}",
            user_phone=user_phone,
            current_flow=f"{request.method} {request.url.path}"
        )
    except Exception as notify_error:
        logger.error(f"Failed to notify support team about server error: {notify_error}")

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error. Our team has been notified.",
            "error_id": str(id(exc))
        }
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        path = request.url.path
        method = request.method
        client_ip = request.client.host if request.client else "unknown"

        logger.info(f"[HTTP-IN] {method} {path} from {client_ip}")
        try:
            response = await call_next(request)
            duration_ms = round((time.time() - start_time) * 1000, 2)
            logger.info(f"[HTTP-OUT] {method} {path} -> {response.status_code} ({duration_ms}ms)")
            return response
        except Exception as e:
            duration_ms = round((time.time() - start_time) * 1000, 2)
            logger.error(f"[HTTP-ERR] {method} {path} failed: {e} ({duration_ms}ms)", exc_info=True)
            raise


# Add context middleware (must be first)
app.add_middleware(ContextMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# License validation is handled in SessionManagementService (not middleware)
# This ensures license checks happen at session creation time, not on every request
if settings.license_enabled:
    # Validate license at startup so we know immediately if there's a problem
    from app.license import validate_license
    is_valid, message = validate_license()
    if is_valid:
        logger.info(f"License validation enabled — {message}")
    else:
        logger.warning(f"License validation enabled but FAILED — {message}")
else:
    logger.info("License validation disabled (LICENSE_ENABLED=false)")

# Add IP restriction middleware
if settings.allowed_ips:
    app.add_middleware(IPRestrictionMiddleware, allowed_ips=settings.allowed_ips)

# Configure CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
import os

# Mount static files and templates
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
logger.info(f"Templates path: {TEMPLATES_DIR} , ")
# templates = Jinja2Templates(directory="templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Include API routers
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
app.include_router(dashboard_router)

# Don't create global ChatService - create per-request with proper session management
# chat_service = ChatService()  # REMOVED: Causes database connection leaks

# Pydantic models for chat API
from typing import Union


class ChatMessage(BaseModel):
    message: Union[str, Dict[str, Any]]
    phone: str = "919876543229"


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "AI Procurement Agent",
            "version": "1.0.0"
        }
    )


@app.get("/")
async def root():
    """Root endpoint with basic information."""
    return JSONResponse(
        content={
            "message": "AI Procurement Agent API",
            "docs": "/docs",
            "health": "/health",
            "chat": "/chat",
            "dashboard": "/dashboard"
        }
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Serve the chat UI page."""
    return templates.TemplateResponse(request, "chat.html")


def _set_dashboard_cookie(response, api_key: Optional[str], request: Request):
    if not api_key:
        return response
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        build_dashboard_session_token(api_key),
        max_age=DASHBOARD_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _serve_dashboard_template(request: Request, template_name: str):
    """Render a dashboard page for an authenticated operator and refresh the cookie."""
    api_key = get_settings().dashboard_api_key
    provided_key = request.headers.get("X-Dashboard-Key")
    session_cookie = request.cookies.get(DASHBOARD_SESSION_COOKIE)
    if not is_valid_dashboard_credential(api_key, provided_key, session_cookie):
        raise HTTPException(status_code=401, detail="Dashboard authentication required")

    return _set_dashboard_cookie(
        templates.TemplateResponse(request, template_name), api_key, request
    )


class DashboardLoginRequest(BaseModel):
    key: str


@app.post("/dashboard/login")
async def dashboard_login(request: Request, credentials: DashboardLoginRequest):
    """Authenticate an operator without placing the key in a URL."""
    api_key = get_settings().dashboard_api_key
    if not is_valid_dashboard_credential(api_key, credentials.key, None):
        raise HTTPException(status_code=401, detail="Dashboard authentication required")
    return _set_dashboard_cookie(
        JSONResponse(content={"status": "success"}), api_key, request
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Serve the Real-Time Analytics Dashboard UI."""
    return _serve_dashboard_template(request, "dashboard.html")


@app.get("/dashboard/classification-details", response_class=HTMLResponse)
async def classification_details_page(request: Request):
    """Serve the User Classification Details page."""
    return _serve_dashboard_template(request, "classification_details.html")


@app.post("/api/chat")
@limiter.limit(settings.rate_limit_chat)
async def process_chat_message(request: Request, chat_message: ChatMessage):
    """Process chat message through ChatService - works exactly like test_multi_turn_conversation.py"""
    from app.utils.logging_utils import UserPhoneContext

    request_start = time.time()

    # Set phone number context for all logs in this request
    async with UserPhoneContext(chat_message.phone):
        # Open a turn so this endpoint produces the same stage-by-stage breakdown
        # as a real WhatsApp message. It runs the identical pipeline with only the
        # outbound sends mocked, which makes it the cheapest way to attribute a
        # slow turn without waiting on the gateway. Without this the [TURN] lines
        # emitted inside process_message would have no id and no summary.
        trace = start_turn(
            chat_message.phone,
            source="api_chat",
            message_preview=str(chat_message.message),
        )
        try:
            t1 = time.time()
            logger.info(f"[PERF] Request parsing completed: {(t1 - request_start) * 1000:.0f}ms")

            # Store captured WhatsApp messages and interactive buttons
            whatsapp_messages = []
            interactive_buttons = []

            # Mock the WhatsApp service to capture messages and buttons
            async def mock_send_message(recipient_id, message, session=None, session_id=None):
                whatsapp_messages.append(message)
                # Track message in session if provided (session_id takes precedence)
                actual_session = session_id if session_id is not None else session
                if actual_session:
                    try:
                        from app.services.helpers.summarization_helpers import SummarizationHelpers
                        from app.redis_db import get_session_redis_service
                        SummarizationHelpers.add_to_conversation_history(actual_session, "assistant", message, "text")
                        redis_session = get_session_redis_service()
                        await redis_session.append_message_to_history(actual_session.session_id, "assistant", message,
                                                                      "text")
                    except Exception as e:
                        logger.warning(f"Failed to track message in mock: {e}")
                return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()

            async def mock_send_configurable_buttons(recipient_id, body, buttons_config, header=None, footer=None,
                                                     session=None, session_id=None):
                whatsapp_messages.append(body)
                # Extract button info for UI
                button_data = [{'id': btn.get('id'), 'title': btn.get('title')} for btn in buttons_config]
                interactive_buttons.append(button_data)
                # Track message in session if provided (session_id takes precedence)
                actual_session = session_id if session_id is not None else session
                if actual_session:
                    try:
                        # Create a record to save in history
                        message_record = {
                            "header": header,
                            "body": body,
                            "footer": footer,
                            "buttons": button_data,  # original button config
                        }
                        from app.services.helpers.summarization_helpers import SummarizationHelpers
                        from app.redis_db import get_session_redis_service
                        SummarizationHelpers.add_to_conversation_history(actual_session, "assistant", message_record,
                                                                         "interactive_button")
                        redis_session = get_session_redis_service()
                        await redis_session.append_message_to_history(actual_session.session_id, "assistant",
                                                                      message_record, "interactive_button")
                    except Exception as e:
                        logger.warning(f"Failed to track button message in mock: {e}")
                return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()

            # Determine message type based on content structure
            message_type = "text"
            content = chat_message.message

            # Check if message contains image data (from UI image upload)
            if isinstance(content, dict) and "image" in content:
                message_type = "image"
            # Check if message is a button reply (interactive message)
            elif isinstance(content, dict) and content.get("type") == "button_reply":
                message_type = "interactive"

            t2 = time.time()
            logger.info(f"[PERF] Message validation completed: {(t2 - t1) * 1000:.0f}ms")

            # Create ChatService per-request with proper session management
            t3 = time.time()
            logger.info(f"[PERF] Setup completed: {(t3 - t2) * 1000:.0f}ms")

            with get_db_session_context() as db:
                t4 = time.time()
                logger.info(f"[PERF] DB session acquired: {(t4 - t3) * 1000:.0f}ms")

                chat_service = ChatService(db_session=db)
                t5 = time.time()
                logger.info(f"[PERF] ChatService initialized: {(t5 - t4) * 1000:.0f}ms")

                try:
                    # Process message through ChatService with mocked WhatsApp services
                    with patch.object(chat_service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
                            patch.object(chat_service.whatsapp_service, 'send_configurable_buttons',
                                         side_effect=mock_send_configurable_buttons):
                        t6 = time.time()
                        logger.info(f"[PERF] Mock patching completed: {(t6 - t5) * 1000:.0f}ms")

                        chat_result = await chat_service.process_message(chat_message.phone, content, message_type)
                        t7 = time.time()
                        logger.info(f"[PERF] process_message completed: {(t7 - t6) * 1000:.0f}ms")
                finally:
                    # Always cleanup resources
                    await chat_service.cleanup()
                    logger.debug("ChatService resources cleaned up")

            t_total = time.time()
            logger.info(f"[PERF] Total request time: {(t_total - request_start) * 1000:.0f}ms")
            trace.finish("complete", replies=len(whatsapp_messages))

            return {
                "success": True,
                "responses": whatsapp_messages,
                "interactive_buttons": interactive_buttons,
                "status": chat_result.get("status", "processed"),
                "debug_info": chat_result
            }

        except Exception as e:
            logger.error(f"Error processing chat message: {e}")
            trace.finish("error", error=type(e).__name__)

            # Notify support team about chat processing error
            try:
                await handle_server_error(
                    error_message=f"Chat processing error: {str(e)}",
                    user_phone=chat_message.phone,
                    current_flow="Chat Processing"
                )
            except Exception as notify_error:
                logger.error(f"Failed to notify support team: {notify_error}")

            return {
                "success": False,
                "error": str(e),
                "responses": ["Sorry, there was an error processing your message."]
            }


@app.post("/api/upload-excel")
@limiter.limit(settings.rate_limit_upload)
async def upload_excel_file(
        request: Request,
        phone: str = Form(...),
        file: UploadFile = File(...)
):
    """Process Excel file upload for testing."""
    try:
        import base64

        # Read file content
        file_content = await file.read()

        # Determine MIME type for Excel files.
        # UploadFile.filename is optional, so normalise once rather than calling
        # .lower() on a value that can be None.
        filename = (file.filename or "").lower()
        excel_mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.endswith('.xls'):
            excel_mime_type = "application/vnd.ms-excel"
        elif filename.endswith('.xlsm'):
            excel_mime_type = "application/vnd.ms-excel.sheet.macroEnabled.12"

        # Create mock document message structure (same as WhatsApp webhook)
        # Include base64 data for attachment support when user is in optional phase
        document_content = {
            "document": {
                "filename": file.filename,
                "link": "mock://uploaded-file",
                "data": base64.b64encode(file_content).decode('utf-8'),
                "mime_type": excel_mime_type
            }
        }

        # Store captured WhatsApp messages and interactive buttons
        whatsapp_messages = []
        interactive_buttons = []

        async def mock_send_message(recipient_id, message, session=None, session_id=None):
            whatsapp_messages.append(message)
            # Track message in session if provided (session_id takes precedence)
            actual_session = session_id if session_id is not None else session
            if actual_session:
                try:
                    from app.services.helpers.summarization_helpers import SummarizationHelpers
                    from app.redis_db import get_session_redis_service
                    SummarizationHelpers.add_to_conversation_history(actual_session, "assistant", message, "text")
                    redis_session = get_session_redis_service()
                    await redis_session.append_message_to_history(actual_session.session_id, "assistant", message,
                                                                  "text")
                except Exception as e:
                    logger.warning(f"Failed to track message in mock: {e}")
            return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()

        async def mock_send_configurable_buttons(recipient_id, body, buttons_config, header=None, footer=None,
                                                 session=None, session_id=None):
            whatsapp_messages.append(body)
            # Extract button info for UI
            button_data = [{'id': btn.get('id'), 'title': btn.get('title')} for btn in buttons_config]
            interactive_buttons.append(button_data)
            # Track message in session if provided (session_id takes precedence)
            actual_session = session_id if session_id is not None else session
            if actual_session:
                try:
                    from app.services.helpers.summarization_helpers import SummarizationHelpers
                    from app.redis_db import get_session_redis_service
                    SummarizationHelpers.add_to_conversation_history(actual_session, "assistant", body,
                                                                     "interactive_button")
                    redis_session = get_session_redis_service()
                    await redis_session.append_message_to_history(actual_session.session_id, "assistant", body,
                                                                  "interactive_button")
                except Exception as e:
                    logger.warning(f"Failed to track button message in mock: {e}")
            return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()

        # Mock the validation service to use direct content
        from app.services.excel_validation_service import ExcelValidationService

        async def mock_validate(self, file_url, filename):
            return {
                'valid': True,
                'content': file_content,
                'filename': filename,
                'size': len(file_content),
                'format': 'xlsx'
            }

        # Create ChatService per-request with proper session management
        from app.database import get_db_session_context
        from unittest.mock import patch

        with get_db_session_context() as db:
            chat_service = ChatService(db_session=db)

            # Process through ChatService with mocked services
            with patch.object(chat_service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
                    patch.object(chat_service.whatsapp_service, 'send_configurable_buttons',
                                 side_effect=mock_send_configurable_buttons), \
                    patch.object(ExcelValidationService, 'validate_excel_file_from_url', mock_validate):
                chat_result = await chat_service.process_message(phone, document_content, "excel_upload")

        return {
            "success": True,
            "responses": whatsapp_messages,
            "interactive_buttons": interactive_buttons,
            "status": chat_result.get("status", "processed"),
            "filename": file.filename,
            "size": len(file_content),
            "debug_info": chat_result
        }

    except Exception as e:
        logger.error(f"Error processing Excel upload: {e}")

        # Notify support team about Excel processing error
        try:
            await handle_server_error(
                error_message=f"Excel processing error: {str(e)}",
                user_phone=phone,
                current_flow="Excel Upload Processing"
            )
        except Exception as notify_error:
            logger.error(f"Failed to notify support team: {notify_error}")

        return {
            "success": False,
            "error": str(e),
            "responses": [f"Sorry, there was an error processing your Excel file: {str(e)}"]
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8005,
        reload=settings.DEBUG,
        log_level=settings.log_level.lower()
    )