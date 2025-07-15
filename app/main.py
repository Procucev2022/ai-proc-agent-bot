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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
from contextlib import asynccontextmanager
from pydantic import BaseModel

from app.config import get_settings
from app.api.webhook import router as webhook_router
from app.database import init_database
from app.services.chat_service import ChatService


# Get settings and configure logging
settings = get_settings()
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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

# Configure CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Include API routers
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])

# Initialize chat service
chat_service = ChatService()

# Pydantic models for chat API
class ChatMessage(BaseModel):
    message: str
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
async def process_chat_message(chat_message: ChatMessage):
    """Process chat message through ChatService - works exactly like test_multi_turn_conversation.py"""
    try:
        # Store captured WhatsApp messages (same as terminal test)
        whatsapp_messages = []
        
        # Mock the WhatsApp service to capture messages (same pattern as terminal test)
        async def mock_send_message(recipient_id, message):
            whatsapp_messages.append(message)
            return type('MessageResponse', (), {'success': True, 'message_id': 'test_id'})()
        
        # Process message through ChatService with mocked WhatsApp (same as terminal test)
        from unittest.mock import patch
        with patch.object(chat_service.whatsapp_service, 'send_message', side_effect=mock_send_message):
            chat_result = await chat_service.process_message(chat_message.phone, chat_message.message, "text")
        
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