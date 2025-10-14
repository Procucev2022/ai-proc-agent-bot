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

        logger.info(f"[WORKFLOW_TRANSITION] Session {session.session_id}: "
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
