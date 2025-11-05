"""
Authentication Helper Functions.

Utility functions for authentication workflow.
"""

import logging
from typing import Dict, Any, List, Optional, Type
import re
from pydantic import BaseModel, ValidationError
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
    def generate_registration_message(schema: Type[BaseModel], role: str, show_optional: bool = False) -> str:
        """Generate a conversational registration message from schema fields."""
        try:
            fields = schema.model_fields
            required_fields = []

            for name, field_info in fields.items():
                # Skip internal or system fields and address field
                if name in {"source_type", "address"}:
                    continue

                # Only include required fields
                if field_info.is_required():
                    label = field_info.description or name.replace("_", " ").title()
                    # Make field names bold for WhatsApp
                    bold_label = f"*{label}*"
                    required_fields.append(bold_label)

            if not required_fields:
                field_text = "your registration details"
            else:
                if len(required_fields) > 1:
                    field_text = (
                        ", ".join(required_fields[:-1]) + f", and {required_fields[-1]}"
                    )
                else:
                    field_text = required_fields[0]

            # Build conversational message
            message = (
                f"Hello {role.capitalize()}! 👋\n"
                f"To get started, please share your {field_text}.\n"
                f"Once received, we'll complete your Registration.\n\n"
                f"Kindly ensure your Email Address is accurate, as you'll receive an OTP there for Verification."
            )

            return message

        except Exception as e:
            logger.error(f"Registration message generation error: {e}")
            return (
                f"Hello {role.capitalize()}! 👋 To get started, please provide your details. "
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
                greeting += f", {user_name.split()[0].title()}!"
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
                    acknowledgment = f"\n I've got:\n\n{chr(10).join(collected)}\n\n"
            
            # Check if validation error is pincode-related
            is_pincode_error = validation_error_message and ("pincode" in validation_error_message.lower() or "zipcode" in validation_error_message.lower())
            
            # Ask for missing fields using their description
            questions = []
            for field in missing_fields:
                if field in schema.model_fields:
                    field_info = schema.model_fields[field]
                    field_desc = field_info.description or field.replace("_", " ").title()
                    
                    # Skip pincode and location questions if there's a pincode validation error
                    if is_pincode_error and field in ["zipCode", "address"]:
                        continue
                    
                    if field == "zipCode":
                        questions.append(f"• What's your pincode?")
                    elif field == "address":
                        questions.append(f"• What's your location?")
                    else:
                        questions.append(f"• What's your {field_desc.lower()}?")
            
            # Add validation error as a question if present
            if validation_error_message:
                questions.append(f"• {validation_error_message}")
            
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
    async def validate_entities(entities: Dict[str, Any], SchemaModel: Type[BaseModel]) -> tuple[Dict[str, Any], Optional[str]]:
        """
        Validate and normalize entity data using schema and external checks.
        
        Args:
            entities: Dict of extracted entities.
            SchemaModel: BuyerRegistrationSchema or SellerRegistrationSchema.

        Returns:
            (validated_entities, validation_error_message)
        """
        validated_entities = entities.copy()
        validation_error_message = None

        # --- 1. Auto-format fields before validation ---
        if "name" in validated_entities and validated_entities["name"]:
            validated_entities["name"] = validated_entities["name"].strip().title()

        if "companyName" in validated_entities and validated_entities["companyName"]:
            validated_entities["companyName"] = validated_entities["companyName"].strip().title()

        if "email" in validated_entities and validated_entities["email"]:
            validated_entities["email"] = validated_entities["email"].strip().lower()

        if "organizationPhonenumber" in validated_entities and validated_entities["organizationPhonenumber"]:
            validated_entities["organizationPhonenumber"] = re.sub(r"\s+", "", validated_entities["organizationPhonenumber"])

        if "zipCode" in validated_entities and validated_entities["zipCode"]:
            validated_entities["zipCode"] = str(validated_entities["zipCode"]).strip()

        if "address" in validated_entities and validated_entities["address"]:
            validated_entities["address"] = validated_entities["address"].strip().title()

        if "gstin" in validated_entities and validated_entities["gstin"]:
            validated_entities["gstin"] = validated_entities["gstin"].strip().upper()

        # --- 2. Schema-based validation (Pydantic) ---
        try:
            from pydantic import ValidationError
            validated_model = SchemaModel(**validated_entities)
            validated_entities = validated_model.model_dump()
        except ValidationError as e:
            # Collect first validation error message
            first_error = e.errors()[0]
            field_name = first_error.get('loc', [''])[0]
            error_msg = first_error.get('msg', '')
            validation_error_message = f"{field_name}: {error_msg}"
            logger.warning(f"Schema validation failed: {validation_error_message}")
            return validated_entities, validation_error_message

        # --- 3. External validation: pincode existence check ---
        pincode = validated_entities.get("zipCode")
        if pincode:
            try:
                if not pincode.isdigit() or len(pincode) != 6:
                    validation_error_message = "Pincode must be a 6-digit number."
                    validated_entities["zipCode"] = None
                else:
                    location_data = await get_location_from_pincode_async(pincode)
                    if not location_data:
                        validation_error_message = "Pincode does not exist. Please provide a valid Indian pincode."
                        validated_entities["zipCode"] = None
            except Exception as e:
                logger.error(f"Error validating pincode {pincode}: {e}")
                validation_error_message = "Error verifying pincode. Please recheck and try again."
                validated_entities["zipCode"] = None

        return validated_entities, validation_error_message