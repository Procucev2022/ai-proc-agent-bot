"""
Gunicorn configuration for AI Procurement Agent.

This configuration uses Uvicorn workers for optimal async performance with FastAPI.

Quick worker calculation guide:
- workers = (2 * CPU cores) + 1 (for I/O bound apps like this one)
- For CPU-bound: workers = CPU cores
- Each worker consumes memory, monitor your RAM usage

Common scenarios:
- Development: 1-2 workers
- Small production (2 cores, 4GB RAM): 3-5 workers
- Medium production (4 cores, 8GB RAM): 6-9 workers
- Large production (8 cores, 16GB RAM): 12-17 workers
"""

import multiprocessing
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================================
# WORKER CONFIGURATION - ADJUST THESE
# ============================================

# Number of worker processes
# Options:
# 1. Set specific number: workers = 4
# 2. Auto-calculate: workers = (multiprocessing.cpu_count() * 2) + 1
# 3. Use environment variable (recommended): workers = int(os.getenv("WORKERS", 4))
workers = int(os.getenv("WORKERS", (multiprocessing.cpu_count() * 2) + 1))

# Worker class - use uvicorn for async FastAPI support
worker_class = "uvicorn.workers.UvicornWorker"

# Threads per worker (for handling concurrent requests within a worker)
threads = int(os.getenv("WORKER_THREADS", 1))

# ============================================
# SERVER BINDING
# ============================================

# Server socket binding
bind = os.getenv("BIND_ADDRESS", "0.0.0.0:8000")

# Alternatively, you can use unix socket for better performance with nginx
# bind = "unix:/tmp/gunicorn.sock"

# ============================================
# PERFORMANCE TUNING
# ============================================

# Worker timeout (seconds) - important for long-running operations
# Your app makes OpenAI calls which can take time
timeout = int(os.getenv("WORKER_TIMEOUT", 120))

# Graceful timeout for workers during restart (seconds)
graceful_timeout = int(os.getenv("GRACEFUL_TIMEOUT", 30))

# Keep-alive connections (seconds)
keepalive = int(os.getenv("KEEPALIVE", 5))

# Maximum number of pending connections
backlog = int(os.getenv("BACKLOG", 2048))

# Maximum number of requests a worker will process before restarting
# Helps prevent memory leaks
max_requests = int(os.getenv("MAX_REQUESTS", 1000))
max_requests_jitter = int(os.getenv("MAX_REQUESTS_JITTER", 50))

# ============================================
# LOGGING
# ============================================

# Logging level
loglevel = os.getenv("LOG_LEVEL", "info")

# Access log file (use "-" for stdout)
accesslog = os.getenv("ACCESS_LOG", "-")

# Error log file (use "-" for stderr)
errorlog = os.getenv("ERROR_LOG", "-")

# Access log format
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Capture stdout/stderr in logs
capture_output = True

# ============================================
# PROCESS NAMING
# ============================================

# Process name in process list
proc_name = "procurement_agent"

# ============================================
# DEVELOPMENT VS PRODUCTION
# ============================================

# Reload on code changes (only for development)
reload = os.getenv("ENV", "production").lower() == "development"

# Preload application code before worker processes are forked
# Set to False if you experience stale data issues with module-level instances
# True = Better memory efficiency, False = Fresh initialization per worker
preload_app = os.getenv("PRELOAD_APP", "false").lower() == "true"

# ============================================
# SECURITY
# ============================================

# Limit request line size (helps prevent certain attacks)
limit_request_line = 4096

# Limit request header size
limit_request_field_size = 8190

# ============================================
# HOOKS (for custom startup/shutdown logic)
# ============================================

def on_starting(server):
    """Called just before the master process is initialized."""
    server.log.info("Starting Gunicorn server")

def on_reload(server):
    """Called when the server is reloaded."""
    server.log.info("Reloading Gunicorn server")

def when_ready(server):
    """Called just after the server is started."""
    server.log.info(f"Server is ready. Workers: {workers}, Worker class: {worker_class}")

def pre_fork(server, worker):
    """Called just before a worker is forked."""
    pass

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")

    # Dispose database connections inherited from parent process
    # This prevents "MySQL server has gone away" errors
    try:
        from app.database import engine, remote_engine
        if engine:
            engine.dispose()
            server.log.info(f"Worker {worker.pid}: Disposed main database engine")
        if remote_engine:
            remote_engine.dispose()
            server.log.info(f"Worker {worker.pid}: Disposed remote database engine")
    except Exception as e:
        server.log.warning(f"Worker {worker.pid}: Error disposing engines: {e}")

def pre_exec(server):
    """Called just before a new master process is forked."""
    server.log.info("Forking new master process")

def worker_int(worker):
    """Called when a worker receives the SIGINT or SIGQUIT signal."""
    worker.log.info(f"Worker received SIGINT/SIGQUIT (pid: {worker.pid})")

def worker_abort(worker):
    """Called when a worker receives the SIGABRT signal."""
    worker.log.warning(f"Worker received SIGABRT (pid: {worker.pid})")

def pre_request(worker, req):
    """Called just before a worker processes a request."""
    pass

def post_request(worker, req, environ, resp):
    """Called after a worker processes a request."""
    pass

def child_exit(server, worker):
    """Called when a worker is exiting."""
    server.log.info(f"Worker exiting (pid: {worker.pid})")

def worker_exit(server, worker):
    """Called after a worker has been exited."""
    server.log.info(f"Worker exited (pid: {worker.pid})")

def nworkers_changed(server, new_value, old_value):
    """Called when the number of workers changes."""
    server.log.info(f"Number of workers changed from {old_value} to {new_value}")

def on_exit(server):
    """Called just before shutting down."""
    server.log.info("Shutting down Gunicorn server")
