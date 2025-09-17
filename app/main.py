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
from contextlib import asynccontextmanager
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from typing import Dict, Any
from app.config import get_settings
from app.api.webhook import router as webhook_router
from app.database import init_database
from app.services.chat_service import ChatService
from app.context.middleware import ContextMiddleware


# Get settings and configure logging
settings = get_settings()
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
    default_limits=[settings.rate_limit_default]
)


class IPRestrictionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: list):
        super().__init__(app)
        self.allowed_ips = allowed_ips

    async def dispatch(self, request: Request, call_next):
        if self.allowed_ips:
            client_ip = request.client.host
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting AI Procurement Agent application")
    
    # Initialize database on startup
    try:
        init_database()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
    
    yield
    
    logger.info("Shutting down AI Procurement Agent application")


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
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Add context middleware (must be first)
app.add_middleware(ContextMiddleware)
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
app.mount("/static", StaticFiles(directory="static"), name="static")
# templates = Jinja2Templates(directory="templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Include API routers
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])

# Initialize chat service
chat_service = ChatService()

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
            "chat": "/chat"
        }
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Serve the chat UI page."""
    return templates.TemplateResponse("chat.html", {"request": request})


@app.post("/api/chat")
@limiter.limit(settings.rate_limit_chat)
async def process_chat_message(request: Request, chat_message: ChatMessage):
    """Process chat message through ChatService - works exactly like test_multi_turn_conversation.py"""
    try:
        # Store captured WhatsApp messages (same as terminal test)
        whatsapp_messages = []
        
        # Mock the WhatsApp service to capture messages (same pattern as terminal test)
        async def mock_send_message(recipient_id, message):
            whatsapp_messages.append(message)
            return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()
        
        # Determine message type based on content structure
        message_type = "text"
        content = chat_message.message
        
        # Check if message contains image data (from UI image upload)
        if isinstance(content, dict) and "image" in content:
            message_type = "image"
        
        # Process message through ChatService with mocked WhatsApp (same as terminal test)
        from unittest.mock import patch
        with patch.object(chat_service.whatsapp_service, 'send_message', side_effect=mock_send_message):
            chat_result = await chat_service.process_message(chat_message.phone, content, message_type)
        
        return {
            "success": True,
            "responses": whatsapp_messages,
            "status": chat_result.get("status", "processed"),
            "debug_info": chat_result
        }
        
    except Exception as e:
        logger.error(f"Error processing chat message: {e}")
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
        # Read file content
        file_content = await file.read()
        
        # Create mock document message structure (same as WhatsApp webhook)
        document_content = {
            "document": {
                "filename": file.filename,
                "link": "mock://uploaded-file"
            }
        }
        
        # Store captured WhatsApp messages
        whatsapp_messages = []
        
        async def mock_send_message(recipient_id, message):
            whatsapp_messages.append(message)
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
        
        # Process through ChatService with mocked services
        from unittest.mock import patch
        with patch.object(chat_service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
             patch.object(ExcelValidationService, 'validate_excel_file_from_url', mock_validate):
            
            chat_result = await chat_service.process_message(phone, document_content, "excel_upload")
        
        return {
            "success": True,
            "responses": whatsapp_messages,
            "status": chat_result.get("status", "processed"),
            "filename": file.filename,
            "size": len(file_content),
            "debug_info": chat_result
        }
        
    except Exception as e:
        logger.error(f"Error processing Excel upload: {e}")
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
        port=8000,
        reload=settings.DEBUG,
        log_level="debug" if settings.DEBUG else "info"
    )