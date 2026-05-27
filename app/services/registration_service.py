"""
Registration Service for WhatsApp Bot.

Handles complete user registration flow including:
- Entity-based data collection for buyers and sellers
- Registration API integration
- Email OTP verification
- Domain approval for buyers
"""

import logging
from typing import Dict, Any, List, Optional
from app.schemas.user import User
from app.models import ConversationSession, UserType, WorkflowType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.entity_service import EntityService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.confirmation_service import ConfirmationService
from app.services.workflow_manager import WorkflowManager
from app.procucev_apis.register_apis import RegisterAPIService
from app.schemas.user import User, BuyerRegistrationSchema, SellerRegistrationSchema, normalize_phone_number
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.services.support_notification_service import SupportNotificationService
from app.services.domain_check_service import DomainCheckService
from app.services.otp_service import OTPService
from app.utils.pincode_lookup import get_location_from_pincode_async
from datetime import datetime
from app.config import get_settings

logger = logging.getLogger(__name__)


class RegistrationService:
    """Handles user registration workflow with entity-based data collection."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None,
                 openai_service: OpenAIService = None,
                 entity_service: EntityService = None,
                 response_helpers: ResponseHelpers = None,
                 confirmation_service: ConfirmationService = None,
                 session_manager=None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.entity_service = entity_service or EntityService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
        self.confirmation_service = confirmation_service
        self.register_api_service = RegisterAPIService()
        self.auth_redis_service = get_auth_redis_service()
        self.support_notification_service = SupportNotificationService()
        self.session_manager = session_manager  # Set this first
        self.domain_check_service = DomainCheckService(self.openai_service, self.whatsapp_service, self.session_manager)
        self.authentication_helpers = AuthenticationHelpers()
        self.settings = get_settings()
        self.otp_service = OTPService(self.register_api_service, self.whatsapp_service, self.support_notification_service)
        # Import here to avoid circular imports
        from app.procucev_apis.auth_apis import AuthAPIService
        self.auth_api_service = AuthAPIService()
    
    async def initiate_registration(self, user_phone: str, session: ConversationSession,
                                  user_type: str, message: str = "") -> Dict[str, Any]:
        """
        Initiate registration flow for new users.
        
        Args:
            user_phone: User's phone number
            session: Current conversation session
            user_type: "buyer" or "seller"
            message: Optional initial message from user
        """
        try:
            session.user_type = UserType.buyer if user_type == "buyer" else UserType.seller

            if user_type == "buyer":
                intro_message = self.authentication_helpers.generate_registration_message(BuyerRegistrationSchema, "Buyer", False)
            else:  # seller
                intro_message = self.authentication_helpers.generate_registration_message(SellerRegistrationSchema, "Seller", False)

            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, intro_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, intro_message, session_id=session)

            result = {
                "status": "registration_initiated",
                "user_type": user_type,
                "stage": "data_collection",
                "message": "Registration flow started"
            }

            logger.info(f"Registration initiated for {user_phone} as {user_type}")
            return result
            
        except Exception as e:
            logger.error(f"Registration initiation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_initiation_error", str(e), session)
    
    async def handle_registration_data_collection(self, user_phone: str, message_content: str,
                                                session: ConversationSession) -> Dict[str, Any]:
        """Handle registration data collection using entity extraction with context awareness."""
        try:
            user_type = session.workflow_state.get("user_type", "buyer")

            # Check for exit commands first
            if await self._check_exit_command(message_content):
                return await self._handle_registration_exit(user_phone, session)

            # Initialize registration_entities if not exists
            if "registration_entities" not in session.workflow_state:
                session.workflow_state["registration_entities"] = {}

            # Get existing entities from session
            existing_entities = session.workflow_state.get("registration_entities", {})

            # Build context from conversation history for better entity extraction
            conversation_context = self._build_registration_context(session, message_content)

            # Extract entities from user message with full context including existing entities
            workflow_type = f"registration_{user_type}"
            entity_result = await self.entity_service.extract_entities(
                message_content,
                context={
                    "workflow_type": workflow_type,
                    "conversation_history": conversation_context,
                    "registration_stage": "data_collection",
                    "existing_entities": existing_entities
                },
                workflow_type=workflow_type
            )

            # Merge extracted entities with existing ones
            if entity_result.get("entities"):
                # Smart merge - prioritize new data but preserve existing
                for key, value in entity_result["entities"].items():
                    if value and str(value).strip():  # Only update if new value is meaningful
                        # Map products_services to details for seller registration
                        if key == "products_services" and user_type == "seller":
                            existing_entities["details"] = str(value).strip()
                        else:
                            existing_entities[key] = str(value).strip()
                
                # Auto-fill address from pincode if provided
                await self._auto_fill_address_from_pincode(existing_entities, user_phone, session)
                
                session.workflow_state["registration_entities"] = existing_entities
                session.workflow_state["last_activity_at"] = utc_now().isoformat()
            else:
                logger.warning("No entities extracted from message")

            # Validate entities (email and pincode) using authentication helper
            if user_type == 'buyer':
                entity_schema = BuyerRegistrationSchema
            else:  # seller
                entity_schema = SellerRegistrationSchema

            # Check for pincode error from auto-fill
            pincode_error = existing_entities.pop("_pincode_error", None)
            
            existing_entities, validation_error_message = await self.authentication_helpers.validate_entities(existing_entities, entity_schema)
            
            # Append pincode error only if not already in validation errors
            if pincode_error and (not validation_error_message or "pincode" not in validation_error_message.lower()):
                pincode_num = pincode_error.replace("Pincode ", "").split(" does not exist")[0]
                formatted_pincode_error = f"• Pincode: {pincode_num} does not exist"
                
                if validation_error_message:
                    validation_error_message = validation_error_message + "\n" + formatted_pincode_error
                else:
                    validation_error_message = formatted_pincode_error
            
            # Remove invalid fields from collected entities
            if validation_error_message:
                # Remove ALL fields that failed validation
                if "organization email" in validation_error_message.lower() or "email" in validation_error_message.lower():
                    existing_entities.pop("email", None)
                if "gstin number" in validation_error_message.lower() or "gstin" in validation_error_message.lower():
                    existing_entities.pop("gstin", None)
                if "pincode" in validation_error_message.lower() or "zipcode" in validation_error_message.lower():
                    existing_entities.pop("zipCode", None)
            
            session.workflow_state["registration_entities"] = existing_entities

            # Dynamic schema-based field validation
            user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
            missing_fields = AuthenticationHelpers.get_missing_fields(user_schema, existing_entities)

            if missing_fields:
                # Generate questions for missing fields with validation errors merged
                questions = await self._generate_contextual_registration_questions(
                    missing_fields, user_type, existing_entities, message_content, validation_error_message
                )
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, questions, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, questions, session_id=session)

                # Update session
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                session.workflow_state["registration_stage"] = "data_collection"
                session.workflow_state["last_activity_at"] = utc_now().isoformat()

                return {
                    "status": "data_collection_in_progress",
                    "missing_fields": missing_fields,
                    "collected_entities": existing_entities
                }
            else:
                # All data collected, show confirmation with buttons
                await self._send_confirmation_with_buttons(user_phone, existing_entities, user_type, session)

                # Mark as awaiting confirmation
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                session.workflow_state["registration_entities"] = existing_entities
                session.workflow_state["registration_stage"] = "confirmation"
                session.workflow_state["last_activity_at"] = utc_now().isoformat()
                logger.info(f"Registration data collected for {user_phone}, awaiting confirmation")

                return {
                    "status": "awaiting_confirmation",
                    "collected_entities": existing_entities
                }
                
        except Exception as e:
            logger.error(f"Registration data collection error: {e}", exc_info=True)
            return await self._redirect_to_support(user_phone, "registration_data_error", str(e), session)

    def _build_registration_context(self, session: ConversationSession, current_message: str,
                                    history_limit: int = 20 ) -> str:
        """Build context from conversation history for better entity extraction."""
        try:
            messages = session.conversation_history.get("messages", [])
            context_messages = []

            # Include last few messages from both user and assistant
            for msg in messages[-history_limit:]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    content = content.strip()
                    if content:
                        context_messages.append(content)

            # Add current message if not empty
            if isinstance(current_message, str):
                current_message = current_message.strip()
                if current_message:
                    context_messages.append(current_message)
            elif isinstance(current_message, dict):
                # Handle button replies or interactive messages
                button_id = current_message.get("button_reply", {}).get("id", "")
                if button_id:
                    context_messages.append(button_id)

            return " ".join(context_messages)
        except Exception as e:
            logger.error(f"Error building registration context: {e}")
            return str(current_message) if isinstance(current_message, str) else ""

    async def _generate_confirmation_message(self, entities: Dict, user_type: str) -> str:
        """Generate confirmation message showing all collected details dynamically."""
        user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
        return AuthenticationHelpers.generate_confirmation_message_dynamic(user_schema, entities)
    
    async def _send_confirmation_with_buttons(self, user_phone: str, entities: Dict, user_type: str, session: ConversationSession) -> None:
        """Send confirmation message with interactive buttons."""
        message = await self._generate_confirmation_message(entities, user_type)
        
        buttons = [
            {"id": "confirm_registration", "title": "Confirm"},
            {"id": "restart_registration", "title": "Restart"}
        ]
        
        # Try to send with buttons first
        button_response = await self.whatsapp_service.send_configurable_buttons(
            user_phone, message, buttons,session_id=session
        )
        
        if not button_response.success:
            # Fallback to text message if buttons fail
            fallback_message = message + "\n\nReply 'YES' to confirm or 'NO' to restart registration."
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, fallback_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, fallback_message, session_id=session)
    
    async def handle_registration_confirmation(self, user_phone: str, message_content: str,
                                             session: ConversationSession) -> Dict[str, Any]:
        """Handle user confirmation response from buttons or keywords."""
        try:
            # Check for exit commands first
            if await self._check_exit_command(message_content):
                logger.info(f"Exit keyword detected during registration confirmation: '{message_content}'")
                return await self._handle_registration_exit(user_phone, session)
            
            user_type = session.workflow_state.get("user_type", "buyer")
            entities = session.workflow_state.get("registration_entities", {})
            
            # Check for button responses first
            button_response = self._parse_button_response(message_content)
            if button_response:
                confirmation = button_response
            else:
                # Fallback to keyword/AI parsing
                confirmation = await self.confirmation_service.parse_confirmation(message_content) if self.confirmation_service else None

            if confirmation == "yes":
                # User confirmed, proceed with registration submission
                result = await self._submit_registration(user_phone, session, entities, user_type)
                
                if result.get("status") == "registration_completed":
                    # Registration successful, now start OTP verification
                    email = entities.get("email")
                    session.workflow_state["registration_stage"] = "email_otp"
                    session.workflow_state["pending_registration_data"] = entities
                    session.workflow_state["otp_email"] = email
                    session.workflow_state["otp_retry_count"] = 0
                    
                    WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                    
                    # Send OTP for email verification
                    return await self.otp_service.send_otp(user_phone, email, session)
                else:
                    return result
                    
            elif confirmation == "no":
                # User wants to restart
                session.workflow_state = {
                    "registration_stage": "data_collection",
                    "user_type": user_type,
                    "registration_entities": {},
                    "last_activity_at": utc_now().isoformat()
                }
                
                if user_type == "buyer":
                    restart_message = self.authentication_helpers.generate_registration_message(BuyerRegistrationSchema, "Buyer", False)
                else:
                    restart_message = self.authentication_helpers.generate_registration_message(SellerRegistrationSchema, "Seller", False)
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, restart_message, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, restart_message, session_id=session)
                
                return {
                    "status": "registration_restarted",
                    "user_type": user_type
                }
            else:
                # Unclear response, ask again with buttons
                await self._send_clarification_with_buttons(user_phone, session)
                
                return {
                    "status": "awaiting_confirmation",
                    "message": "clarification_requested"
                }
                
        except Exception as e:
            logger.error(f"Registration confirmation error: {e}")
            return await self._redirect_to_support(user_phone, "confirmation_error", str(e), session)
    
    def _parse_button_response(self, message_content) -> Optional[str]:
        """Parse button response from WhatsApp interactive message."""
        # Handle both string and dictionary inputs
        if isinstance(message_content, dict):
            # Extract button ID from interactive message structure
            button_reply = message_content.get("button_reply", {})
            button_id = button_reply.get("id", "")
            if button_id and isinstance(button_id, str):
                message_lower = button_id.lower().strip()
            else:
                return None
        elif isinstance(message_content, str):
            message_lower = message_content.lower().strip()
        else:
            return None
        
        # Check for button IDs or titles
        if message_lower in ["confirm_registration", "confirm"]:
            return "yes"
        elif message_lower in ["restart_registration", "restart"]:
            return "no"
        
        return None
    
    async def _send_clarification_with_buttons(self, user_phone: str, session: ConversationSession) -> None:
        """Send clarification message with buttons."""
        message = "Please confirm your registration details."
        
        buttons = [
            {"id": "confirm_registration", "title": "Confirm"},
            {"id": "restart_registration", "title": "Restart"}
        ]
        
        # Try to send with buttons first
        button_response = await self.whatsapp_service.send_configurable_buttons(
            user_phone, message, buttons,session_id=session
        )
        
        if not button_response.success:
            # Fallback to text message if buttons fail
            fallback_message = "Please reply 'yes' to confirm your details or 'no' to restart registration."
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, fallback_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, fallback_message, session_id=session)
    
    async def _generate_contextual_registration_questions(self, missing_fields: List[str], 
                                                        user_type: str, existing_entities: Dict,
                                                        current_message: str, validation_error_message: Optional[str] = None) -> str:
        """Generate contextual questions dynamically based on schema with validation errors."""
        user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
        return AuthenticationHelpers.generate_registration_questions_dynamic(
            user_schema, existing_entities, missing_fields, validation_error_message
        )
    
    async def _submit_registration(self, user_phone: str, session: ConversationSession,
                                 entities: Dict, user_type: str) -> Dict[str, Any]:
        """Submit registration to API using dynamic schema-based payload generation."""
        try:
            # Get schema dynamically based on user type
            user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
            
            # Handle field mapping for seller registration
            mapped_entities = entities.copy()
            if user_type == "seller" and "products_services" in mapped_entities:
                mapped_entities["details"] = mapped_entities.pop("products_services")
            
            # Build payload dynamically from schema
            registration_data = AuthenticationHelpers.build_registration_payload_dynamic(
                user_schema, mapped_entities, user_phone
            )

            # For sellers: Parse and categorize their products/services BEFORE registration
            if user_type == "seller" and "details" in registration_data:
                division_categories = await self._categorize_seller_products(
                    seller_details=registration_data["details"],
                    user_phone=user_phone,
                    session=session
                )

                # Add divisionCategories to registration payload
                if division_categories:
                    registration_data["divisionCategories"] = division_categories
                    logger.info(f"Added {len(division_categories)} divisionCategories to seller registration")

                    # Log the categories for visibility
                    import json
                    logger.info(f"Seller registration divisionCategories: {json.dumps(division_categories, indent=2)}")

            # Log the complete registration payload (excluding sensitive fields)
            import json
            payload_for_logging = registration_data.copy()
            # Mask sensitive fields for logging
            if "gstin" in payload_for_logging:
                payload_for_logging["gstin"] = "***MASKED***"
            logger.info(f"Seller registration payload: {json.dumps(payload_for_logging, indent=2)}")

            # Call appropriate API based on user type
            if user_type == "buyer":
                result = await self.register_api_service.register_buyer(registration_data)
            else:
                result = await self.register_api_service.register_seller(registration_data)
            
            if result.get("statusCode") in ["1001", "200"] or result.get("status") == "Success":
                # Registration successful - extract user_id and org_id from response and store them
                logger.info(f"{user_type.title()} registration completed for {user_phone}")

                # Extract user_id and org_id from API response if available
                user_id = None
                org_id = None
                if result.get("data") and isinstance(result["data"], dict):
                    user_id = result["data"].get("userId") or result["data"].get("id")
                    org_id = result["data"].get("orgId")
                elif result.get("type") and isinstance(result["type"], dict):
                    user_id = result["type"].get("userId") or result["type"].get("id")
                    org_id = result["type"].get("orgId")

                # Store user_id and org_id in entities for later use
                if user_id:
                    entities["user_id"] = user_id
                else:
                    logger.warning(f"No user_id found in registration response: {result}")

                if org_id:
                    entities["org_id"] = org_id
                else:
                    logger.warning(f"No org_id found in registration response: {result}")
                
                # Update session with both user_id and org_id
                session.workflow_state["registration_entities"] = entities
                
                return {
                    "status": "registration_completed",
                    "user_type": user_type,
                    "continue_to_otp": True,
                    "user_id": user_id
                }
            else:
                # Registration failed - check for specific error codes
                error_msg = result.get("message", "Registration failed")
                status_code = result.get("status_code", result.get("statusCode"))
                
                # Handle 409 Conflict - User already exists
                if status_code == 409 or "already exists" in error_msg.lower():
                    logger.info(f"User already exists: {error_msg}")

                    # Get user type before clearing workflow state
                    user_type = session.workflow_state.get("user_type", "buyer")

                    # Call cancel service to reset workflow state (keeps user authenticated)
                    from app.services.cancel_service import CancelService
                    cancel_service = CancelService(self.whatsapp_service, self.session_manager, None, None)
                    await cancel_service._clear_workflow_state(session)

                    # Restart registration flow with the same user type
                    session.workflow_state = {
                        "registration_stage": "data_collection",
                        "user_type": user_type,
                        "registration_entities": {},
                        "last_activity_at": utc_now().isoformat()
                    }

                    # Build field list for registration message
                    user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
                    fields = user_schema.model_fields
                    required_field_labels = []

                    for name, field_info in fields.items():
                        if name in {"sourceType", "address1"}:
                            continue
                        if field_info.is_required():
                            label = field_info.description or name.replace("_", " ").title()
                            required_field_labels.append(f"*{label}*")

                    field_list = ", ".join(required_field_labels)

                    # Combine error message with registration prompt into ONE message
                    combined_message = (
                        "*User Registration Failed – User Already Exists*\n\n"
                        "The details you provided are already registered in our system. Please try again using different information.\n\n"
                        f"To continue with the registration, please share the following details:\n{field_list}\n\n"
                        "Please ensure your email address is correct, as you will receive an OTP there for verification."
                    )

                    if self.session_manager:
                        await self.session_manager.send_and_track_message(user_phone, combined_message, session)
                        await self.session_manager.save_session(session, WorkflowType.registration)
                    else:
                        await self.whatsapp_service.send_message(user_phone, combined_message, session_id=session)

                    return {
                        "status": "user_already_exists",
                        "message": "User registration rejected - user already exists",
                        "registration_restarted": True
                    }
                else:
                    # Other registration failures
                    logger.info(f"Registration failed: {error_msg}")
                    
                    # Send email notification to support team for registration failure
                    await self.support_notification_service.notify_registration_failed(
                        entities.get("name") or entities.get("full_name", "User"),
                        entities.get("email", "unknown"),
                        user_phone,
                        user_type
                    )
                    
                    return await self._redirect_to_support(user_phone, "registration_failed", error_msg, session)
                    
        except Exception as e:
            logger.error(f"Registration submission error: {e}")
            return await self._redirect_to_support(user_phone, "registration_submission_error", str(e), session)
    


    async def handle_registration_otp_validation(self, user_phone: str, message_content: str,
                                               session: ConversationSession) -> Dict[str, Any]:
        """Handle OTP validation for registration."""
        try:
            # Check for exit commands first
            if await self._check_exit_command(message_content):
                return await self._handle_registration_exit(user_phone, session)

            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")

            # Use OTP service for validation
            otp_result = await self.otp_service.handle_user_message(user_phone, message_content, session)

            # If OTP is valid, complete registration
            if otp_result.get("status") == "otp_valid":
                entities = session.workflow_state.get("pending_registration_data", {})
                user_type = session.workflow_state.get("user_type", "buyer")

                # Fetch complete user data from API after successful registration and OTP validation
                auth_response = await self.auth_api_service.authenticate_user(user_phone)

                if auth_response.get("success") and auth_response.get("data"):
                    # Use the fresh API data which includes org_id

                    # IMPORTANT: User may have multiple profiles (buyer + seller)
                    # We need to select the CORRECT profile that was just registered

                    all_users = auth_response["data"]
                    logger.info(f"REGISTRATION_SERVICE: API returned {len(all_users)} user profiles for {user_phone}")

                    # Strategy 1: Match by user_id (most reliable if available from registration)
                    user_id_from_registration = entities.get("user_id")
                    fresh_user_data = None

                    if user_id_from_registration:
                        logger.info(f"REGISTRATION_SERVICE: Looking for user with ID: {user_id_from_registration}")
                        matching_by_id = [u for u in all_users if u.get("id") == user_id_from_registration]
                        if matching_by_id:
                            fresh_user_data = matching_by_id[0]
                            logger.info(f"REGISTRATION_SERVICE: ✓ Found user by ID match: {fresh_user_data.get('username')}")

                    # Strategy 2: Match by registration email (fallback)
                    if not fresh_user_data:
                        registration_email = entities.get("email")
                        if registration_email:
                            logger.info(f"REGISTRATION_SERVICE: Looking for user with email: {registration_email}")
                            matching_by_email = [u for u in all_users if u.get("username") == registration_email]
                            if matching_by_email:
                                fresh_user_data = matching_by_email[0]
                                logger.info(f"REGISTRATION_SERVICE: ✓ Found user by email match: {fresh_user_data.get('username')}")

                    # Strategy 3: Match by selfClient field matching user_type (fallback)
                    if not fresh_user_data:
                        expected_self_client = (user_type == "buyer")
                        logger.info(f"REGISTRATION_SERVICE: Looking for user with selfClient={expected_self_client} (user_type={user_type})")
                        matching_by_type = [u for u in all_users if u.get("selfClient") == expected_self_client]
                        if matching_by_type:
                            fresh_user_data = matching_by_type[0]
                            logger.info(f"REGISTRATION_SERVICE: ✓ Found user by selfClient match: {fresh_user_data.get('username')}")

                    # Final fallback: Use first user (with warning)
                    if not fresh_user_data:
                        fresh_user_data = all_users[0] if all_users else {}
                        logger.warning(f"REGISTRATION_SERVICE: ⚠ Could not match user by ID/email/type, using first profile: {fresh_user_data.get('username')}")

                    logger.info(f"REGISTRATION_SERVICE: Selected user profile - username: {fresh_user_data.get('username')}, selfClient: {fresh_user_data.get('selfClient')}, orgId: {fresh_user_data.get('orgId')}")

                    # Store user session with complete API data
                    user_obj = User.from_api_response(fresh_user_data)
                    logger.info(f"REGISTRATION_SERVICE: Created User object - role: {user_obj.role.value}, email: {user_obj.email}")
                    session_stored = await self.store_user_session(user_phone, user_obj)

                    if not session_stored:
                        logger.error(f"Failed to store user session for {user_type} {user_phone}")
                else:
                    logger.warning(f"Failed to fetch fresh user data, using registration entities for {user_phone}")
                    # Fallback to original method
                    session_stored = await self._store_user_session_after_registration(user_phone, entities, user_type)

                    if not session_stored:
                        logger.error(f"Failed to store user session for {user_type} {user_phone}")

                if user_type == "buyer":
                    # Check domain approval after email verification
                    user_id = entities.get("user_id")
                    if user_id:
                        from app.services.verification_check_service import VerificationCheckService
                        verification_service = VerificationCheckService(None, self.otp_service, self.whatsapp_service)
                        # Pass the fresh user data from API for domain check
                        user_data_for_domain_check = fresh_user_data if 'fresh_user_data' in locals() else entities
                        domain_result = await verification_service._check_domain_approval(user_id, False, user_data_for_domain_check)


                        if domain_result.get("approved"):
                            logger.info(f"Buyer registration completed for {user_phone} - domain approved")
                            

                            # Check for stored intent to determine next action
                            stored_intent_result = session.workflow_state.get("current_intent_result", {})
                            intent = stored_intent_result.get("intent", "general_inquiry")

                            logger.info(f"intet in handle registration otp validation is :{intent}")
                            
                            # if intent == "buy_something":
                            logger.info(f"entitiies:{entities}")
                            # Show buying options with buttons
                            buying_message = (
                                f"Your OTP has been verified successfully, and your account is now active and ready to use."
                                f"To serve you better, please update your profile and categories at {self.settings.procucev_rfq_details_url} "
                                f"You will receive your login details through E-mail.\n\n"
                                f"Let's continue with your *Buyer profile ({entities.get('email', 'your profile')})*.\n"
                                "What would you like to do today?\n"
                                "You can choose from the options below or type your request."
                            )

                            name = entities.get('name', 'there').split()[0].title()
                            header = f"Hello {name},"
                            buttons_config = [
                                {"id": "create_rfq", "title": "Create new RFQ"},
                                {"id": "rfq_status", "title": "Check RFQ Status"},
                                {"id": "search_bfs", "title": "Search Ready Stocks"}
                            ]

                            await self.whatsapp_service.send_configurable_buttons(
                                user_phone,
                                buying_message,
                                buttons_config,
                                header,
                                session_id=session
                            )

                            return {
                                "status": "buyer_options_presented",
                                "user_type": "buyer",
                                "email": entities.get('email')
                            }
                        else:
                            logger.info(f"Buyer registration completed for {user_phone} - domain not approved, awaiting manual approval")
                            # Domain not approved - send pending message
                            # Send email notification to support team
                            await self.support_notification_service.notify_buyer_registration_not_approved(
                                entities.get("name", "User"),
                                entities.get("email", "unknown"),
                                user_phone
                            )
                            
                            # Domain not approved - send pending message
                            pending_message = (
                                "*Registration received—thank you!*\n\n"
                                "We’re reviewing your details to ensure everything is set up perfectly for your onboarding. "
                                "Our team will get in touch shortly to complete the process, and once verified, "
                                "you’ll be able to access your account and start raising RFQs.\n\n"
                                "Feel free to return to this chat anytime to continue your journey with *Procucev* — simply type *“Hi”* to start the conversation again."

                            )

                            if self.session_manager:
                                await self.session_manager.send_and_track_message(user_phone, pending_message, session)
                            else:
                                await self.whatsapp_service.send_message(user_phone, pending_message, session_id=session)

                            # Call exit without showing exit message
                            from app.services.exit_service import ExitService
                            exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
                            await exit_service.handle_exit_intent(user_phone, session, show_message=False)
                            
                            return {
                                "status": "redirect_to_support",
                                "reason": "domain_not_approved",
                                "exit_completed": True
                            }
                    else:
                        logger.warning(f"Buyer registration completed for {user_phone} - missing user_id")
                        # No user_id found - redirect to support

                        
                        # Send email notification to support team
                        await self.support_notification_service.notify_buyer_registration_not_approved(
                            entities.get("name", "User"),
                            entities.get("email", "unknown"),
                            user_phone
                        )
                        
                        # No user_id found - redirect to support
                        pending_message = (
                            "*Registration received—thank you!*\n\n"
                            "We’re reviewing your details to ensure everything is set up perfectly for your onboarding. "
                            "Our team will get in touch shortly to complete the process, and once verified, "
                            "you’ll be able to access your account and start raising RFQs.\n\n"
                            "Feel free to return to this chat anytime to continue your journey with *Procucev* — simply type *“Hi”* to start the conversation again."

                        )

                        if self.session_manager:
                            await self.session_manager.send_and_track_message(user_phone, pending_message, session)
                        else:
                            await self.whatsapp_service.send_message(user_phone, pending_message, session_id=session)
                        
                        # Call exit without showing exit message
                        from app.services.exit_service import ExitService
                        exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
                        await exit_service.handle_exit_intent(user_phone, session, show_message=False)
                        
                        return {
                            "status": "redirect_to_support",
                            "reason": "missing_user_id",
                            "exit_completed": True
                        }
                else:
                    logger.info(f"Seller registration completed for {user_phone}")
                    # Sellers: Email verified successfully, proceed to main flow
                    
                    # Check for stored intent to determine next action
                    stored_intent_result = session.workflow_state.get("current_intent_result", {})
                    intent = stored_intent_result.get("intent", "general_inquiry")
                    
                    logger.info(f"Seller registration completed - processing intent: {intent}")
                    
                    # Send success message without buttons
                    name = entities.get('name', 'there').split()[0].title()
                    success_message = (
                        f"Hello {name},"
                        f"Your OTP has been verified successfully and your account is now active and ready to use."
                        f"To serve you better, Please update your profile and categories at {self.settings.procucev_rfq_details_url}.\n\n"
                        f"Got it, you'd like to sell items!\n"
                        f"Let's continue with your Seller profile ({entities.get('email', 'your profile')}).\n"
                    )

                    # Return status to continue processing the original message based on intent
                    return {
                        "status": "registration_completed",
                        "user_type": "seller",
                        "email": entities.get('email'),
                        "redirect_to_main_flow": True,
                        "original_intent": intent,
                        "original_message": stored_intent_result.get("original_message", "I want to sell items")
                    }
            
            # Handle maximum OTP attempts exceeded - call exit without message
            elif otp_result.get("status") == "max_otp_exceeded":
                logger.warning(f"Maximum OTP attempts exceeded for {user_phone}")
                from app.services.exit_service import ExitService
                exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
                return await exit_service.handle_exit_intent(user_phone, session, show_message=False)
            
            # Handle OTP service redirect to support
            elif otp_result.get("status") == "redirect_to_support":
                logger.warning(f"OTP validation failed for {user_phone}: {otp_result.get('reason')}")
                return await self._redirect_to_support(user_phone, otp_result.get("reason", "otp_error"), "OTP validation failed", session)

            return otp_result
                
        except Exception as e:
            logger.error(f"Registration OTP validation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "otp_validation_error", str(e), session)

    async def store_user_session(self, user_phone: str, user_details: User) -> bool:
        """Store user session data in Redis."""
        try:
            # Normalize phone number (remove + prefix for consistent Redis keys)
            normalized_phone = user_phone.lstrip('+')
            session_data = user_details.dict()
            session_data["authenticated_at"] = datetime.now().isoformat()

            # Token expires after 12 hours of inactivity
            success = await self.auth_redis_service.store(normalized_phone, session_data, expiry_seconds=43200)  # 12 hours

            if not success:
                logger.error(f"Failed to store session in Redis for user {normalized_phone}")

            return success
        except Exception as e:
            logger.error(f"Session storage error for {user_phone}: {e}")
            return False
    
    async def _store_user_session_after_registration(self, user_phone: str, entities: Dict, user_type: str) -> bool:
        """Store user session token after successful registration."""
        try:
            # Get the actual user_id and org_id from the registration API response if available
            user_id = entities.get("user_id") or f"reg_{user_phone}_{int(utc_now().timestamp())}"
            org_id = entities.get("org_id")  # This should be set from registration API response
            
            # Create User from registration data with proper org_id
            user_details = User(
                id=user_id,
                name=entities.get("name") or entities.get("full_name", ""),
                email=entities.get("email", ""),
                phone_number=user_phone,
                self_client=user_type == "buyer",
                role=user_type,
                is_registered=True,
                company_name=entities.get("company_name", ""),
                unique_id=f"reg_{user_phone}",
                org_id=org_id  # Include org_id from registration response
            )
            
            # Store session data in Redis
            session_data = user_details.dict()
            session_data["authenticated_at"] = utc_now().isoformat()
            session_data["registration_source"] = "whatsapp_bot"
            
            success = await self.auth_redis_service.store(user_phone, session_data, expiry_seconds=86400)  # 24 hours

            if not success:
                logger.error(f"Failed to store user session for {user_phone} after registration")
            
            return success
            
        except Exception as e:
            logger.error(f"Error storing user session after registration: {e}")
            return False
    


    async def _redirect_to_support(self, user_phone: str, issue_type: str, error_details: str, session: ConversationSession = None) -> Dict[str, Any]:
        """Redirect user to support team with issue-specific messages."""
        try:
            # Default message for registration issues
            support_message = (
                "*Registration Unsuccessful*\n"
                "We couldn't complete your registration at this time. Our support team will "
                "reach out to you shortly to help finalize your onboarding.\n\n"
                f"If you need immediate assistance, please contact us at *{self.settings.support_contact_info}*.\n\n"
                "*Thank you for choosing Procucev!*"
            )

            if self.session_manager and session:
                await self.session_manager.send_and_track_message(user_phone, support_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, support_message, session_id=session)

            logger.error(f"Support redirect for {user_phone}: {issue_type} - {error_details}")

            # Call exit service to properly clean up session (without showing exit message)
            from app.services.exit_service import ExitService
            exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
            await exit_service.handle_exit_intent(user_phone, session, show_message=False)

            return {
                "status": "redirected_to_support",
                "issue_type": issue_type,
                "support_ticket_created": True,
                "exit_completed": True
            }

        except Exception as e:
            logger.error(f"Support redirect error: {e}")
            return {"status": "error", "error": "Failed to redirect to support"}
    
    async def _check_exit_command(self, message_content) -> bool:
        """Check if user wants to exit registration."""
        # Handle both string and dictionary inputs
        if isinstance(message_content, dict):
            # For interactive messages, check button ID
            button_reply = message_content.get("button_reply", {})
            button_id = button_reply.get("id", "")
            if button_id:
                return button_id.lower().strip() in ["exit", "quit", "stop", "cancel"]
            return False
        elif isinstance(message_content, str):
            return message_content.lower().strip() in ["exit", "quit", "stop", "cancel"]
        else:
            return False
    
    async def _auto_fill_address_from_pincode(self, entities: Dict[str, Any], user_phone: str, session: ConversationSession) -> None:
        """Auto-fill address from pincode if valid pincode is provided."""
        try:
            pincode = entities.get("zipCode")
            if not pincode or entities.get("address1"):  # Skip if no pincode or address1 already exists
                return
            
            # Validate pincode format
            if not pincode.isdigit() or len(pincode) != 6:
                return
            
            # Get location details from pincode
            location_data = await get_location_from_pincode_async(pincode)
            
            if location_data:
                city = location_data.get("city", "")
                state = location_data.get("state", "")
                
                if city and state:
                    # Auto-fill address1 with city and state
                    entities["address1"] = f"{city}, {state}"
                    logger.info(f"Auto-filled address1 for {user_phone}: {city}, {state} from pincode {pincode}")
                else:
                    logger.warning(f"Incomplete location data for pincode {pincode}: {location_data}")
            else:
                # Keep invalid pincode and store error message for later display
                entities["_pincode_error"] = f"Pincode {pincode} does not exist. Please provide a valid Indian pincode."
                logger.warning(f"Invalid pincode {pincode} provided by {user_phone}")
                
        except Exception as e:
            logger.error(f"Error auto-filling address from pincode: {e}")
    
    async def _handle_registration_exit(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle exit during registration."""
        from app.services.exit_service import ExitService
        exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
        return await exit_service.handle_exit_intent(user_phone, session, show_message=False)

    async def _categorize_seller_products(self, seller_details: str, user_phone: str, session: ConversationSession) -> List[Dict[str, str]]:
        """
        Parse and categorize seller products during registration.
        Args:
            seller_details: The seller's product/service details string
            user_phone: User's phone number for tracking
            session: Current conversation session
        Returns:
            List of divisionCategories in format: [{"category": "...", "division": ""}, ...]
        """
        try:
            logger.info(f"Starting product categorization for seller {user_phone}")
            logger.info(f"Seller details to categorize: '{seller_details}'")

            # Step 1: Parse seller details into individual items
            parsing_result = await self.openai_service.parse_seller_product_items(seller_details)
            logger.info(f"Parsing result: {parsing_result}")

            if not parsing_result.get("success") or not parsing_result.get("items"):
                logger.warning(f"Failed to parse seller details or no items found: {parsing_result}")
                # Return empty list but don't fail registration
                return []

            items = parsing_result.get("items", [])
            logger.info(f"Parsed {len(items)} items from seller details: {items}")

            # Step 2: Get auto categorization service singleton (reuses preloaded model)
            try:
                from app.services.auto_categorization_service import get_auto_categorization_service
                categorization_service = get_auto_categorization_service()
                logger.info("Successfully initialized auto-categorization service")
            except Exception as e:
                logger.error(f"Failed to initialize auto-categorization service: {e}")
                # Return empty list but don't fail registration
                return []

            # Step 3: Categorize each item
            categorizations = []
            division_categories = []

            for i, item in enumerate(items, 1):
                logger.info(f"Categorizing seller item {i}/{len(items)}: '{item}'")

                try:
                    categorization_result = await categorization_service.categorize_item(
                        item_description=item,
                        user_id=user_phone,  # Use phone as user_id during registration
                        session_id=session.session_id if hasattr(session, 'session_id') else None
                    )
                    logger.info(f"Categorization result for '{item}': {categorization_result}")

                    if categorization_result.get("success"):
                        client_category = categorization_result.get("category")

                        categorizations.append({
                            "item": item,
                            "client_category": client_category,
                            "confidence_score": categorization_result.get("confidence_score"),
                            "method": categorization_result.get("method")
                        })

                        # Add to divisionCategories array (division field empty)
                        if client_category:
                            division_categories.append({
                                "category": client_category,
                                "division": ""
                            })

                        logger.info(f"✓ Categorized '{item}' as '{client_category}' (confidence: {categorization_result.get('confidence_score', 0)})")
                    else:
                        logger.warning(f"✗ Failed to categorize '{item}': {categorization_result.get('error', 'Unknown error')}")

                except Exception as e:
                    logger.error(f"✗ Error categorizing item '{item}': {e}")
                    # Continue with next item instead of failing completely

            # Step 4: Store results in session for reference
            session.workflow_state = session.workflow_state or {}
            session.workflow_state["seller_categorizations"] = {
                "original_details": seller_details,
                "parsed_items": items,
                "categorizations": categorizations,
                "total_items": len(items),
                "successfully_categorized": len([c for c in categorizations if c.get("client_category")])
            }

            logger.info(f"Seller categorization complete: {len(categorizations)}/{len(items)} items categorized")
            logger.info(f"Final division categories: {division_categories}")
            return division_categories

        except Exception as e:
            logger.error(f"Error in seller product categorization: {e}")
            # Don't fail registration if categorization fails - just log and continue
            logger.warning("Seller registration will continue without auto-categorization")
            return []

    async def _handle_neutral_greeting(self, user_phone: str, profiles: List[Dict],
                                     session: ConversationSession) -> Dict[str, Any]:
        """Handle Case 1: Neutral/Greeting Start."""
        try:
            # Group profiles by role
            buyer_profiles = [p for p in profiles if p.get('role') == 'buyer']
            seller_profiles = [p for p in profiles if p.get('role') == 'seller']
            
            # Get user's name and determine greeting
            if len(profiles) == 1:
                user_name = self._extract_user_name(profiles)
                greeting = f"👋 Hi {user_name}!"
            else:
                greeting = "👋 Hi there!"
            
            message_parts = [
                greeting,
                "I can help you with both Buying (creating/checking RFQs) and Selling (responding to buyer requests).",
                "",
                "Please select your profile to continue:"
            ]
            
            profile_options = []
            option_num = 1
            
            # Add all buyer profiles
            for profile in buyer_profiles:
                message_parts.append(f" {option_num}. {profile['email']} — Buyer")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — Buyer"
                })
                option_num += 1
            
            # Add all seller profiles
            for profile in seller_profiles:
                message_parts.append(f" {option_num}. {profile['email']} — Seller")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — Seller"
                })
                option_num += 1
            
            # Add registration option
            message_parts.append(f" {option_num}. Add or Register a new profile")
            profile_options.append({
                "number": option_num,
                "action": "register_new",
                "display": "Add or Register a new profile"
            })
            
            message_parts.extend([
                "",
                "Reply with the number corresponding to your account to continue."
            ])
            
            # Store in session for later reference
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_options'] = profile_options
            session.workflow_state['profile_selection_stage'] = 'neutral_greeting'
            
            # Send profile selection message
            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message, session_id=session)
            
            return {
                "status": "profile_selection_sent",
                "profiles_count": len(profiles),
                "selection_type": "neutral_greeting"
            }
        except Exception as e:
            logger.error(f"Error handling neutral greeting for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}
    
    def _extract_user_name(self, profiles: List[Dict]) -> str:
        """Extract user name from profiles """
        if len(profiles) == 1:
            profile = profiles[0]
            name = profile.get('fullName') or profile.get('name')
            if name:
                # Extract first name only and capitalize first letter
                first_name = name.strip().split()[0]
                return first_name.capitalize()
        return "there"