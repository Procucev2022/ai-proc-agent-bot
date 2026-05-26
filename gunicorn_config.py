"""
Gunicorn configuration for AI Procurement Agent.

This configuration uses Uvicorn workers for optimal async performance with FastAPI.

Optimized for Azure D8as v5 Server (8 vCPUs, 32GB RAM + 32GB Swap):
- App Priority: 16GB RAM + swap, 4 cores (heavy WhatsApp traffic)
- Workers: 10 (optimized for 16GB allocation, ~1.6GB per worker)

Performance notes:
- Each worker consumes ~1.5-1.7GB memory with embedding models
- 10 workers * 1.6GB = ~16GB (within limit with swap support)
- Formula: workers = (CPU cores / 2) + 1 (for I/O bound apps)
- For 8 vCPUs: (8/2) + 1 = 5 minimum, using 10 for headroom with swap

Common scenarios:
- Development: 1-2 workers
- Small production (2 cores, 4GB RAM): 3-5 workers
- Medium production (4 cores, 8GB RAM): 6-9 workers
- D8as v5 (8 cores, 32GB RAM + 32GB swap): 10 workers (16GB allocation) ← CURRENT
"""

import multiprocessing
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================================================================
# WORKER CONFIGURATION - ADJUST THESE
# ============================================================================

# Number of worker processes
# Optimized for Azure D8as v5 (8 vCPUs, 32GB RAM + 32GB Swap)
# App gets 16GB RAM allocation with swap support
# Default: 10 workers (~1.6GB each = ~16GB total)
# Set WORKERS environment variable in docker-compose.yml or .env
# Options:
# 1. Set specific number: workers = 10
# 2. Auto-calculate: workers = (multiprocessing.cpu_count() // 2) + 1
# 3. Use environment variable (recommended): workers = int(os.getenv("WORKERS", 10))
workers = int(os.getenv("WORKERS", 10))

# Worker class - use uvicorn for async FastAPI support
worker_class = "uvicorn.workers.UvicornWorker"

# Threads per worker (for handling concurrent requests within a worker)
# Increased for better async performance with WhatsApp webhooks
threads = int(os.getenv("WORKER_THREADS", 2))

# ============================================================================
# SERVER BINDING
# ============================================================================

# Server socket binding
bind = os.getenv("BIND_ADDRESS", "0.0.0.0:8005")

# Alternatively, you can use unix socket for better performance with nginx
# bind = "unix:/tmp/gunicorn.sock"

# ============================================================================
# PERFORMANCE TUNING
# ============================================================================

# Worker timeout (seconds) - important for long-running operations
# Your app makes OpenAI calls which can take time
# Increased timeout for heavy batch processing during day mode
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

# Memory management for D8as v5 (32GB RAM system)
# Monitor worker memory and restart if exceeds threshold
max_requests_write_file = os.getenv("MAX_REQUESTS_WRITE_FILE", None)

# ============================================================================
# LOGGING
# ============================================================================

# Logging level
loglevel = os.getenv("LOG_LEVEL", "info")

# Access and error logs — write directly to files (no FIFO).
# Python's app logging uses ConcurrentTimedRotatingFileHandler; writes to
# app.log and rotates to app.log.YYYY-MM-DD at midnight (multi-process safe).
# Gunicorn's own logs are rotated the same way via logconfig_dict below.
_gunicorn_log_dir = os.getenv("LOG_DIR", "/app/logs/app")
os.makedirs(_gunicorn_log_dir, exist_ok=True)
accesslog = os.getenv("ACCESS_LOG", os.path.join(_gunicorn_log_dir, "gunicorn_access.log"))
errorlog = os.getenv("ERROR_LOG", os.path.join(_gunicorn_log_dir, "gunicorn_error.log"))

# Access log format
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Capture stdout/stderr into errorlog so stray writes (tqdm, warnings, tracebacks)
# go to a real file instead of raising BrokenPipeError.
capture_output = True

# Override gunicorn's own loggers with multi-process-safe, daily-rotating handlers.
# Produces gunicorn_access.log + gunicorn_access.log.YYYY-MM-DD (and same for error).
# dictConfig is applied after gunicorn attaches its default handlers, so the
# `handlers` list here replaces them on gunicorn.access / gunicorn.error.
_log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", "30"))
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "generic": {
            "format": "%(asctime)s [%(process)d] [%(levelname)s] %(message)s",
            "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
            "class": "logging.Formatter",
        },
        "access": {
            # Gunicorn pre-formats the access line via access_log_format, so just
            # emit the resulting message verbatim.
            "format": "%(message)s",
            "class": "logging.Formatter",
        },
    },
    "handlers": {
        "error_file": {
            "class": "concurrent_log_handler.ConcurrentTimedRotatingFileHandler",
            "filename": errorlog,
            "when": "midnight",
            "interval": 1,
            "backupCount": _log_backup_count,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "generic",
        },
        "access_file": {
            "class": "concurrent_log_handler.ConcurrentTimedRotatingFileHandler",
            "filename": accesslog,
            "when": "midnight",
            "interval": 1,
            "backupCount": _log_backup_count,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "access",
        },
    },
    "loggers": {
        "gunicorn.error": {
            "level": "INFO",
            "handlers": ["error_file"],
            "propagate": False,
            "qualname": "gunicorn.error",
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["access_file"],
            "propagate": False,
            "qualname": "gunicorn.access",
        },
    },
    # Must override gunicorn's default root — CONFIG_DEFAULTS['root'] references a
    # 'console' handler that our shallow-merged handlers dict no longer contains,
    # which crashes dictConfig with KeyError: 'console'. app.utils.logging_utils
    # .setup_basic_logging() attaches the real file handler to root at app startup.
    "root": {
        "level": "INFO",
        "handlers": [],
    },
}

# ============================================================================
# PROCESS NAMING
# ============================================================================

# Process name in process list
proc_name = "procurement_agent"

# ============================================================================
# DEVELOPMENT VS PRODUCTION
# ============================================================================

# Reload on code changes (only for development)
reload = os.getenv("ENV", "production").lower() == "development"

# Preload application code before worker processes are forked
# Set to False if you experience stale data issues with module-level instances
# True = Better memory efficiency, False = Fresh initialization per worker
preload_app = os.getenv("PRELOAD_APP", "false").lower() == "true"

# ============================================================================
# SECURITY
# ============================================================================

# Limit request line size (helps prevent certain attacks)
limit_request_line = 4096

# Limit request header size
limit_request_field_size = 8190

# ============================================================================
# HOOKS (for custom startup/shutdown logic)
# ============================================================================

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


def reload_config(server):
    """Reload configuration and reopen log files (called via SIGHUP)."""
    server.log.info("Received SIGHUP - reloading configuration and reopening logs...")
    
    # Reopen log files for all workers
    server.log.info("Reopening log files...")


# Register SIGHUP handler for log rotation and config reload
try:
    import signal
    # Gunicorn already handles SIGHUP by default, but we can add custom logging
    # The master process will automatically reopen log files on SIGHUP
except (AttributeError, ValueError):
    # SIGHUP not available on all platforms (e.g., Windows)
    pass
