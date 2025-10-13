"""
Authentication Helper Functions.

Utility functions for authentication workflow.
"""

import logging
from typing import Dict, Any, List, Optional, Type
import re
from pydantic import BaseModel

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

            message_lines.append("\nWe'll have you registered right away.")

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
    def generate_registration_questions_dynamic(schema: Type[BaseModel], collected_entities: Dict, missing_fields: List[str]) -> str:
        """Generate registration questions dynamically based on schema descriptions."""
        try:
            # Build greeting with user name if available
            greeting = "Great"
            user_name = collected_entities.get("name") or collected_entities.get("full_name")
            if user_name:
                greeting += f", {user_name.split()[0].lower()}!"
            else:
                greeting += "!"
            
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
                return greeting + acknowledgment + "I still need:\n\n" + "\n".join(questions)
            else:
                return greeting + acknowledgment + "Please provide the remaining registration details."
                
        except Exception as e:
            logger.error(f"Registration questions generation error: {e}")
            return "Please provide the remaining registration details."
    
    @staticmethod
    def build_registration_payload_dynamic(schema: Type[BaseModel], collected_entities: Dict, user_phone: str) -> Dict[str, Any]:
        """
        Build API payload dynamically based on schema.
        
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

            # Optional field mapping if API expects different keys
            field_mapping = {
                "pincode": "zipCode",
                "location": "address1",
                "products_services": "details",
                "company_name": "companyName",
                "full_name": "name"  # Seller uses full_name, API expects name
            }

            for name, field_info in schema.model_fields.items():
                if name in {"source_type", "organizationPhonenumber", "whatsapp"}:
                    continue  # Already handled

                value = collected_entities.get(name)

                # Include required OR optional with meaningful value
                if field_info.is_required() or (value and str(value).strip()):
                    api_key = field_mapping.get(name, name)
                    payload[api_key] = value

            # Default "details" field for buyer if missing
            if "details" not in payload and "name" in payload:
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