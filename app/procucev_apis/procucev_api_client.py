"""
Procucev API Handler for Procucev API Integration.

This handler manages OAuth authentication , registration ,rfq's , bfs , etc..
and API calls to the GMT Procucev backend system.
"""

import logging
import asyncio
import time
import aiohttp
from datetime import datetime, timedelta , UTC
from typing import Dict, Any, Optional, Literal
from app.config import get_settings
from app.schemas.user import normalize_phone_number
from app.redis_db import get_redis_service
from app.utils.procucev_api_logger import manual_log_api_call

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global singleton instance
_global_client: Optional['ProcucevAPIClient'] = None

def get_procucev_api_client() -> 'ProcucevAPIClient':
    """
    Get the global ProcucevAPIClient singleton instance.

    Returns:
        The shared ProcucevAPIClient instance

    Raises:
        RuntimeError: If client hasn't been initialized via init_procucev_api_client()
    """
    global _global_client
    if _global_client is None:
        raise RuntimeError(
            "ProcucevAPIClient not initialized. "
            "Call init_procucev_api_client() during application startup."
        )
    return _global_client

async def init_procucev_api_client() -> 'ProcucevAPIClient':
    """
    Initialize the global ProcucevAPIClient singleton.
    Should be called once during application startup.

    Returns:
        The initialized ProcucevAPIClient instance
    """
    global _global_client
    if _global_client is None:
        _global_client = ProcucevAPIClient()
        await _global_client.create_session()
        logger.info("Global ProcucevAPIClient initialized")
    return _global_client

async def close_procucev_api_client():
    """
    Close the global ProcucevAPIClient singleton.
    Should be called once during application shutdown.
    """
    global _global_client
    if _global_client is not None:
        await _global_client.close_session()
        _global_client = None
        logger.info("Global ProcucevAPIClient closed")

class ProcucevAPIClient:
    """
    General asynchronous API client with pooled session, auth token management,
    and unified GET/POST/PUT/DELETE handling.
    """

    def __init__(self):
        self.settings = get_settings()  # Pydantic BaseSettings from env/secrets
        self.base_url = self.settings.gmt_base_url
        self.username = self.settings.gmt_username
        self.password = self.settings.gmt_password
        self.phone = self.settings.gmt_phone
        self.client_id = self.settings.gmt_client_id
        self.client_secret = self.settings.gmt_client_secret
        self.max_retries = self.settings.gmt_max_retries or 3
        self.retry_delay = self.settings.gmt_retry_delay or 1.0
        
        # Enhanced timeout configuration
        self.connect_timeout = 10  # Connection timeout
        self.read_timeout = 30     # Read timeout
        self.total_timeout = 60    # Total request timeout

        self.auth_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.redis_service = get_redis_service()
        self.token_cache_key = "procucev_api:auth_token"

    # Note: __del__ removed - cannot reliably close async sessions in __del__
    # Use context managers (async with) or explicit close_session() calls instead

    async def __aenter__(self):
        await self.create_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()

    async def create_session(self):
        """Initialize one aiohttp session for all requests (connection pooling)."""
        if not self.session:
            # Enhanced timeout configuration
            timeout = aiohttp.ClientTimeout(
                total=self.total_timeout,
                connect=self.connect_timeout,
                sock_read=self.read_timeout
            )
            # Connection pooling with retry-friendly settings
            connector = aiohttp.TCPConnector(
                limit_per_host=50,
                limit=100,
                ttl_dns_cache=300,
                use_dns_cache=True,
                enable_cleanup_closed=True
            )
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            
    async def close_session(self):
        """Close the aiohttp session cleanly."""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("HTTP session closed")
            self.session = None



    async def authenticate(self) -> bool:
        """
        Authenticate with the API to obtain a bearer token.
        Example: POST to /authenticate with username and phone.
        """
        auth_url = f"{self.base_url}/authenticate"

        try:
            payload = {
                "username": self.username,
                "phone": f"{normalize_phone_number(self.phone)}"
            }
            resp = await self.send_request("POST", auth_url, json_data=payload, require_auth=False)
            token = resp.get("access_token") or resp.get("token")
            expires_in = resp.get("expires_in") or resp.get("expires", 3600)
            if token:
                # Subtract 5 minutes for safety buffer
                safe_expires_in = max(expires_in - 300, 60)
                self.auth_token = token
                self.token_expiry = datetime.now(UTC) + timedelta(seconds=int(expires_in))

                # Cache token in Redis with safety buffer
                token_data = {
                    "access_token": token,
                    "expires_at": self.token_expiry.timestamp(),
                    "created_at": datetime.now(UTC).isoformat()
                }
                await self.redis_service.set(self.token_cache_key, token_data, ex=safe_expires_in)

                logger.info(f"Procucev API authentication successful. Token expires in {expires_in} seconds")
                logger.info(f"Token cached in Redis with {safe_expires_in}s expiry")
                return True
            else:
                logger.error(f"No token in auth response: {resp}")
                return False
        except Exception as e:
            logger.error(f"Token Authentication error: {e}")
            return False

    async def _ensure_authenticated(self) -> bool:
        """
        Ensure a valid auth token exists. If expired or missing, refresh it safely.
        Uses an asyncio.Lock to prevent race conditions. Checks Redis cache first.
        """
        # If token still valid in memory, nothing to do
        if self.auth_token and self.token_expiry and datetime.now(UTC) < self.token_expiry:
            return True

        # Check Redis cache for existing valid token
        try:
            cached_token_data = await self.redis_service.get(self.token_cache_key, as_json=True)
            if cached_token_data and isinstance(cached_token_data, dict):
                cached_expires_at = cached_token_data.get("expires_at")
                if cached_expires_at and datetime.now(UTC).timestamp() < cached_expires_at:
                    self.auth_token = cached_token_data.get("access_token")
                    self.token_expiry = datetime.fromtimestamp(cached_expires_at, UTC)
                    logger.info("Retrieved valid Procucev API token from Redis cache")
                    return True
                else:
                    logger.info("Cached Procucev API token expired, will re-authenticate")
            else:
                logger.info("No valid Procucev API token found in cache")
        except Exception as e:
            logger.warning(f"Error checking Procucev API token cache: {e}")

        # Acquire lock to refresh token
        async with self._lock:
            # Double-check in case another coroutine refreshed while waiting
            if self.auth_token and self.token_expiry and datetime.now(UTC) < self.token_expiry:
                return True
            success = await self.authenticate()
            if not success or not self.auth_token:
                logger.error(f"Authentication failed with token: {self.auth_token}")
            return success

    async def send_request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        url_or_endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        require_auth: bool = False,
        api_title: Optional[str] = None
    ) -> Dict[str, Any]:
        """ Base HTTP request handler with retry/backoff. """
        # Start timing for logging
        start_time = time.time()
        
        # check session initialisation
        if self.session is None:
            await self.create_session()

        # Build full URL
        url = url_or_endpoint if url_or_endpoint.startswith("http") else f"{self.base_url}{url_or_endpoint}"
        headers = headers.copy() if headers else {}
        
        # Prepare input data for logging
        input_data = {
            "method": method,
            "url": url,
            "params": params,
            "payload": json_data,
            "data": str(data) if data else None,
            "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},  # Exclude auth header
            "require_auth": require_auth
        }

        # Ensure authentication if needed
        if require_auth:
            await self._ensure_authenticated()
            headers["Authorization"] = f"Bearer {self.auth_token}"

        for attempt in range(self.max_retries):
            try:
                request_start_time = time.time()
                async with self.session.request(
                    method, url, params=params, json=json_data, data=data, headers=headers
                ) as resp:
                    response_time = time.time() - request_start_time
                    status = resp.status
                    text = await resp.text()
                    logger.info(f"Procucev API {method} {url} response received in {response_time:.3f}s (status: {status})")
                    
                    # If 401 Unauthorized, maybe token expired: retry after refreshing token
                    if status == 401 and require_auth:
                        logger.warning("401 Unauthorized – refreshing token and retrying...")
                        self.auth_token = None
                        await self._ensure_authenticated()
                        continue
                    # Success (2xx)
                    if 200 <= status < 300:
                        result = await self.normalize_response(resp, text)
                        # Log successful API call
                        processing_time = time.time() - start_time
                        title = api_title or f"{method} {url_or_endpoint}"
                        manual_log_api_call(title, url, input_data, result, processing_time)
                        return result
                    # Error: attempt to parse error message, then return error response
                    try:
                        error_payload = await resp.json()
                    except Exception:
                        error_payload = {"error": text}
                    logger.error(f"HTTP {status} error: {error_payload}")
                    error_result = {
                        "success": False,
                        "status_code": status,
                        "message": error_payload.get("error", "Request failed"),
                        "data": error_payload,
                        "timestamp": datetime.now(UTC).isoformat() + "Z"
                    }
                    
                    # Send WhatsApp notification for 500 errors
                    if status == 500:
                        await self._handle_500_error(url_or_endpoint, error_payload.get("error", "Request failed"))
                    
                    # Log error response
                    processing_time = time.time() - start_time
                    title = api_title or f"{method} {url_or_endpoint}"
                    manual_log_api_call(title, url, input_data, error_result, processing_time)
                    return error_result
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                request_time = time.time() - request_start_time
                error_type = "Network error"
                if isinstance(e, asyncio.TimeoutError):
                    error_type = "Request timeout"
                elif isinstance(e, aiohttp.ClientConnectorError):
                    error_type = "Connection failed"
                elif isinstance(e, aiohttp.ClientResponseError):
                    error_type = "HTTP error"

                logger.warning(f"Attempt {attempt+1}/{self.max_retries} for {method} {url} failed in {request_time:.3f}s: {error_type} - {e}")
                
                if attempt == self.max_retries - 1:
                    logger.error(f"Max retries exceeded for {method} {url}")
                    
                    # Log API failure and send WhatsApp notification
                    logger.error(f"Procucev API failure: {error_type} - {str(e)} for {method} {url_or_endpoint}")
                    
                    error_result = {
                        "success": False,
                        "status_code": 500,
                        "message": f"{error_type}. Please check your connection and try again.",
                        "data": None,
                        "timestamp": datetime.now(UTC).isoformat() + "Z"
                    }
                    
                    # Send WhatsApp notification for timeout/network errors
                    await self._handle_timeout_error(url_or_endpoint, f"{error_type}: {str(e)}")
                    
                    # Log network error
                    processing_time = time.time() - start_time
                    title = api_title or f"{method} {url_or_endpoint}"
                    manual_log_api_call(title, url, input_data, error_result, processing_time)
                    return error_result
                
                # Exponential backoff with jitter
                backoff_time = self.retry_delay * (2 ** attempt) + (attempt * 0.1)
                logger.info(f"Retrying {method} {url} in {backoff_time:.1f} seconds...")
                await asyncio.sleep(backoff_time)

        # Should never reach here, but just in case
        logger.error(f"Exceeded retry loop for {method} {url}")
        final_error_result = {
            "success": False,
            "status_code": 500,
            "message": "Service temporarily unavailable. Please try again later.",
            "data": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z"
        }
        # Log final fallback error
        processing_time = time.time() - start_time
        title = api_title or f"{method} {url_or_endpoint}"
        manual_log_api_call(title, url, input_data, final_error_result, processing_time)
        return final_error_result

    async def normalize_response(self, resp: aiohttp.ClientResponse, raw_text: str) -> Dict[str, Any]:
        """
        Parse JSON or text response and normalize it.
        Adds a 'status': 'success' field for consistency.
        """
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            result = await resp.json()
            if isinstance(result, dict):
                result.setdefault("success", True)
                result.setdefault("timestamp", datetime.now(UTC).isoformat() + "Z")
                return result
            elif isinstance(result, list):
                return {"success": True, "data": result, "timestamp": datetime.now(UTC).isoformat() + "Z"}
        # Fallback to raw text
        return {"success": True, "data": raw_text, "timestamp": datetime.now(UTC).isoformat() + "Z"}

    # Convenience methods for each HTTP verb
    async def get(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        return await self.send_request("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        return await self.send_request("POST", endpoint, **kwargs)

    async def put(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        return await self.send_request("PUT", endpoint, **kwargs)

    async def delete(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        return await self.send_request("DELETE", endpoint, **kwargs)
    
    async def _handle_500_error(self, endpoint: str, error_message: str) -> None:
        """Handle 500 errors by sending WhatsApp notifications."""
        try:
            from app.services.global_error_handler import handle_api_error
            await handle_api_error("Procucev API", endpoint, error_message)
        except Exception as e:
            logger.error(f"Failed to handle 500 error notification: {e}")
    
    async def _handle_timeout_error(self, endpoint: str, error_message: str) -> None:
        """Handle timeout errors by sending WhatsApp notifications."""
        try:
            from app.services.global_error_handler import handle_api_error
            await handle_api_error("Procucev API", endpoint, error_message)
        except Exception as e:
            logger.error(f"Failed to handle timeout error notification: {e}")

