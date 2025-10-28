"""
Authentication Helper Functions.

Utility functions for authentication workflow.
"""

import logging
from typing import Dict, Any, List, Optional, Type
import re
from pydantic import BaseModel
from ...utils.pincode_lookup import get_location_from_pincode_async

logger = logging.getLogger(__name__)


class AuthenticationHelpers:
    """Helper functions for authentication workflow."""
    
    @staticmethod
    def extract_user_details(api_response: List) -> Optional[Dict]:
        """Extract user details from API response."""
        try:
            if not api_response:
                return None
            
            # Filter for selfClient=true users first
            self_client_users = [user for user in api_response if user.get("selfClient") is True]
            
            if self_client_users:
                return self_client_users[0]
            else:
                return api_response[0]
                
        except Exception as e:
            logger.error(f"User details extraction error: {e}")
            return None
    
    @staticmethod
    def format_email_list(emails: List[str]) -> str:
        """Format email list for user selection."""
        try:
            return "\n".join([f"{i+1}. {email}" for i, email in enumerate(emails)])
        except Exception as e:
            logger.error(f"Email list formatting error: {e}")
            return ""
    
    @staticmethod
    def validate_otp_format(otp: str) -> bool:
        """Validate OTP format."""
        try:
            # Remove spaces and check if it's 4-6 digits
            clean_otp = re.sub(r'\s+', '', otp)
            return clean_otp.isdigit() and 4 <= len(clean_otp) <= 6
        except Exception as e:
            logger.error(f"OTP validation error: {e}")
            return False
    
    @staticmethod
    def extract_emails_from_user_data(user_data: Dict) -> List[str]:
        """Extract emails from user data."""
        try:
            emails = []
            email_field = user_data.get("email")
            
            if email_field:
                if isinstance(email_field, list):
                    emails.extend(email_field)
                elif isinstance(email_field, str):
                    emails.append(email_field)
            
            return [email for email in emails if email and "@" in email]
            
        except Exception as e:
            logger.error(f"Email extraction error: {e}")
            return []
        
    @staticmethod
    def generate_registration_message(schema: Type[BaseModel], role: str, show_optional: bool = True) -> str:
        """Generate a registration intro message dynamically from schema fields."""
        try:
            # Return specific messages based on role
            if role.lower() == "buyer":
                return (
                    "Hello Buyer!\n"
                    "To get started, please share the following details:\n"
                    "1. Full name\n"
                    "2. Company name\n"
                    "3. Organization email\n"
                    "4. Pincode \n"
                    "We'll have you registered right away.\n"
                    "Please ensure your email is correct, as we will send an OTP to verify it in the next step"
                )
            elif role.lower() == "seller":
                return (
                    "Hello Seller!\n"
                    "To get started, please share the following details:\n"
                    "1. Full name\n"
                    "2. Company name\n"
                    "3. Organization email\n"
                    "4. Location\n"
                    "5. Pincode\n"
                    "6. GSTIN number\n"
                    "7. Products or Services offered\n\n"
                    "We'll have you registered right away.\n"
                    "please ensure your email is correct, as we will send an OTP to verify it in the next step"
                )
            else:
                # Fallback to original dynamic logic for other roles
                fields = schema.model_fields
                required_fields = []
                optional_fields = []

                for name, field_info in fields.items():
                    # Skip internal or system fields
                    if name in {"source_type"}:
                        continue

                    label = field_info.description or name.replace("_", " ").title()

                    # Determine if the field is required or optional
                    if field_info.is_required():
                        required_fields.append(label)
                    else:
                        optional_fields.append(label)

                # Build numbered required list
                numbered_required = [
                    f"{idx + 1}. {label}" for idx, label in enumerate(required_fields)
                ]

                # Build numbered optional list (only if enabled)
                numbered_optional = [
                    f"{idx + 1}. {label}" for idx, label in enumerate(optional_fields)
                ] if show_optional else []

                # Construct message
                message_lines = [
                    f"Hello {role.capitalize()}!",
                    "To get started, please share the following details:\n",
                    "\n".join(numbered_required) if numbered_required else "(No required fields)",
                ]

                if numbered_optional:
                    message_lines.append("\nOptional fields:\n" + "\n".join(numbered_optional))

                message_lines.append("\nWe'll have you registered right away." )
                message_lines.append("\nPlease ensure your email is correct, as we will send an OTP to verify it in the next step")

                return "\n".join(message_lines)
        except Exception as e:
            logger.error(f"Registration message generation error: {e}")
            # Fallback message
            return (
                f"Hello {role.capitalize()}! To get started, please provide your details. "
                "We'll guide you through the registration process shortly."
            )
    
    @staticmethod
    def get_required_fields(schema: Type[BaseModel]) -> List[str]:
        """Return required field names from a Pydantic schema."""
        return [name for name, field_info in schema.model_fields.items() 
                if field_info.is_required() and name not in {"source_type"}]
    
    @staticmethod
    def get_missing_fields(schema: Type[BaseModel], collected_entities: Dict) -> List[str]:
        """Return list of missing required fields based on collected entities."""
        required_fields = AuthenticationHelpers.get_required_fields(schema)
        return [field for field in required_fields if not collected_entities.get(field)]
    
    @staticmethod
    def generate_confirmation_message_dynamic(schema: Type[BaseModel], collected_entities: Dict) -> str:
        """Generate confirmation message fully schema-driven - no hardcoded fields."""
        try:
            message = "Please confirm your registration details:\n\n"
            
            # Iterate all schema fields dynamically
            for name, field_info in schema.model_fields.items():
                # Skip internal/system fields
                if name in {"source_type"}:
                    continue
                
                # Use description if present, otherwise humanize field name
                field_desc = field_info.description or name.replace("_", " ").title()
                
                # Get value from collected entities or show N/A
                value = collected_entities.get(name, "N/A")
                
                # Only show if we have a meaningful value
                if value and str(value).strip() and value != "N/A":
                    message += f"• {field_desc}: {value}\n"
            
            message += "\nPlease re-check your email, as an OTP will be sent to complete the registration process."
            return message
            
        except Exception as e:
            logger.error(f"Confirmation message generation error: {e}")
            return "Please confirm your registration details. (Error generating message)"
    
    @staticmethod
    def generate_registration_questions_dynamic(schema: Type[BaseModel], collected_entities: Dict, missing_fields: List[str], validation_error_message: Optional[str] = None) -> str:
        """Generate registration questions dynamically based on schema descriptions."""
        try:
            # Build greeting with user name if available
            greeting = "Great"
            user_name = collected_entities.get("name") or collected_entities.get("full_name")
            if user_name:
                greeting += f", {user_name.split()[0].lower()}!"
            else:
                greeting += "!"
            
            # Add validation error if present
            validation_notice = ""
            if validation_error_message:
                validation_notice = f"\n {validation_error_message}\n"
            
            # Acknowledge collected fields
            acknowledgment = ""
            if collected_entities:
                collected = []
                for name, field_info in schema.model_fields.items():
                    if name in {"source_type"}:  # Skip internal fields
                        continue
                    value = collected_entities.get(name)
                    if value:
                        field_desc = field_info.description or name.replace("_", " ").title()
                        collected.append(f"{field_desc}: {value}")
                if collected:
                    acknowledgment = f" I've got:\n\n{chr(10).join(collected)}\n\n"
            
            # Ask for missing fields using their description
            questions = []
            for field in missing_fields:
                if field in schema.model_fields:
                    field_info = schema.model_fields[field]
                    field_desc = field_info.description or field.replace("_", " ").title()
                    questions.append(f"• What's your {field_desc.lower()}?")
            
            if questions:
                return greeting + validation_notice + acknowledgment + "I still need:\n\n" + "\n".join(questions)
            else:
                return greeting + validation_notice + acknowledgment + "Please provide the remaining registration details."
                
        except Exception as e:
            logger.error(f"Registration questions generation error: {e}")
            return "Please provide the remaining registration details."
    
    @staticmethod
    def build_registration_payload_dynamic(schema: Type[BaseModel], collected_entities: Dict, user_phone: str) -> Dict[str, Any]:
        """
        Build API payload dynamically based on schema with correct field mapping.
        
        Rules:
        1. Include all required fields from schema.
        2. Include optional fields only if user provided meaningful values.
        3. Add default/internal values like source_type, organizationPhonenumber, whatsApp.
        4. Use field mapping to convert schema field names to API payload keys.
        """
        try:
            from app.schemas.user import normalize_phone_number

            # Base payload
            payload = {
                "organizationPhonenumber": normalize_phone_number(user_phone),
                "whatsApp": True,
                "source_type": "W"
            }

            for name, field_info in schema.model_fields.items():
                if name in {"source_type", "organizationPhonenumber", "whatsApp"}:
                    continue  # Already handled

                value = collected_entities.get(name)

                # Include required OR optional with meaningful value
                if field_info.is_required() or (value and str(value).strip()):
                    payload[name] = value

            # Default "details" field for buyer if missing
            if "details" not in payload and payload.get("name"):
                payload["details"] = "Registered via WhatsApp bot"

            return payload

        except Exception as e:
            logger.error(f"Payload generation error: {e}")
            # Fallback minimal payload
            return {
                "organizationPhonenumber": user_phone,
                "source_type": "W",
                "whatsApp": True
            }
    
    @staticmethod
    async def validate_pincode(entities: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
        """
        Validate pincode in entities using API-based lookup.
        
        Args:
            entities: Dict of extracted entities
            
        Returns:
            tuple: (updated_entities, validation_error_message)
        """
        validated_entities = entities.copy()
        validation_error_message = None
        # Check both possible field names for backward compatibility
        pincode = entities.get("zipCode") or entities.get("pincode")
        pincode_field = "zipCode" if "zipCode" in entities else "pincode"
        
        if pincode:
            try:
                # Clean and validate pincode format
                clean_pincode = str(pincode).strip()
                if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                    logger.info(f"Invalid pincode format: {pincode}")
                    validated_entities[pincode_field] = None
                    validation_error_message = "Pincode does not exist. Please provide a valid Indian pincode."
                else:
                    # Use API to validate pincode existence
                    location_data = await get_location_from_pincode_async(clean_pincode)
                    if not location_data:
                        logger.info(f"Pincode {pincode} does not exist")
                        validated_entities[pincode_field] = None
                        validation_error_message = "Pincode does not exist. Please provide a valid Indian pincode."
                    else:
                        logger.info(f"Pincode {pincode} is valid")
            except Exception as e:
                logger.error(f"Error validating pincode {pincode}: {e}")
                validated_entities[pincode_field] = None
                validation_error_message = "Pincode does not exist. Please provide a valid Indian pincode."
        
        return validated_entities, validation_error_message