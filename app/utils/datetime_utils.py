"""
Datetime utilities for consistent UTC timezone handling.

All operations use UTC timezone only.
"""

from datetime import datetime, timedelta
import pytz
from typing import Optional

# Constants
UTC = pytz.UTC

def utc_now() -> datetime:
    """Get current time in UTC (timezone-aware)."""
    return datetime.now(UTC)

def utc_from_naive(dt: datetime) -> datetime:
    """Convert naive datetime to UTC (assumes input was already in UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

def format_utc_display(dt: datetime, format_str: str = "%Y-%m-%d %H:%M:%S UTC") -> str:
    """Format datetime for display in UTC."""
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        # Assume naive datetime is UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime(format_str)

def format_utc_time_only(dt: datetime) -> str:
    """Format datetime showing only time in UTC (for compact display)."""
    if dt is None:
        return "N/A"
    return format_utc_display(dt, "%H:%M UTC")

def format_utc_short(dt: datetime) -> str:
    """Format datetime in short format with UTC."""
    if dt is None:
        return "N/A"
    return format_utc_display(dt, "%d/%m %H:%M UTC")

def is_expired(last_activity: datetime, timeout_hours: int) -> tuple[bool, datetime, datetime]:
    """
    Check if session is expired using UTC comparison.
    
    Returns:
        tuple: (is_expired, last_activity_utc, expired_threshold_utc)
    """
    if last_activity is None:
        return True, None, utc_now()
    
    # Ensure both times are in UTC for comparison
    last_activity_utc = utc_from_naive(last_activity)
    current_utc = utc_now()
    expired_threshold_utc = current_utc - timedelta(hours=timeout_hours)
    
    is_expired = last_activity_utc < expired_threshold_utc
    
    return is_expired, last_activity_utc, expired_threshold_utc