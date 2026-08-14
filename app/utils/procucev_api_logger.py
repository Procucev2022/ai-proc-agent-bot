"""
Procucev API Logging Utility
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from functools import wraps
import time
import asyncio

logger = logging.getLogger(__name__)

class ProcucevAPILogger:
    def __init__(self, log_dir: str = "logs/procucev_api_interactions"):
        self.log_dir = log_dir
        self._ensure_log_directory()
    
    def _ensure_log_directory(self):
        os.makedirs(self.log_dir, exist_ok=True)
    
    def _get_log_file_path(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"procucev_api_calls_{today}.jsonl")
    
    def log_api_call(self, api_title: str, api_url: str, input_data: Dict[str, Any], response_data: Dict[str, Any], processing_time: float, timestamp: Optional[str] = None, phone_number: Optional[str] = None):
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        base_url = os.getenv('GMT_BASE_URL', 'https://api.procucev.com')
        api_url = f"{base_url}{api_url}"

        log_entry = {
            "timestamp": timestamp,
            "phone_number": phone_number or "N/A",
            "source": "procucev_api",
            "api_title": api_title,
            "api_url": api_url,
            "input": input_data,
            "response": response_data,
            "processing_time_seconds": round(processing_time, 6)
        }
        
        try:
            log_file = self._get_log_file_path()
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"Failed to write Procucev API log: {e}")

procucev_api_logger = ProcucevAPILogger()

def manual_log_api_call(api_title: str, endpoint: str, input_data: Dict[str, Any], response_data: Dict[str, Any], processing_time: float, phone_number: Optional[str] = None):
    """Manually log API call with specific input data."""
    timestamp = datetime.now(timezone.utc).isoformat()
    procucev_api_logger.log_api_call(api_title, endpoint, input_data, response_data, processing_time, timestamp, phone_number)

def log_procucev_api_call(api_title: Optional[str] = None, endpoint: Optional[str] = None):
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            timestamp = datetime.now(timezone.utc).isoformat()
            title = api_title or func.__name__
            base_url = os.getenv('GMT_BASE_URL', 'https://api.procucev.com')
            url = f"{base_url}{endpoint}" if endpoint else f"{base_url}/{func.__name__}"

            # Extract phone_number from kwargs if available
            phone_number = kwargs.get('user_phone') or kwargs.get('phone_number')
            input_data = {"args": [str(arg) for arg in args[1:]] if len(args) > 1 else [], "kwargs": kwargs}

            try:
                result = await func(*args, **kwargs)
                processing_time = time.time() - start_time
                procucev_api_logger.log_api_call(title, url, input_data, result, processing_time, timestamp, phone_number)
                return result
            except Exception as e:
                processing_time = time.time() - start_time
                error_response = {"error": str(e), "success": False, "timestamp": datetime.now(timezone.utc).isoformat()}
                procucev_api_logger.log_api_call(title, url, input_data, error_response, processing_time, timestamp, phone_number)
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            timestamp = datetime.now(timezone.utc).isoformat()
            title = api_title or func.__name__
            base_url = os.getenv('GMT_BASE_URL', 'https://api.procucev.com')
            url = f"{base_url}{endpoint}" if endpoint else f"{base_url}/{func.__name__}"

            # Extract phone_number from kwargs if available
            phone_number = kwargs.get('user_phone') or kwargs.get('phone_number')
            input_data = {"args": [str(arg) for arg in args[1:]] if len(args) > 1 else [], "kwargs": kwargs}

            try:
                result = func(*args, **kwargs)
                processing_time = time.time() - start_time
                procucev_api_logger.log_api_call(title, url, input_data, result, processing_time, timestamp, phone_number)
                return result
            except Exception as e:
                processing_time = time.time() - start_time
                error_response = {"error": str(e), "success": False, "timestamp": datetime.now(timezone.utc).isoformat()}
                procucev_api_logger.log_api_call(title, url, input_data, error_response, processing_time, timestamp, phone_number)
                raise
        
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
    return decorator