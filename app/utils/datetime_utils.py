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

def format_date_display(dt: datetime) -> str:
    """Format date in DD Month YYYY format (e.g., 5 Sep 2025)."""
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%d %b %Y").lstrip('0')

def format_date_for_validation_error(date_str: str) -> str:
    """Format date string for validation error messages (e.g., 12 Sept 2025)."""
    if not date_str:
        return "N/A"
    
    try:
        # Parse the date string (assuming YYYY-MM-DD format)
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d %b %Y").lstrip('0')
    except:
        # If parsing fails, return as-is
        return date_str

def add_business_days(start_date: datetime, business_days: int) -> datetime:
    """
    Add business days (Monday-Friday) to a date, skipping weekends.
    
    Args:
        start_date: Starting date
        business_days: Number of business days to add
        
    Returns:
        Date after adding business days
    """
    if start_date is None or business_days <= 0:
        return start_date
    
    current_date = start_date
    days_added = 0
    
    while days_added < business_days:
        current_date += timedelta(days=1)
        # Monday = 0, Sunday = 6
        if current_date.weekday() < 5:  # Monday to Friday
            days_added += 1
    
    return current_date

def calculate_working_days_from_now(working_days: int) -> str:
    """
    Calculate date that is N working days from now.
    
    Args:
        working_days: Number of working days to add
        
    Returns:
        Date string in YYYY-MM-DD format
    """
    current_date = datetime.now().date()
    result_date = add_business_days(datetime.combine(current_date, datetime.min.time()), working_days)
    return result_date.strftime("%Y-%m-%d")

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