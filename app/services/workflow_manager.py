"""
Workflow State Manager Service.

Centralized workflow state management to prevent state corruption, data loss,
and inconsistent workflow transitions. Provides type-safe, validated operations
for all workflow state changes.

Key responsibilities:
- Manage workflow types with enum validation
- Control workflow stages within each workflow
- Manage pending flags with lifecycle tracking
- Validate workflow transitions
- Prevent data loss during state changes
- Provide audit trail for all state modifications
"""

import logging
from enum import Enum
from typing import Optional, Dict, Any, List, Set
from datetime import datetime
import inspect

from app.models import ConversationSession, WorkflowType
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class WorkflowStage(Enum):
    """Fine-grained stages within workflows."""
    COLLECTING = "collecting"
    CONFIRMING = "confirming"
    SUBMITTING = "submitting"
    COMPLETED = "completed"
    EXCEL_REUPLOAD_REQUIRED = "excel_reupload_required"
    AWAITING_INPUT = "awaiting_input"


class PendingFlag(Enum):
    """Typed pending flags with clear semantics."""
    # User decision flags (awaiting user response)
    ROLE_SWITCH = "pending_role_switch"
    ACCOUNT_SWITCH = "pending_account_switch"
    INTENT_SWITCH = "pending_intent_switch"
    AUTH_REG_SWITCH = "pending_auth_reg_switch"
    ATTACHMENT_DECISION = "awaiting_attachment_decision"

    # RFQ confirmation flags (product data awaiting confirmation)
    RFQ = "pending_rfq"
    COMBINED_RFQ = "pending_combined_rfq"
    OPTIONAL_RFQ = "pending_optional_rfq"
    OPTIONAL_COMBINED_RFQ = "pending_optional_combined_rfq"

    # Other workflow flags
    EXCEL_REUPLOAD = "pending_excel_reupload"
    REGISTRATION_DATA = "pending_registration_data"
    ACCOUNT_CHANGE = "pending_account_change"


class WorkflowManager:
    """
    Centralized workflow state management.

    All workflow state modifications should go through this manager to ensure:
    - Type safety (enum-based)
    - Validation (enforce valid transitions)
    - Audit trail (log all changes)
    - Data preservation (prevent accidental data loss)
    """

    # Define valid workflow transitions
    VALID_TRANSITIONS: Dict[WorkflowType, Set[WorkflowType]] = {
        WorkflowType.authentication: {
            WorkflowType.registration,
            WorkflowType.rfq_creation,
            WorkflowType.general_inquiry,
            WorkflowType.seller_rfq_view,
            WorkflowType.rfq_status_check,
        },
        WorkflowType.registration: {
            WorkflowType.rfq_creation,
            WorkflowType.general_inquiry,
            WorkflowType.seller_rfq_view,
            WorkflowType.authentication,
        },
        WorkflowType.rfq_creation: {
            WorkflowType.rfq_submitted,
            WorkflowType.rfq_status_check,
            WorkflowType.general_inquiry,
            WorkflowType.excel_rfq_upload,
        },
        WorkflowType.rfq_submitted: {
            WorkflowType.rfq_status_check,
            WorkflowType.rfq_creation,
            WorkflowType.general_inquiry,
        },
        WorkflowType.rfq_status_check: {
            WorkflowType.rfq_creation,
            WorkflowType.general_inquiry,
        },
        WorkflowType.general_inquiry: {
            WorkflowType.rfq_creation,
            WorkflowType.rfq_status_check,
            WorkflowType.seller_rfq_view,
            WorkflowType.authentication,
            WorkflowType.registration,
        },
        WorkflowType.seller_rfq_view: {
            WorkflowType.general_inquiry,
            WorkflowType.rfq_status_check,
        },
        WorkflowType.excel_rfq_upload: {
            WorkflowType.rfq_creation,
            WorkflowType.rfq_submitted,
            WorkflowType.general_inquiry,
        },
        WorkflowType.product_search: {
            WorkflowType.rfq_creation,
            WorkflowType.general_inquiry,
        },
        WorkflowType.user_exit: {
            WorkflowType.authentication,
            WorkflowType.general_inquiry,
        },
    }

    # Fields that should NEVER be cleared without explicit permission
    PROTECTED_FIELDS = {
        'extracted_entities',
        'complete_products',
        'conversation_history',
    }

    # ===== WORKFLOW TYPE MANAGEMENT =====

    @staticmethod
    def set_workflow_type(session: ConversationSession, workflow_type: WorkflowType,
                         caller: Optional[str] = None) -> bool:
        """
        Set workflow type with validation and logging.

        Args:
            session: Conversation session
            workflow_type: New workflow type (must be WorkflowType enum)
            caller: Optional caller identification for logging

        Returns:
            True if successful, False if invalid
        """
        if not isinstance(workflow_type, WorkflowType):
            logger.error(f"[WORKFLOW_ERROR] Attempted to set workflow_type as non-enum: {workflow_type} (type: {type(workflow_type)})")
            # Try to convert string to enum
            try:
                workflow_type = WorkflowType(workflow_type)
            except (ValueError, KeyError):
                logger.error(f"[WORKFLOW_ERROR] Cannot convert '{workflow_type}' to WorkflowType enum")
                return False

        old_type = WorkflowManager.get_workflow_type(session)
        caller_info = caller or inspect.stack()[1].function

        logger.debug(f"[WORKFLOW_TRANSITION] Session {session.session_id}: "
                    f"{old_type} → {workflow_type.value} (caller: {caller_info})")

        session.workflow_type = workflow_type
        return True

    @staticmethod
    def get_workflow_type(session: ConversationSession) -> Optional[WorkflowType]:
        """
        Get workflow type as enum, handling both enum and string values.

        Args:
            session: Conversation session

        Returns:
            WorkflowType enum or None
        """
        if not session.workflow_type:
            return None

        # Already an enum
        if isinstance(session.workflow_type, WorkflowType):
            return session.workflow_type

        # Convert string to enum
        try:
            return WorkflowType(session.workflow_type)
        except (ValueError, KeyError):
            logger.warning(f"[WORKFLOW_WARNING] Invalid workflow_type value: {session.workflow_type}")
            return None

    @staticmethod
    def transition_workflow(session: ConversationSession, new_type: WorkflowType,
                          new_stage: Optional[WorkflowStage] = None,
                          validate: bool = True,
                          caller: Optional[str] = None) -> bool:
        """
        Safely transition workflow with optional validation.

        Args:
            session: Conversation session
            new_type: Target workflow type
            new_stage: Optional target stage
            validate: Whether to validate transition (default True)
            caller: Optional caller identification

        Returns:
            True if transition successful, False if blocked
        """
        current_type = WorkflowManager.get_workflow_type(session)
        caller_info = caller or inspect.stack()[1].function

        # Validate transition if requested
        if validate and not WorkflowManager.can_transition(session, new_type):
            logger.warning(f"[INVALID_TRANSITION] {session.session_id}: "
                         f"{current_type} → {new_type.value} blocked (caller: {caller_info})")
            # For now, log but allow (to maintain backward compatibility)
            # In strict mode, would return False here

        # Execute transition
        WorkflowManager.set_workflow_type(session, new_type, caller=caller_info)

        if new_stage:
            WorkflowManager.set_stage(session, new_stage, caller=caller_info)

        return True

    @staticmethod
    def can_transition(session: ConversationSession, new_type: WorkflowType) -> bool:
        """
        Check if workflow transition is valid.

        Args:
            session: Conversation session
            new_type: Target workflow type

        Returns:
            True if transition is valid
        """
        current_type = WorkflowManager.get_workflow_type(session)

        # Allow any transition if no current type
        if not current_type:
            return True

        # Check if transition is in valid set
        valid_targets = WorkflowManager.VALID_TRANSITIONS.get(current_type, set())
        return new_type in valid_targets

    # ===== WORKFLOW STAGE MANAGEMENT =====

    @staticmethod
    def set_stage(session: ConversationSession, stage: WorkflowStage,
                 caller: Optional[str] = None) -> None:
        """
        Set workflow stage.

        Args:
            session: Conversation session
            stage: New workflow stage
            caller: Optional caller identification
        """
        if not isinstance(stage, WorkflowStage):
            logger.error(f"[STAGE_ERROR] Attempted to set stage as non-enum: {stage}")
            return

        session.workflow_state = session.workflow_state or {}
        old_stage = session.workflow_state.get('stage')
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[STAGE_CHANGE] Session {session.session_id}: "
                   f"{old_stage} → {stage.value} (caller: {caller_info})")

        session.workflow_state['stage'] = stage.value

    @staticmethod
    def get_stage(session: ConversationSession) -> Optional[WorkflowStage]:
        """
        Get current workflow stage.

        Args:
            session: Conversation session

        Returns:
            WorkflowStage enum or None
        """
        if not session.workflow_state:
            return None

        stage_value = session.workflow_state.get('stage')
        if not stage_value:
            return None

        try:
            return WorkflowStage(stage_value)
        except (ValueError, KeyError):
            logger.warning(f"[STAGE_WARNING] Invalid stage value: {stage_value}")
            return None

    # ===== PENDING FLAG MANAGEMENT =====

    @staticmethod
    def set_pending(session: ConversationSession, flag: PendingFlag, value: Any = True,
                   caller: Optional[str] = None) -> None:
        """
        Set a pending flag with logging.

        Args:
            session: Conversation session
            flag: Pending flag to set
            value: Value to set (default True)
            caller: Optional caller identification
        """
        if not isinstance(flag, PendingFlag):
            logger.error(f"[PENDING_ERROR] Attempted to set pending flag as non-enum: {flag}")
            return

        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[PENDING_SET] Session {session.session_id}: "
                   f"{flag.value} = {type(value).__name__} (caller: {caller_info})")

        session.workflow_state[flag.value] = value

    @staticmethod
    def clear_pending(session: ConversationSession, *flags: PendingFlag,
                     caller: Optional[str] = None) -> None:
        """
        Clear one or more pending flags with logging.

        Args:
            session: Conversation session
            flags: Pending flags to clear
            caller: Optional caller identification
        """
        if not session.workflow_state:
            return

        caller_info = caller or inspect.stack()[1].function
        cleared = []

        for flag in flags:
            if not isinstance(flag, PendingFlag):
                logger.error(f"[PENDING_ERROR] Attempted to clear non-enum flag: {flag}")
                continue

            if flag.value in session.workflow_state:
                del session.workflow_state[flag.value]
                cleared.append(flag.value)

        if cleared:
            logger.info(f"[PENDING_CLEAR] Session {session.session_id}: "
                       f"Cleared {cleared} (caller: {caller_info})")

    @staticmethod
    def has_pending(session: ConversationSession, flag: PendingFlag) -> bool:
        """
        Check if a pending flag is set and truthy.

        Args:
            session: Conversation session
            flag: Pending flag to check

        Returns:
            True if flag is set and truthy
        """
        if not isinstance(flag, PendingFlag):
            logger.error(f"[PENDING_ERROR] Attempted to check non-enum flag: {flag}")
            return False

        if not session.workflow_state:
            return False

        return bool(session.workflow_state.get(flag.value))

    @staticmethod
    def get_pending_value(session: ConversationSession, flag: PendingFlag) -> Any:
        """
        Get value of a pending flag.

        Args:
            session: Conversation session
            flag: Pending flag to retrieve

        Returns:
            Value of the flag, or None if not set
        """
        if not isinstance(flag, PendingFlag):
            logger.error(f"[PENDING_ERROR] Attempted to get non-enum flag: {flag}")
            return None

        if not session.workflow_state:
            return None

        return session.workflow_state.get(flag.value)

    # ===== HELPER METHODS =====

    @staticmethod
    def has_any_pending_confirmation(session: ConversationSession) -> bool:
        """
        Check if any RFQ confirmation is pending.

        Args:
            session: Conversation session

        Returns:
            True if any RFQ confirmation flag is set
        """
        return any([
            WorkflowManager.has_pending(session, PendingFlag.RFQ),
            WorkflowManager.has_pending(session, PendingFlag.COMBINED_RFQ),
            WorkflowManager.has_pending(session, PendingFlag.OPTIONAL_RFQ),
            WorkflowManager.has_pending(session, PendingFlag.OPTIONAL_COMBINED_RFQ),
        ])

    @staticmethod
    def has_any_optional_confirmation(session: ConversationSession) -> bool:
        """
        Check if any optional RFQ confirmation is pending.

        Args:
            session: Conversation session

        Returns:
            True if any optional confirmation flag is set
        """
        return any([
            WorkflowManager.has_pending(session, PendingFlag.OPTIONAL_RFQ),
            WorkflowManager.has_pending(session, PendingFlag.OPTIONAL_COMBINED_RFQ),
        ])

    @staticmethod
    def clear_all_rfq_pending(session: ConversationSession, caller: Optional[str] = None) -> None:
        """
        Clear all RFQ-related pending flags.

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        WorkflowManager.clear_pending(
            session,
            PendingFlag.RFQ,
            PendingFlag.COMBINED_RFQ,
            PendingFlag.OPTIONAL_RFQ,
            PendingFlag.OPTIONAL_COMBINED_RFQ,
            caller=caller or inspect.stack()[1].function
        )

    @staticmethod
    def clear_all_decision_pending(session: ConversationSession, caller: Optional[str] = None) -> None:
        """
        Clear all user decision pending flags.

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        WorkflowManager.clear_pending(
            session,
            PendingFlag.ROLE_SWITCH,
            PendingFlag.ACCOUNT_SWITCH,
            PendingFlag.INTENT_SWITCH,
            PendingFlag.AUTH_REG_SWITCH,
            PendingFlag.ATTACHMENT_DECISION,
            caller=caller or inspect.stack()[1].function
        )

    @staticmethod
    def safe_reset_workflow_state(session: ConversationSession,
                                 preserve_fields: Optional[List[str]] = None,
                                 caller: Optional[str] = None) -> None:
        """
        Reset workflow state while preserving critical data.

        Args:
            session: Conversation session
            preserve_fields: Additional fields to preserve beyond defaults
            caller: Optional caller identification
        """
        caller_info = caller or inspect.stack()[1].function

        # Check if optional fields exist before reset
        had_optional_fields = bool(
            session.workflow_state and (
                'pending_optional_rfq' in session.workflow_state or
                'pending_optional_combined_rfq' in session.workflow_state
            )
        )

        # Determine fields to preserve
        fields_to_preserve = set(WorkflowManager.PROTECTED_FIELDS)
        if preserve_fields:
            fields_to_preserve.update(preserve_fields)

        # Save preserved data
        preserved_data = {}
        if session.workflow_state:
            for field in fields_to_preserve:
                if field in session.workflow_state:
                    preserved_data[field] = session.workflow_state[field]

        logger.warning(f"[WORKFLOW_RESET] Session {session.session_id}: "
                      f"Resetting workflow_state (preserving: {list(preserved_data.keys())}) "
                      f"(caller: {caller_info})")

        if had_optional_fields:
            logger.warning(f"[WORKFLOW_RESET_ALERT] Session {session.session_id}: "
                          f"CLEARED pending_optional fields during reset! Caller: {caller_info}")

        # Reset state with preserved data
        session.workflow_state = {
            'stage': WorkflowStage.COLLECTING.value,
            'last_activity_at': utc_now().isoformat(),
            **preserved_data
        }

    @staticmethod
    def initialize_workflow_state(session: ConversationSession) -> None:
        """
        Initialize workflow_state if None or empty.

        Args:
            session: Conversation session
        """
        if not session.workflow_state:
            session.workflow_state = {
                'extracted_entities': [],
                'stage': WorkflowStage.COLLECTING.value,
                'last_activity_at': utc_now().isoformat()
            }
            logger.info(f"[WORKFLOW_INIT] Session {session.session_id}: Initialized workflow_state")
        else:
            # DEFENSIVE CLEANUP: Remove any lingering session_archive
            if 'session_archive' in session.workflow_state:
                logger.warning(f"[WORKFLOW_INIT] Session {session.session_id}: Removing lingering session_archive")
                del session.workflow_state['session_archive']

    # ===== TRACK 2 RFQ FLOW METHODS =====

    @staticmethod
    def set_delivery_details(session: ConversationSession, delivery_data: Dict[str, Any],
                            caller: Optional[str] = None) -> None:
        """
        Set delivery details in workflow state (Track 2).

        Args:
            session: Conversation session
            delivery_data: Dict with delivery_date, pincode, city, state
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[DELIVERY_SET] Session {session.session_id}: "
                   f"Setting delivery details (caller: {caller_info})")

        session.workflow_state['delivery_details'] = delivery_data
        session.workflow_state['delivery_confirmed'] = False

    @staticmethod
    def get_delivery_details(session: ConversationSession) -> Optional[Dict[str, Any]]:
        """
        Get delivery details from workflow state (Track 2).

        Args:
            session: Conversation session

        Returns:
            Delivery details dict or None
        """
        if not session.workflow_state:
            return None
        return session.workflow_state.get('delivery_details')

    @staticmethod
    def confirm_delivery(session: ConversationSession, caller: Optional[str] = None) -> None:
        """
        Mark delivery details as confirmed (Track 2).

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[DELIVERY_CONFIRM] Session {session.session_id}: "
                   f"Delivery confirmed (caller: {caller_info})")

        session.workflow_state['delivery_confirmed'] = True
        session.workflow_state['awaiting_delivery_modification'] = False

    @staticmethod
    def is_delivery_confirmed(session: ConversationSession) -> bool:
        """
        Check if delivery details are confirmed (Track 2).

        Args:
            session: Conversation session

        Returns:
            True if delivery confirmed
        """
        if not session.workflow_state:
            return False
        return session.workflow_state.get('delivery_confirmed', False)

    @staticmethod
    def mark_initial_extraction_complete(session: ConversationSession,
                                        caller: Optional[str] = None) -> None:
        """
        Mark that initial items extraction has been completed (Track 2).
        This prevents re-extraction on subsequent modifications.

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[EXTRACTION_FLAG] Session {session.session_id}: "
                   f"Initial extraction complete (caller: {caller_info})")

        session.workflow_state['initial_items_extracted'] = True

    @staticmethod
    def is_initial_extraction_complete(session: ConversationSession) -> bool:
        """
        Check if initial items extraction has been completed (Track 2).

        Args:
            session: Conversation session

        Returns:
            True if initial extraction is done
        """
        if not session.workflow_state:
            return False
        return session.workflow_state.get('initial_items_extracted', False)

    @staticmethod
    def set_awaiting_modification(session: ConversationSession,
                                  subtype: str,
                                  original_format: str,
                                  caller: Optional[str] = None) -> None:
        """
        Set awaiting modification state with original format (Track 2).

        Args:
            session: Conversation session
            subtype: "delivery" or "items"
            original_format: Formatted text to show user
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[AWAITING_MOD] Session {session.session_id}: "
                   f"Awaiting {subtype} modification (caller: {caller_info})")

        if subtype == "delivery":
            session.workflow_state['awaiting_delivery_modification'] = True
        elif subtype == "items":
            session.workflow_state['awaiting_items_modification'] = True
        else:
            logger.error(f"[AWAITING_MOD_ERROR] Invalid subtype: {subtype}")
            return

        session.workflow_state['format_modification_subtype'] = subtype
        session.workflow_state['original_format'] = original_format
        session.workflow_state['format_retry_count'] = 0

    @staticmethod
    def clear_awaiting_modification(session: ConversationSession,
                                    caller: Optional[str] = None) -> None:
        """
        Clear awaiting modification state (Track 2).

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        subtype = session.workflow_state.get('format_modification_subtype')
        logger.info(f"[CLEAR_AWAITING_MOD] Session {session.session_id}: "
                   f"Clearing {subtype} modification (caller: {caller_info})")

        session.workflow_state['awaiting_delivery_modification'] = False
        session.workflow_state['awaiting_items_modification'] = False
        session.workflow_state['format_modification_subtype'] = None
        session.workflow_state['original_format'] = None
        session.workflow_state['format_retry_count'] = 0

    @staticmethod
    def is_awaiting_modification(session: ConversationSession) -> tuple[bool, Optional[str]]:
        """
        Check if awaiting modification and return subtype (Track 2).

        Args:
            session: Conversation session

        Returns:
            Tuple of (is_awaiting, subtype)
            Example: (True, "items") or (False, None)
        """
        if not session.workflow_state:
            return (False, None)

        if session.workflow_state.get('awaiting_delivery_modification'):
            return (True, "delivery")
        elif session.workflow_state.get('awaiting_items_modification'):
            return (True, "items")
        else:
            return (False, None)

    @staticmethod
    def increment_retry_count(session: ConversationSession,
                             caller: Optional[str] = None) -> int:
        """
        Increment format retry count and return new count (Track 2).

        Args:
            session: Conversation session
            caller: Optional caller identification

        Returns:
            New retry count
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        current_count = session.workflow_state.get('format_retry_count', 0)
        new_count = current_count + 1
        session.workflow_state['format_retry_count'] = new_count

        logger.warning(f"[RETRY_INCREMENT] Session {session.session_id}: "
                      f"Retry count: {new_count} (caller: {caller_info})")

        return new_count

    @staticmethod
    def reset_retry_count(session: ConversationSession,
                         caller: Optional[str] = None) -> None:
        """
        Reset format retry count to 0 (Track 2).

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[RETRY_RESET] Session {session.session_id}: "
                   f"Retry count reset (caller: {caller_info})")

        session.workflow_state['format_retry_count'] = 0

    @staticmethod
    def get_retry_count(session: ConversationSession) -> int:
        """
        Get current retry count (Track 2).

        Args:
            session: Conversation session

        Returns:
            Current retry count
        """
        if not session.workflow_state:
            return 0
        return session.workflow_state.get('format_retry_count', 0)

    @staticmethod
    def can_proceed_to_items(session: ConversationSession) -> bool:
        """
        Check if can proceed to items collection (Track 2).
        Requires delivery to be confirmed first.

        Args:
            session: Conversation session

        Returns:
            True if delivery is confirmed
        """
        return WorkflowManager.is_delivery_confirmed(session)

    @staticmethod
    def get_track2_context(session: ConversationSession) -> Dict[str, Any]:
        """
        Get relevant context for current Track 2 workflow step.

        Args:
            session: Conversation session

        Returns:
            Dict with current context information
        """
        if not session.workflow_state:
            return {}

        is_awaiting, subtype = WorkflowManager.is_awaiting_modification(session)

        return {
            "delivery_confirmed": WorkflowManager.is_delivery_confirmed(session),
            "delivery_details": WorkflowManager.get_delivery_details(session),
            "initial_extraction_complete": WorkflowManager.is_initial_extraction_complete(session),
            "is_awaiting_modification": is_awaiting,
            "modification_subtype": subtype,
            "retry_count": WorkflowManager.get_retry_count(session),
            "can_proceed_to_items": WorkflowManager.can_proceed_to_items(session)
        }

    @staticmethod
    def save_interruption_context(session: ConversationSession,
                                   interrupted_by: str,
                                   caller: Optional[str] = None) -> None:
        """
        Save workflow context before interruption (Track 2 - Module 2.3).

        Args:
            session: Conversation session
            interrupted_by: Type of interruption ("faq", "greeting", "help")
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[INTERRUPTION_SAVE] Session {session.session_id}: "
                   f"Saving context before {interrupted_by} interruption (caller: {caller_info})")

        # Save current workflow position
        is_awaiting, subtype = WorkflowManager.is_awaiting_modification(session)

        interruption_context = {
            "interrupted_by": interrupted_by,
            "delivery_confirmed": WorkflowManager.is_delivery_confirmed(session),
            "awaiting_modification": is_awaiting,
            "modification_subtype": subtype,
            "has_delivery_details": WorkflowManager.get_delivery_details(session) is not None,
            "has_extracted_entities": bool(session.workflow_state.get("extracted_entities")),
            "retry_count": WorkflowManager.get_retry_count(session)
        }

        session.workflow_state["interrupted_by"] = interrupted_by
        session.workflow_state["resume_context"] = interruption_context
        session.workflow_state["can_resume"] = True

    @staticmethod
    def can_resume_workflow(session: ConversationSession) -> bool:
        """
        Check if workflow can be resumed after interruption (Track 2 - Module 2.3).

        Args:
            session: Conversation session

        Returns:
            True if can resume workflow
        """
        if not session.workflow_state:
            return False

        return session.workflow_state.get("can_resume", False)

    @staticmethod
    def get_resume_prompt(session: ConversationSession) -> Optional[str]:
        """
        Generate contextual resume prompt after interruption (Track 2 - Module 2.3).

        Args:
            session: Conversation session

        Returns:
            Resume prompt message or None
        """
        if not WorkflowManager.can_resume_workflow(session):
            return None

        resume_context = session.workflow_state.get("resume_context", {})
        interrupted_by = resume_context.get("interrupted_by", "interruption")

        # Build contextual message based on workflow state
        if resume_context.get("awaiting_modification"):
            subtype = resume_context.get("modification_subtype", "items")
            return (f"Let's continue with your RFQ. "
                   f"You were modifying your {subtype}. "
                   f"Please send the updated format when ready.")

        elif resume_context.get("has_extracted_entities"):
            return ("Let's continue with your RFQ. "
                   "You were reviewing your items. "
                   "Would you like to confirm or modify them?")

        elif resume_context.get("has_delivery_details") and not resume_context.get("delivery_confirmed"):
            return ("Let's continue with your RFQ. "
                   "You were confirming your delivery details. "
                   "Please confirm or modify them.")

        elif resume_context.get("delivery_confirmed"):
            return ("Let's continue with your RFQ. "
                   "Please share the items you need with name, brand/specs (if any), and quantity.")

        else:
            return "Let's continue with your RFQ. What would you like to do next?"

    @staticmethod
    def resume_workflow(session: ConversationSession,
                       caller: Optional[str] = None) -> Dict[str, Any]:
        """
        Resume workflow after interruption (Track 2 - Module 2.3).

        Args:
            session: Conversation session
            caller: Optional caller identification

        Returns:
            Dict with resume context information
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[WORKFLOW_RESUME] Session {session.session_id}: "
                   f"Resuming workflow (caller: {caller_info})")

        resume_context = session.workflow_state.get("resume_context", {})

        # Clear interruption flags
        session.workflow_state["interrupted_by"] = None
        session.workflow_state["can_resume"] = False

        return resume_context

    # ========================================================================
    # SECTIONED RFQ WORKFLOW METHODS (Track 3)
    # ========================================================================

    @staticmethod
    def initialize_sectioned_rfq(session: ConversationSession,
                                 caller: Optional[str] = None) -> None:
        """
        Initialize sectioned RFQ workflow state.

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        from app.schemas.sectioned_rfq_state import initialize_sectioned_rfq_state

        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_INIT] Session {session.session_id}: "
                   f"Initializing sectioned RFQ (caller: {caller_info})")

        session.workflow_state["sectioned_rfq"] = initialize_sectioned_rfq_state()

    @staticmethod
    def is_sectioned_rfq_active(session: ConversationSession) -> bool:
        """
        Check if sectioned RFQ workflow is active.

        Args:
            session: Conversation session

        Returns:
            True if sectioned RFQ is active
        """
        if not session.workflow_state:
            return False
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        return sectioned_rfq.get("active", False)

    @staticmethod
    def set_sectioned_rfq_section(session: ConversationSession,
                                  section: str,
                                  caller: Optional[str] = None) -> None:
        """
        Set current section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name (date_location, items, attachments, final_confirmation)
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_SECTION] Session {session.session_id}: "
                   f"Setting section to '{section}' (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        session.workflow_state["sectioned_rfq"]["current_section"] = section

    @staticmethod
    def get_sectioned_rfq_section(session: ConversationSession) -> Optional[str]:
        """
        Get current section in sectioned RFQ workflow.

        Args:
            session: Conversation session

        Returns:
            Current section name or None
        """
        if not session.workflow_state:
            return None
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        return sectioned_rfq.get("current_section")

    @staticmethod
    def confirm_sectioned_section(session: ConversationSession,
                                  section: str,
                                  caller: Optional[str] = None) -> None:
        """
        Mark section as confirmed in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name to confirm
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_CONFIRM] Session {session.session_id}: "
                   f"Confirming section '{section}' (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        sections = session.workflow_state["sectioned_rfq"].get("sections", {})
        if section in sections:
            sections[section]["confirmed"] = True

    @staticmethod
    def is_section_confirmed(session: ConversationSession, section: str) -> bool:
        """
        Check if section is confirmed in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name to check

        Returns:
            True if section is confirmed
        """
        if not session.workflow_state:
            return False
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        sections = sectioned_rfq.get("sections", {})
        if section in sections:
            return sections[section].get("confirmed", False)
        return False

    @staticmethod
    def get_section_data(session: ConversationSession, section: str) -> Optional[Any]:
        """
        Get data for specific section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name

        Returns:
            Section data or None
        """
        if not session.workflow_state:
            return None
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        sections = sectioned_rfq.get("sections", {})
        if section in sections:
            return sections[section].get("data")
        return None

    @staticmethod
    def update_section_data(session: ConversationSession,
                           section: str,
                           data: Any,
                           caller: Optional[str] = None) -> None:
        """
        Update data for specific section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name
            data: Data to store
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_DATA] Session {session.session_id}: "
                   f"Updating data for section '{section}' (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        sections = session.workflow_state["sectioned_rfq"].get("sections", {})
        if section in sections:
            sections[section]["data"] = data

    @staticmethod
    def increment_section_retry(session: ConversationSession,
                                section: str,
                                caller: Optional[str] = None) -> int:
        """
        Increment retry counter for section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name
            caller: Optional caller identification

        Returns:
            New retry count
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        sections = session.workflow_state["sectioned_rfq"].get("sections", {})
        if section in sections:
            current_retry = sections[section].get("retry_count", 0)
            new_retry = current_retry + 1
            sections[section]["retry_count"] = new_retry

            logger.info(f"[SECTIONED_RFQ_RETRY] Session {session.session_id}: "
                       f"Section '{section}' retry count: {new_retry} (caller: {caller_info})")

            return new_retry
        return 0

    @staticmethod
    def reset_section_retry(session: ConversationSession,
                           section: str,
                           caller: Optional[str] = None) -> None:
        """
        Reset retry counter for section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_RETRY_RESET] Session {session.session_id}: "
                   f"Resetting retry count for section '{section}' (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            return

        sections = session.workflow_state["sectioned_rfq"].get("sections", {})
        if section in sections:
            sections[section]["retry_count"] = 0

    @staticmethod
    def set_awaiting_section_modification(session: ConversationSession,
                                          section: str,
                                          value: bool,
                                          caller: Optional[str] = None) -> None:
        """
        Set awaiting modification flag for section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name
            value: True if awaiting modification
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_AWAITING_MOD] Session {session.session_id}: "
                   f"Section '{section}' awaiting modification: {value} (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        sections = session.workflow_state["sectioned_rfq"].get("sections", {})
        if section in sections:
            sections[section]["awaiting_modification"] = value

    @staticmethod
    def is_awaiting_section_modification(session: ConversationSession,
                                         section: str) -> bool:
        """
        Check if awaiting modification for section in sectioned RFQ workflow.

        Args:
            session: Conversation session
            section: Section name

        Returns:
            True if awaiting modification
        """
        if not session.workflow_state:
            return False
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        sections = sectioned_rfq.get("sections", {})
        if section in sections:
            return sections[section].get("awaiting_modification", False)
        return False

    @staticmethod
    def set_sectioned_rfq_pending_restart(session: ConversationSession,
                                          value: bool,
                                          caller: Optional[str] = None) -> None:
        """
        Set pending restart flag for sectioned RFQ workflow.

        Args:
            session: Conversation session
            value: True if restart confirmation pending
            caller: Optional caller identification
        """
        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_RESTART] Session {session.session_id}: "
                   f"Pending restart: {value} (caller: {caller_info})")

        if "sectioned_rfq" not in session.workflow_state:
            WorkflowManager.initialize_sectioned_rfq(session)

        session.workflow_state["sectioned_rfq"]["pending_restart"] = value

    @staticmethod
    def is_sectioned_rfq_pending_restart(session: ConversationSession) -> bool:
        """
        Check if restart confirmation is pending for sectioned RFQ workflow.

        Args:
            session: Conversation session

        Returns:
            True if pending restart
        """
        if not session.workflow_state:
            return False
        sectioned_rfq = session.workflow_state.get("sectioned_rfq", {})
        return sectioned_rfq.get("pending_restart", False)

    @staticmethod
    def reset_sectioned_rfq(session: ConversationSession,
                           caller: Optional[str] = None) -> None:
        """
        Reset sectioned RFQ workflow to initial state.

        Args:
            session: Conversation session
            caller: Optional caller identification
        """
        from app.schemas.sectioned_rfq_state import initialize_sectioned_rfq_state

        session.workflow_state = session.workflow_state or {}
        caller_info = caller or inspect.stack()[1].function

        logger.info(f"[SECTIONED_RFQ_RESET] Session {session.session_id}: "
                   f"Resetting sectioned RFQ (caller: {caller_info})")

        session.workflow_state["sectioned_rfq"] = initialize_sectioned_rfq_state()
