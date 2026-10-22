"""
WhatsApp RFQ-creation policy.

Buyers create RFQs only on the Procucev Enterprise website or by emailing
rfq@procucev.com. WhatsApp RFQ creation stays off unless
ENABLE_WHATSAPP_RFQ_CREATION=true, and every check here fails closed: a
missing or unreadable setting means creation is disabled.

Guards sit at each entry point (typed purchase intent, RFQ buttons, BFS
"Raise RFQ", Excel upload, sectioned workflow) and at the single call to the
RFQ-create API, so a path that slips past an entry point still never reaches
the backend.
"""

import logging
import re
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RFQ_EMAIL = "rfq@procucev.com"
DEFAULT_PORTAL_URL = "https://p2pdevuiindia.azurewebsites.net/login"
RFQ_CREATION_DISABLED_STATUS = "rfq_creation_disabled"
# Login address named in the buyer message; BUYER_PORTAL_DISPLAY_URL overrides it.
DEFAULT_BUYER_PORTAL_DISPLAY = "https://equa.procucev.com"

# Button ids that start an RFQ. Hidden from every outgoing button message
# while WhatsApp RFQ creation is disabled.
RFQ_CREATION_BUTTON_IDS = frozenset({"create_rfq", "new_rfq", "raise_rfq", "bfs_raise_rfq"})

# Shown in place of the menu when hiding the RFQ buttons leaves a message
# with none, since WhatsApp rejects an interactive message without buttons.
FALLBACK_BUTTONS: List[Dict[str, str]] = [
    {"id": "rfq_status", "title": "Check RFQ Status"},
    {"id": "search_bfs", "title": "Search Ready Stocks"},
]


def _resolve_settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    try:
        from app.config import get_settings
        return get_settings()
    except Exception as e:
        logger.error(f"[RFQ_POLICY] Could not load settings, treating RFQ creation as disabled: {e}")
        return None


def is_rfq_creation_enabled(settings: Any = None) -> bool:
    """True only when WhatsApp RFQ creation is explicitly enabled.

    A settings object without the flag (or settings that fail to load) falls
    back to the ENABLE_WHATSAPP_RFQ_CREATION environment variable, which
    defaults to disabled.
    """
    resolved = _resolve_settings(settings)
    flag = getattr(resolved, "enable_whatsapp_rfq_creation", None)
    if flag is None:
        flag = os.getenv("ENABLE_WHATSAPP_RFQ_CREATION", "false").strip().lower() == "true"
    return bool(flag)


def is_buyer_whatsapp_enabled(settings: Any = None) -> bool:
    """True only when buyer services on WhatsApp (menus, RFQ status, ready stocks) are explicitly enabled.

    While disabled, buyers can still register and verify on WhatsApp, but every
    buyer session ends with the portal/email template.
    """
    resolved = _resolve_settings(settings)
    flag = getattr(resolved, "enable_whatsapp_buyer_services", None)
    if flag is None:
        flag = os.getenv("ENABLE_WHATSAPP_BUYER_SERVICES", "false").strip().lower() == "true"
    return bool(flag)


def get_portal_url(settings: Any = None) -> str:
    resolved = _resolve_settings(settings)
    return getattr(resolved, "PROCUCEV_PORTAL_URL", None) or DEFAULT_PORTAL_URL


def rfq_policy_notice(settings: Any = None) -> str:
    """Short policy note appended to buyer menus and welcome messages."""
    return (
        "📌 *Important RFQ Policy:*\n"
        f"Buyers can create RFQs only through the *Procucev Enterprise website* "
        f"({get_portal_url(settings)}) or by emailing *{RFQ_EMAIL}*."
    )


def faq_rfq_channels_text(settings: Any = None) -> str:
    """Where buyers raise RFQs, as stated in the FAQ context the LLM answers from."""
    if is_rfq_creation_enabled(settings):
        return "Raise, track, and customize RFQs (quotes) through the WhatsApp AI Agent or the web portal."
    return (
        "Buyers create RFQs (quotes) only through the Procucev Enterprise website "
        f"({get_portal_url(settings)}) or by emailing {RFQ_EMAIL}; RFQs cannot be created on WhatsApp. "
        "On WhatsApp, buyers can check RFQ status and search ready stocks."
    )


def rfq_creation_disabled_message(settings: Any = None) -> str:
    """Full redirect sent when a buyer tries to create an RFQ on WhatsApp."""
    return (
        "📌 *Important RFQ Policy:*\n\n"
        "Buyers can create RFQs only through the *Procucev Enterprise website* "
        f"or by emailing *{RFQ_EMAIL}*.\n\n"
        f"🔗 *Portal:* {get_portal_url(settings)}\n"
        f"✉️ *Email:* {RFQ_EMAIL}\n\n"
        "WhatsApp no longer accepts RFQ creation requests directly through messages, buttons, or Excel uploads."
    )


def get_buyer_portal_display(settings: Any = None) -> str:
    """Address buyers are told to log in at, as written in the buyer message."""
    resolved = _resolve_settings(settings)
    configured = getattr(resolved, "buyer_portal_display_url", None)
    if not isinstance(configured, str) or not configured.strip():
        configured = os.getenv("BUYER_PORTAL_DISPLAY_URL", "")
    return configured.strip() or DEFAULT_BUYER_PORTAL_DISPLAY


def _registration_details(email: Any, phone: Any) -> str:
    return (
        "Registration Details:\n\n"
        f"Username / Email: {email}\n"
        f"Registered Mobile: {format_registered_mobile(phone)}"
    )


def _rfq_channels(settings: Any = None) -> str:
    return (
        "Your buyer profile has just been upgraded with AI to simplify your experience. "
        f"To raise an RFQ, please email us at {RFQ_EMAIL} or log in at {get_buyer_portal_display(settings)}."
    )


def buyer_portal_message(settings: Any = None, email: Any = None, phone: Any = None) -> str:
    """The only reply a buyer gets on WhatsApp while buyer services are disabled.

    With the buyer's email, their registration details come first.
    """
    parts = [_registration_details(email, phone)] if email else []
    parts.append(_rfq_channels(settings))
    return "\n\n".join(parts)


def format_registered_mobile(phone: Any) -> str:
    """+91 XXXXXXXXXX for an Indian number; other numbers with a leading +."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:]}"
    if len(digits) == 10:
        return f"+91 {digits}"
    return f"+{digits}" if digits else ""


def buyer_registration_message(email: Any, phone: Any, settings: Any = None) -> str:
    """What a buyer is told when their WhatsApp registration completes (buyer services off)."""
    return (
        "Thank you! Your registration has been received.\n\n"
        f"{_registration_details(email, phone)}\n\n"
        f"{_rfq_channels(settings)}\n\n"
        "For your login credentials, please check your registered email."
    )


async def redirect_buyer_to_portal(whatsapp_service: Any, exit_service: Any, phone_number: str, session: Any,
                                   settings: Any = None, intro: Optional[str] = None,
                                   message: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
    """Send the buyer the portal/email template, then end the session.

    Ending the session clears the auth token, so the next "Hi" starts a fresh
    profile selection (a seller profile on the same number stays reachable).
    The returned status is one chat_service already treats as a finished exit.
    """
    message = message or buyer_portal_message(settings, email=email, phone=phone_number)
    if intro:
        message = f"{intro}\n\n{message}"
    await whatsapp_service.send_message(phone_number, message, session_id=session)
    await exit_service.handle_exit_intent(phone_number, session, show_message=False)
    return {
        "status": "user_exited",
        "exit_completed": True,
        "reason": "buyer_whatsapp_services_disabled",
        "message": message,
    }


def filter_rfq_creation_buttons(buttons: Optional[List[Dict[str, Any]]],
                                settings: Any = None) -> Optional[List[Dict[str, Any]]]:
    """Drop RFQ-creation buttons when creation is disabled; never return an empty list."""
    if not buttons or is_rfq_creation_enabled(settings):
        return buttons
    kept = [b for b in buttons if b.get("id") not in RFQ_CREATION_BUTTON_IDS]
    if len(kept) == len(buttons):
        return buttons
    return kept or [dict(b) for b in FALLBACK_BUTTONS]


def clear_rfq_workflow(session: Any) -> None:
    """Drop any in-progress RFQ so the session stops routing back into RFQ creation."""
    if session is None:
        return
    from app.models import WorkflowType
    from app.services.workflow_manager import WorkflowManager

    if getattr(session, "workflow_state", None):
        WorkflowManager.safe_reset_workflow_state(session, caller="rfq_policy")
    if getattr(session, "workflow_type", None) == WorkflowType.rfq_creation:
        session.workflow_type = None


async def send_rfq_creation_disabled(whatsapp_service: Any, phone_number: str, session: Any = None,
                                     settings: Any = None, message: Optional[str] = None) -> Dict[str, Any]:
    """Tell the buyer where to create RFQs, reset any RFQ workflow, and report the block."""
    redirect_msg = message or rfq_creation_disabled_message(settings)
    clear_rfq_workflow(session)
    await whatsapp_service.send_configurable_buttons(
        phone_number,
        redirect_msg,
        [dict(b) for b in FALLBACK_BUTTONS],
        session_id=session
    )
    return {"status": RFQ_CREATION_DISABLED_STATUS, "message": redirect_msg}
