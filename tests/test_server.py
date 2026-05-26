#!/usr/bin/env python3
"""
Minimal test server for rate limiting demonstration.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import time

# Create limiter
limiter = Limiter(key_func=get_remote_address)

# Create FastAPI app
app = FastAPI(title="Rate Limiting Test Server")

# Add rate limiting to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.get("/")
async def root():
    return {"message": "Rate Limiting Test Server", "timestamp": time.time()}

@app.get("/health")
@limiter.limit("10/minute")
async def health_check(request: Request):
    return {"status": "healthy", "timestamp": time.time()}

@app.post("/api/chat")
@limiter.limit("5/minute")
async def chat_endpoint(request: Request, message: dict = None):
    return {
        "message": "Chat response",
        "received": message,
        "timestamp": time.time()
    }

@app.post("/api/upload")
@limiter.limit("2/minute")
async def upload_endpoint(request: Request):
    return {
        "message": "Upload processed",
        "timestamp": time.time()
    }

@app.get("/api/fast")
@limiter.limit("20/minute")
async def fast_endpoint(request: Request):
    return {
        "message": "Fast endpoint",
        "timestamp": time.time()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)