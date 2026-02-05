"""
Shared utilities for Celery tasks.
"""

import logging
from typing import List, Set
from datetime import datetime, timedelta

from sqlalchemy import text

from app.database import get_db_session

logger = logging.getLogger(__name__)


def normalize_phone_for_comparison(phone: str) -> str:
    """
    Normalize phone number for comparison by removing + prefix.

    Args:
        phone: Phone number string

    Returns:
        Phone number without + prefix
    """
    if not phone:
        return ""
    return phone.lstrip('+')


def get_users_active_in_last_24hrs(phone_numbers: List[str]) -> Set[str]:
    """
    Get set of phone numbers that were active in the last 24 hours.

    Queries conversation_sessions table to find users who have
    last_activity_at within the last 24 hours.

    Args:
        phone_numbers: List of phone numbers to check (with or without + prefix)

    Returns:
        Set of normalized phone numbers (without +) that were active in last 24hrs
    """
    if not phone_numbers:
        return set()

    db = None
    try:
        db = get_db_session()

        # Normalize all input phone numbers (remove + prefix)
        normalized_phones = [normalize_phone_for_comparison(p) for p in phone_numbers if p]

        if not normalized_phones:
            return set()

        cutoff_time = datetime.utcnow() - timedelta(hours=24)

        # Build parameterized query
        # We check both with and without + prefix since external_user_id format varies
        placeholders = ', '.join([f':phone_{i}' for i in range(len(normalized_phones))])
        params = {f'phone_{i}': phone for i, phone in enumerate(normalized_phones)}
        params['cutoff_time'] = cutoff_time

        # Query checks external_user_id against normalized phones
        # Using REPLACE to strip + from stored values for comparison
        query = text(f"""
            SELECT DISTINCT REPLACE(external_user_id, '+', '') as phone
            FROM conversation_sessions
            WHERE REPLACE(external_user_id, '+', '') IN ({placeholders})
            AND last_activity_at > :cutoff_time
        """)

        result = db.execute(query, params)
        active_phones = {row[0] for row in result.fetchall() if row[0]}

        logger.info(
            f"[TASK_UTILS] Checked {len(normalized_phones)} phones, "
            f"found {len(active_phones)} active in last 24hrs"
        )

        return active_phones

    except Exception as e:
        logger.error(f"[TASK_UTILS] Error checking user activity: {e}")
        # On error, return empty set (assume no one is active, use template for safety)
        return set()
    finally:
        if db:
            db.close()


def is_user_active_in_last_24hrs(phone: str) -> bool:
    """
    Check if a single user was active in the last 24 hours.

    Args:
        phone: Phone number to check

    Returns:
        True if user was active in last 24hrs, False otherwise
    """
    if not phone:
        return False

    active_phones = get_users_active_in_last_24hrs([phone])
    normalized = normalize_phone_for_comparison(phone)
    return normalized in active_phones
