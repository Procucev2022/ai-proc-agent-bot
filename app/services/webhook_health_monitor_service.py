"""
Webhook Health Monitoring Service for ICS WhatsApp API.

This service continuously monitors the health of the ICS WhatsApp API by:
- Calling the /qualitycheck endpoint every 30 seconds
- Detecting performance degradation and outages
- Managing alert state machine to prevent spam
- Sending intelligent email notifications
- Tracking webhook callback health passively

State Machine Flow:
    HEALTHY → FAILING → ALERTING → RECOVERED → HEALTHY
             (grace)   (alert)    (confirm)

Key responsibilities:
- Continuous API health monitoring with configurable intervals
- Response time measurement and classification (OK/WARNING/CRITICAL)
- State machine management with grace periods and recovery confirmation
- Email alert coordination with anti-spam logic
- Graceful degradation when dependencies fail
- Operational logging and history tracking
"""

import asyncio
import logging
import aiohttp
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from enum import Enum

from app.config import get_settings
from app.redis_db import get_redis_service
from app.services.email_service import EmailService

# Create dedicated logger for health monitoring
health_logger = logging.getLogger("api_health_monitor")
health_logger.setLevel(logging.INFO)

# Add date-based file handler (consistent with other services)
from logging.handlers import RotatingFileHandler
import os

log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Use date-based log file naming like other services
log_file = os.path.join(log_dir, f"api_health_monitor_{datetime.now().strftime('%Y-%m-%d')}.log")

# Add file handler if not already present
if not health_logger.handlers:
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    health_logger.addHandler(file_handler)

    # Also log warnings/errors to console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    health_logger.addHandler(console_handler)


class HealthStatus(Enum):
    """API health status classification."""
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class MonitorState(Enum):
    """State machine states for alert management."""
    HEALTHY = "HEALTHY"
    FAILING = "FAILING"
    ALERTING = "ALERTING"
    RECOVERED = "RECOVERED"


class WebhookHealthMonitorService:
    """
    Automated health monitoring service for ICS WhatsApp API.
    
    Runs continuously in the background, checking API health at regular
    intervals and managing alert lifecycle through a state machine.
    
    Implements leader election to ensure only one instance runs when
    multiple application workers are present.
    """
    
    # Redis keys for monitoring state
    STATE_KEY = "webhook:health:state"
    CALLBACK_KEY = "webhook:last_callback_time"
    HISTORY_KEY = "webhook:health:history"
    
    # Redis key for distributed leader election lock
    LEADER_LOCK_KEY = "webhook:health:leader_lock"
    LEADER_LOCK_TTL = 60  # Lock time-to-live in seconds
    
    def __init__(self):
        self.settings = get_settings()
        self.redis = get_redis_service()
        self.email_service = EmailService()
        
        # Monitoring configuration
        self.check_interval = self.settings.webhook_health_check_interval_seconds
        self.response_threshold = self.settings.webhook_api_response_threshold_seconds
        self.api_timeout = self.settings.webhook_api_timeout_seconds
        self.grace_period = self.settings.webhook_failure_grace_period_seconds
        self.recovery_confirmations = self.settings.webhook_recovery_confirmations
        self.warning_threshold = self.settings.webhook_warning_consecutive_threshold
        
        # Alert configuration
        self.alert_recipients = [email.strip() for email in self.settings.webhook_alert_recipients]
        
        # Check if email alerts are configured
        if not self.alert_recipients:
            health_logger.warning(
                "WEBHOOK_ALERT_RECIPIENTS not configured. "
                "Health monitoring will continue but email alerts will be skipped and only logged."
            )
        
        # Control flags
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # Leader election state
        self.worker_id = f"worker_{os.getpid()}"
        self.is_leader = False
        
        # HTTP session for health checks
        self._session: Optional[aiohttp.ClientSession] = None
        
        health_logger.info(  # TEMP_TEST: was debug
            f"WebhookHealthMonitorService initialized: "
            f"worker_id={self.worker_id}, "
            f"check_interval={self.check_interval}s, "
            f"response_threshold={self.response_threshold}s, "
            f"timeout={self.api_timeout}s, "
            f"grace_period={self.grace_period}s"
        )
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session for health checks."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.api_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def _close_session(self):
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def start_monitoring(self):
        """
        Start the health monitoring loop with leader election.
        
        Implements distributed leader election using Redis to ensure only
        one worker instance performs monitoring when multiple application
        workers are running. Non-leader workers remain in standby mode
        and can take over if the leader fails.
        
        This method runs continuously until stop_monitoring() is called.
        It should be started as a background task during application startup.
        """
        if self._running:
            health_logger.warning(f"{self.worker_id}: Health monitoring already running")
            return
        
        if not self.settings.webhook_health_monitoring_enabled:
            health_logger.info(f"{self.worker_id}: Health monitoring disabled by configuration")  # TEMP_TEST: was debug
            return
        
        self._running = True
        health_logger.info(f"{self.worker_id}: Attempting to become monitoring leader")  # TEMP_TEST: was debug
        
        try:
            while self._running:
                if await self._try_acquire_leader_lock():
                    if not self.is_leader:
                        health_logger.info(f"{self.worker_id}: Acquired leader lock, starting monitoring")  # TEMP_TEST: was debug
                        self.is_leader = True
                    
                    await self._run_as_leader()
                else:
                    if self.is_leader:
                        health_logger.warning(f"{self.worker_id}: Lost leader lock, entering standby mode")
                        self.is_leader = False
                    
                    await asyncio.sleep(self.check_interval)
        
        finally:
            await self._release_leader_lock()
            await self._close_session()
            health_logger.info(f"{self.worker_id}: Health monitoring stopped")  # TEMP_TEST: was debug
    
    async def stop_monitoring(self):
        """Stop the health monitoring loop and cleanup resources."""
        health_logger.info(f"{self.worker_id}: Stopping health monitoring")  # TEMP_TEST: was debug
        self._running = False

        # Close aiohttp session
        await self._close_session()
        health_logger.info(f"{self.worker_id}: Cleaned up HTTP session")  # TEMP_TEST: was debug
    
    async def _try_acquire_leader_lock(self) -> bool:
        """
        Attempt to acquire the distributed leader lock.
        
        Uses Redis SET with NX (set if not exists) and EX (expiry) options
        to implement a distributed lock. Only one worker can hold the lock
        at any given time.
        
        Returns:
            bool: True if lock was acquired or already held by this worker,
                  False if another worker holds the lock.
        """
        try:
            await self.redis.init_client()
            
            # Attempt to acquire lock atomically
            lock_acquired = await self.redis.client.set(
                self.LEADER_LOCK_KEY,
                self.worker_id,
                nx=True,  # Only set if key does not exist
                ex=self.LEADER_LOCK_TTL  # Lock expires after TTL seconds
            )
            
            if lock_acquired:
                return True
            
            # Check if this worker already holds the lock
            current_leader = await self.redis.get(self.LEADER_LOCK_KEY)
            if current_leader == self.worker_id:
                return True
            
            return False
        
        except Exception as e:
            health_logger.error(f"{self.worker_id}: Error acquiring leader lock: {e}")
            return False
    
    async def _renew_leader_lock(self) -> bool:
        """
        Renew the leader lock to prevent expiration.
        
        Should be called periodically by the leader worker to maintain
        the lock and prevent other workers from taking over.
        
        Returns:
            bool: True if lock was successfully renewed, False otherwise.
        """
        try:
            await self.redis.init_client()
            
            # Verify this worker still holds the lock before renewing
            current_leader = await self.redis.get(self.LEADER_LOCK_KEY)
            if current_leader != self.worker_id:
                health_logger.warning(
                    f"{self.worker_id}: Cannot renew lock, "
                    f"current leader is {current_leader}"
                )
                return False
            
            # Renew lock expiration
            await self.redis.expire(self.LEADER_LOCK_KEY, self.LEADER_LOCK_TTL)
            return True
        
        except Exception as e:
            health_logger.error(f"{self.worker_id}: Error renewing leader lock: {e}")
            return False
    
    async def _release_leader_lock(self):
        """
        Release the leader lock when stopping monitoring.
        
        Allows another worker to immediately take over leadership
        instead of waiting for lock expiration.
        """
        try:
            await self.redis.init_client()
            
            # Only delete lock if this worker holds it
            current_leader = await self.redis.get(self.LEADER_LOCK_KEY)
            if current_leader == self.worker_id:
                await self.redis.delete(self.LEADER_LOCK_KEY)
                health_logger.info(f"{self.worker_id}: Released leader lock")  # TEMP_TEST: was debug
        
        except Exception as e:
            health_logger.error(f"{self.worker_id}: Error releasing leader lock: {e}")
    
    async def _run_as_leader(self):
        """
        Execute monitoring loop as the leader worker.
        
        Performs health checks and renews the leader lock to maintain
        leadership. If lock renewal fails, relinquishes leadership and
        allows another worker to take over.
        """
        try:
            # Renew lock before performing health check
            if not await self._renew_leader_lock():
                self.is_leader = False
                return
            
            # Perform health check cycle
            await self._health_check_cycle()
            
            # Wait for next check interval
            await asyncio.sleep(self.check_interval)
        
        except Exception as e:
            health_logger.error(f"{self.worker_id}: Error in leader monitoring: {e}", exc_info=True)
            self.is_leader = False
    
    async def _health_check_cycle(self):
        """Execute one complete health check cycle."""
        # Perform API health check
        status, latency_ms, error = await self._check_api_health()
        
        # Get current state
        state = await self._get_state()
        
        # Log check result
        if status == HealthStatus.OK:
            health_logger.info(f"API health check: status=OK, latency={latency_ms}ms")  # TEMP_TEST: was debug
        elif status == HealthStatus.WARNING:
            health_logger.warning(f"API health check: status=WARNING, latency={latency_ms}ms")
        else:
            health_logger.error(f"API health check: status=CRITICAL, error={error}")
        
        # Update state based on check result
        await self._process_check_result(state, status, latency_ms, error)
        
        # Add to history buffer
        await self._add_to_history(status, latency_ms, error)
    
    async def _check_api_health(self) -> tuple[HealthStatus, Optional[float], Optional[str]]:
        """
        Check ICS WhatsApp API health via qualitycheck endpoint.
        
        Returns:
            Tuple of (health_status, latency_ms, error_message)
        """
        url = f"{self.settings.WHATSAPP_BASE_URL}/qualitycheck"
        payload = {
            "user": self.settings.WHATSAPP_USERNAME,
            "pass": self.settings.WHATSAPP_PASSWORD
        }
        
        start_time = datetime.utcnow()
        
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as response:
                latency = (datetime.utcnow() - start_time).total_seconds()
                latency_ms = round(latency * 1000, 2)
                
                # Check response status
                if response.status >= 500:
                    return HealthStatus.CRITICAL, latency_ms, f"Server error: {response.status}"
                
                if response.status >= 400:
                    return HealthStatus.CRITICAL, latency_ms, f"Client error: {response.status}"
                
                if response.status == 200:
                    # Check response time threshold
                    if latency >= self.response_threshold:
                        return HealthStatus.WARNING, latency_ms, f"Slow response: {latency:.2f}s"
                    else:
                        return HealthStatus.OK, latency_ms, None
                
                return HealthStatus.CRITICAL, latency_ms, f"Unexpected status: {response.status}"
        
        except asyncio.TimeoutError:
            latency = (datetime.utcnow() - start_time).total_seconds()
            latency_ms = round(latency * 1000, 2)
            return HealthStatus.CRITICAL, latency_ms, f"Timeout after {self.api_timeout}s"
        
        except aiohttp.ClientError as e:
            latency = (datetime.utcnow() - start_time).total_seconds()
            latency_ms = round(latency * 1000, 2)
            return HealthStatus.CRITICAL, latency_ms, f"Connection error: {str(e)}"
        
        except Exception as e:
            latency = (datetime.utcnow() - start_time).total_seconds()
            latency_ms = round(latency * 1000, 2)
            return HealthStatus.CRITICAL, latency_ms, f"Unexpected error: {str(e)}"
    
    async def _get_state(self) -> Dict[str, Any]:
        """Get current monitoring state from Redis."""
        try:
            await self.redis.init_client()
            state_json = await self.redis.get(self.STATE_KEY)
            
            if state_json:
                return json.loads(state_json)
            else:
                # Initialize default state
                return {
                    "current_state": MonitorState.HEALTHY.value,
                    "is_alerting": False,
                    "last_alert_time": None,
                    "failure_start_time": None,
                    "consecutive_failures": 0,
                    "consecutive_successes": 0,
                    "consecutive_warnings": 0,
                    "last_severity": HealthStatus.OK.value,
                    "last_check_time": None,
                    "last_latency_ms": None,
                    "last_error": None
                }
        
        except Exception as e:
            health_logger.error(f"Error getting state from Redis: {e}")
            # Return default state for graceful degradation
            return {
                "current_state": MonitorState.HEALTHY.value,
                "is_alerting": False,
                "last_alert_time": None,
                "failure_start_time": None,
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "consecutive_warnings": 0,
                "last_severity": HealthStatus.OK.value,
                "last_check_time": None,
                "last_latency_ms": None,
                "last_error": None
            }
    
    async def _save_state(self, state: Dict[str, Any]):
        """Save monitoring state to Redis."""
        try:
            await self.redis.init_client()
            state_json = json.dumps(state)
            await self.redis.set(
                self.STATE_KEY,
                state_json,
                ex=self.settings.webhook_alert_state_ttl_seconds
            )
        except Exception as e:
            health_logger.error(f"Error saving state to Redis: {e}")
            # Continue monitoring even if Redis fails (graceful degradation)
    
    async def _process_check_result(
        self,
        state: Dict[str, Any],
        status: HealthStatus,
        latency_ms: Optional[float],
        error: Optional[str]
    ):
        """
        Process health check result and update state machine.
        
        Implements state transitions and alert logic.
        """
        current_state = MonitorState(state["current_state"])
        now = datetime.utcnow().isoformat()
        
        # Update basic state
        state["last_check_time"] = now
        state["last_latency_ms"] = latency_ms
        state["last_error"] = error
        state["last_severity"] = status.value
        
        # State machine transitions
        if status == HealthStatus.OK:
            await self._handle_ok_status(state, current_state)
        
        elif status == HealthStatus.WARNING:
            await self._handle_warning_status(state, current_state)
        
        elif status == HealthStatus.CRITICAL:
            await self._handle_critical_status(state, current_state)
        
        # Save updated state
        await self._save_state(state)
    
    async def _handle_ok_status(self, state: Dict[str, Any], current_state: MonitorState):
        """Handle OK health status in state machine."""
        state["consecutive_failures"] = 0
        state["consecutive_warnings"] = 0
        state["consecutive_successes"] += 1
        
        if current_state == MonitorState.HEALTHY:
            # Stay healthy
            pass
        
        elif current_state == MonitorState.FAILING:
            # Recovered before alert threshold
            health_logger.info("State transition: FAILING → HEALTHY (recovered within grace period)")  # TEMP_TEST: was debug
            state["current_state"] = MonitorState.HEALTHY.value
            state["failure_start_time"] = None
        
        elif current_state == MonitorState.ALERTING:
            # Start recovery confirmation
            health_logger.info("State transition: ALERTING → RECOVERED (recovery detected)")  # TEMP_TEST: was debug
            state["current_state"] = MonitorState.RECOVERED.value
            state["consecutive_successes"] = 1  # Reset counter
        
        elif current_state == MonitorState.RECOVERED:
            # Check if recovery confirmed (need N consecutive successes)
            if state["consecutive_successes"] >= self.recovery_confirmations:
                health_logger.info(  # TEMP_TEST: was debug
                    f"State transition: RECOVERED → HEALTHY "
                    f"(recovery confirmed with {self.recovery_confirmations} checks)"
                )
                state["current_state"] = MonitorState.HEALTHY.value
                state["is_alerting"] = False
                state["failure_start_time"] = None
                
                # Send recovery notification
                await self._send_recovery_notification(state)
    
    async def _handle_warning_status(self, state: Dict[str, Any], current_state: MonitorState):
        """Handle WARNING health status in state machine."""
        state["consecutive_successes"] = 0
        state["consecutive_failures"] += 1
        state["consecutive_warnings"] += 1
        
        if current_state == MonitorState.HEALTHY:
            # Start tracking potential failure
            health_logger.warning("State transition: HEALTHY → FAILING (WARNING)")
            state["current_state"] = MonitorState.FAILING.value
            state["failure_start_time"] = datetime.utcnow().isoformat()
        
        elif current_state == MonitorState.FAILING:
            # Check if grace period expired
            failure_start = datetime.fromisoformat(state["failure_start_time"])
            elapsed = (datetime.utcnow() - failure_start).total_seconds()
            
            if elapsed >= self.grace_period:
                # Check if warning threshold met
                if state["consecutive_warnings"] >= self.warning_threshold:
                    health_logger.warning("State transition: FAILING → ALERTING (WARNING threshold met)")
                    state["current_state"] = MonitorState.ALERTING.value
                    state["is_alerting"] = True
                    
                    # Send warning alert
                    await self._send_warning_alert(state)
        
        elif current_state == MonitorState.RECOVERED:
            # Relapse during recovery - strict recovery policy
            health_logger.warning("State transition: RECOVERED → ALERTING (relapse detected)")
            state["current_state"] = MonitorState.ALERTING.value
            state["consecutive_successes"] = 0
            
            # Send relapse alert
            await self._send_relapse_alert(state)
    
    async def _handle_critical_status(self, state: Dict[str, Any], current_state: MonitorState):
        """Handle CRITICAL health status in state machine."""
        state["consecutive_successes"] = 0
        state["consecutive_warnings"] = 0
        state["consecutive_failures"] += 1
        
        if current_state == MonitorState.HEALTHY:
            # Immediate transition to failing
            health_logger.error("State transition: HEALTHY → FAILING (CRITICAL)")
            state["current_state"] = MonitorState.FAILING.value
            state["failure_start_time"] = datetime.utcnow().isoformat()
        
        elif current_state == MonitorState.FAILING:
            # Check if grace period expired
            failure_start = datetime.fromisoformat(state["failure_start_time"])
            elapsed = (datetime.utcnow() - failure_start).total_seconds()
            
            if elapsed >= self.grace_period and not state["is_alerting"]:
                health_logger.error("State transition: FAILING → ALERTING (CRITICAL after grace period)")
                state["current_state"] = MonitorState.ALERTING.value
                state["is_alerting"] = True
                
                # Send critical alert
                await self._send_critical_alert(state)
        
        elif current_state == MonitorState.RECOVERED:
            # Relapse during recovery
            health_logger.error("State transition: RECOVERED → ALERTING (relapse detected)")
            state["current_state"] = MonitorState.ALERTING.value
            state["consecutive_successes"] = 0
            
            # Send relapse alert
            await self._send_relapse_alert(state)
    
    async def _send_critical_alert(self, state: Dict[str, Any]):
        """Send critical alert email."""
        # Skip if no alert recipients configured
        if not self.alert_recipients:
            health_logger.warning(
                "CRITICAL ALERT (email skipped - no recipients configured): "
                f"API down for {state.get('consecutive_failures', 0)} checks, "
                f"error: {state.get('last_error', 'Unknown')}"
            )
            return
        
        try:
            # Calculate downtime
            failure_start = datetime.fromisoformat(state["failure_start_time"])
            downtime_duration = self._format_duration(datetime.utcnow() - failure_start)
            
            # Prepare email variables
            variables = {
                "alert_recipients": ",".join(self.alert_recipients),
                "alert_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "downtime_duration": downtime_duration,
                "last_success_time": state.get("last_check_time", "Unknown"),
                "error_details": state.get("last_error", "API not responding"),
                "consecutive_failures": state["consecutive_failures"]
            }
            
            health_logger.warning(f"Sending CRITICAL alert email to {self.alert_recipients}")
            
            result = await self.email_service.send_email_by_template(
                "webhook_api_critical",
                variables
            )
            
            if result.get("status") == "Success":
                state["last_alert_time"] = datetime.utcnow().isoformat()
                health_logger.debug("Critical alert email sent successfully")
            else:
                health_logger.error(f"Failed to send critical alert: {result}")
        
        except Exception as e:
            health_logger.error(f"Error sending critical alert: {e}", exc_info=True)
    
    async def _send_warning_alert(self, state: Dict[str, Any]):
        """Send warning alert email."""
        # Skip if no alert recipients configured
        if not self.alert_recipients:
            health_logger.warning(
                "WARNING ALERT (email skipped - no recipients configured): "
                f"Slow API response {state.get('last_latency_ms', 0)}ms, "
                f"{state.get('consecutive_warnings', 0)} consecutive slow checks"
            )
            return
        
        try:
            variables = {
                "alert_recipients": ",".join(self.alert_recipients),
                "alert_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "response_time": f"{state['last_latency_ms']}ms",
                "consecutive_slow_checks": state["consecutive_warnings"],
                "threshold": f"{self.response_threshold}s"
            }
            
            health_logger.warning(f"Sending WARNING alert email to {self.alert_recipients}")
            
            result = await self.email_service.send_email_by_template(
                "webhook_api_warning",
                variables
            )
            
            if result.get("status") == "Success":
                state["last_alert_time"] = datetime.utcnow().isoformat()
                health_logger.debug("Warning alert email sent successfully")
            else:
                health_logger.error(f"Failed to send warning alert: {result}")
        
        except Exception as e:
            health_logger.error(f"Error sending warning alert: {e}", exc_info=True)
    
    async def _send_recovery_notification(self, state: Dict[str, Any]):
        """Send recovery notification email."""
        # Skip if no alert recipients configured
        if not self.alert_recipients:
            health_logger.debug(
                "RECOVERY NOTIFICATION (email skipped - no recipients configured): "
                f"API recovered, response time now {state.get('last_latency_ms', 0)}ms"
            )
            return
        
        try:
            # Calculate total downtime
            if state.get("failure_start_time"):
                failure_start = datetime.fromisoformat(state["failure_start_time"])
                total_downtime = self._format_duration(datetime.utcnow() - failure_start)
            else:
                total_downtime = "Unknown"
            
            variables = {
                "alert_recipients": ",".join(self.alert_recipients),
                "recovery_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "total_downtime": total_downtime,
                "current_response_time": f"{state['last_latency_ms']}ms"
            }
            
            health_logger.debug(f"Sending recovery notification email to {self.alert_recipients}")
            
            result = await self.email_service.send_email_by_template(
                "webhook_api_recovery",
                variables
            )
            
            if result.get("status") == "Success":
                health_logger.debug("Recovery notification email sent successfully")
            else:
                health_logger.error(f"Failed to send recovery notification: {result}")
        
        except Exception as e:
            health_logger.error(f"Error sending recovery notification: {e}", exc_info=True)
    
    async def _send_relapse_alert(self, state: Dict[str, Any]):
        """Send relapse alert email."""
        # Skip if no alert recipients configured
        if not self.alert_recipients:
            health_logger.warning(
                "RELAPSE ALERT (email skipped - no recipients configured): "
                f"API degraded again, severity: {state.get('last_severity', 'Unknown')}, "
                f"error: {state.get('last_error', 'Unknown')}"
            )
            return
        
        try:
            variables = {
                "alert_recipients": ",".join(self.alert_recipients),
                "relapse_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "error_details": state.get("last_error", "API health degraded again"),
                "last_severity": state.get("last_severity", HealthStatus.CRITICAL.value)
            }
            
            health_logger.warning(f"Sending RELAPSE alert email to {self.alert_recipients}")
            
            result = await self.email_service.send_email_by_template(
                "webhook_api_relapse",
                variables
            )
            
            if result.get("status") == "Success":
                state["last_alert_time"] = datetime.utcnow().isoformat()
                health_logger.debug("Relapse alert email sent successfully")
            else:
                health_logger.error(f"Failed to send relapse alert: {result}")
        
        except Exception as e:
            health_logger.error(f"Error sending relapse alert: {e}", exc_info=True)
    
    async def _add_to_history(
        self,
        status: HealthStatus,
        latency_ms: Optional[float],
        error: Optional[str]
    ):
        """Add health check result to history buffer (last 100 checks)."""
        try:
            await self.redis.init_client()
            
            # Create history entry
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "status": status.value,
                "latency_ms": latency_ms,
                "error": error
            }
            
            # Get existing history
            history_json = await self.redis.get(self.HISTORY_KEY)
            if history_json:
                history = json.loads(history_json)
            else:
                history = []
            
            # Add new entry
            history.append(entry)
            
            # Keep only last 100 entries (circular buffer)
            if len(history) > 100:
                history = history[-100:]
            
            # Save updated history
            history_json = json.dumps(history)
            await self.redis.set(
                self.HISTORY_KEY,
                history_json,
                ex=86400  # 24 hours
            )
        
        except Exception as e:
            health_logger.error(f"Error adding to history: {e}")
            # Non-critical, continue monitoring
    
    @staticmethod
    def _format_duration(delta: timedelta) -> str:
        """Format timedelta as human-readable duration."""
        total_seconds = int(delta.total_seconds())
        
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0 or not parts:
            parts.append(f"{seconds}s")
        
        return " ".join(parts)
