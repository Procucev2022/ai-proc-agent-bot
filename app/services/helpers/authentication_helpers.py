"""
Authentication Helper Functions.

Utility functions for authentication workflow.
"""

import logging
from typing import Dict, Any, List, Optional
import re

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