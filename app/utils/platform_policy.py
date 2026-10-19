"""
Backend platform policy.

The bot talks to one of two procurement backends, chosen by BACKEND_PLATFORM:

- "qua" (default): the QUA Java backend at GMT_BASE_URL, plus direct reads of
  its MySQL schema. This is the original setup and nothing changes under it.
- "equa": Enterprise QUA. GMT_BASE_URL points at its QUA-compatible bridge
  (https://<backend>/api/bot), which answers the same endpoints with the same
  response shapes, and every request carries BOT_API_KEY. EQUA keeps its data in
  Cloudflare D1, which the bot cannot query, so the features that read QUA's
  MySQL schema directly are switched off by default:
    * Buy From Stock (BFS) — EQUA has no stock catalogue.
    * Seller matching / vector sync / category rebuild Celery tasks — EQUA
      matches and notifies vendors itself when an RFQ is created.

Each of those keeps its own environment flag (ENABLE_BFS,
ENABLE_SELLER_MATCHING, ...), so any one can still be forced on or off; the
platform only changes the default.
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUA = "qua"
EQUA = "equa"
SUPPORTED_PLATFORMS = (QUA, EQUA)

# Header the EQUA bridge authenticates the bot with.
BOT_API_KEY_HEADER = "X-Bot-Api-Key"

BFS_SEARCH_BUTTON_ID = "search_bfs"
BFS_BUTTON_PREFIX = "bfs_"
BFS_UNAVAILABLE_STATUS = "bfs_unavailable"
BFS_UNAVAILABLE_MESSAGE = (
    "Ready-stock search is not available right now. "
    "You can check your RFQ status here, or raise a requirement on the Procucev portal."
)

# Shown when hiding BFS buttons leaves a message with none, since WhatsApp
# rejects an interactive message without buttons.
FALLBACK_BUTTONS: List[Dict[str, str]] = [{"id": "rfq_status", "title": "Check RFQ Status"}]


def normalize_platform(value: Optional[str]) -> str:
    """Return a supported platform name; anything unrecognised means QUA."""
    platform = (value or QUA).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        logger.warning(f"[PLATFORM] Unknown BACKEND_PLATFORM '{value}', using '{QUA}'")
        return QUA
    return platform


def platform_default(qua_value: str, equa_value: str, platform: Optional[str] = None) -> str:
    """The default for a setting whose sensible value depends on the platform."""
    resolved = normalize_platform(platform if platform is not None else os.getenv("BACKEND_PLATFORM"))
    return equa_value if resolved == EQUA else qua_value


def _resolve_settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    try:
        from app.config import get_settings
        return get_settings()
    except Exception as e:
        logger.error(f"[PLATFORM] Could not load settings, falling back to environment: {e}")
        return None


def get_backend_platform(settings: Any = None) -> str:
    resolved = _resolve_settings(settings)
    platform = getattr(resolved, "backend_platform", None)
    # Only a real value counts; anything else (unset, a stand-in object) defers to the environment.
    return normalize_platform(platform if isinstance(platform, str) else os.getenv("BACKEND_PLATFORM"))


def is_equa(settings: Any = None) -> bool:
    return get_backend_platform(settings) == EQUA


def is_bfs_enabled(settings: Any = None) -> bool:
    """True when Buy From Stock is offered. Defaults to on for QUA, off for EQUA."""
    resolved = _resolve_settings(settings)
    flag = getattr(resolved, "enable_bfs", None)
    if flag is None:
        flag = os.getenv("ENABLE_BFS", platform_default("true", "false")).strip().lower() == "true"
    return bool(flag)


def is_bfs_button(button_id: Optional[str]) -> bool:
    button_id = button_id or ""
    return button_id == BFS_SEARCH_BUTTON_ID or button_id.startswith(BFS_BUTTON_PREFIX)


def filter_bfs_buttons(buttons: Optional[List[Dict[str, str]]], settings: Any = None) -> Optional[List[Dict[str, str]]]:
    """Drop BFS buttons from an outgoing button message while BFS is disabled."""
    if not buttons or is_bfs_enabled(settings):
        return buttons
    kept = [button for button in buttons if not is_bfs_button(button.get("id"))]
    if len(kept) == len(buttons):
        return buttons
    logger.info(f"[PLATFORM] Hid {len(buttons) - len(kept)} BFS button(s): BFS is disabled")
    return kept or list(FALLBACK_BUTTONS)


def bot_api_headers(settings: Any = None) -> Dict[str, str]:
    """Headers that authenticate the bot to the EQUA bridge; empty when no key is set."""
    resolved = _resolve_settings(settings)
    key = getattr(resolved, "bot_api_key", None) if resolved is not None else None
    if key is None:
        key = os.getenv("BOT_API_KEY")
    return {BOT_API_KEY_HEADER: key} if key else {}
