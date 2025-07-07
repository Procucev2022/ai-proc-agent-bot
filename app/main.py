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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging
from contextlib import asynccontextmanager

from app.config import get_settings
from app.api.webhook import router as webhook_router
from app.database import init_database


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

# Include API routers
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])


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
            "health": "/health"
        }
    )