"""
Support Helper Functions.

Scalable support class with tag-based template system and email API integration.
Architecture similar to entity extraction service with OpenAI integration.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

from app.services.openai_service import OpenAIService
from app.procucev_apis.email_service_api import EmailServiceAPI

logger = logging.getLogger(__name__)


class SupportHelpers:
    """Helper functions for support operations with tag-based template system."""
    
    def __init__(self, openai_service=None, email_service=None):
        """Initialize support helpers with dependencies."""
        self.openai_service = openai_service or OpenAIService()
        self.email_service = email_service or EmailServiceAPI()
        self.templates_dir = Path(__file__).parent.parent.parent / "email_templates"
        self.tools_dir = Path(__file__).parent.parent.parent / "tools"
    
    def load_template(self, tag: str) -> Optional[Dict[str, Any]]:
        """
        Load email template based on tag.
        
        Template path: /app/email_templates/{tag}.json
        
        Args:
            tag: Template tag name (e.g., "support_non_standard_request")
            
        Returns:
            Template dictionary or None if not found
        """
        try:
            template_path = self.templates_dir / f"{tag}.json"
            
            if not template_path.exists():
                logger.error(f"Template not found: {template_path}")
                return None
            
            with open(template_path, 'r', encoding='utf-8') as f:
                template = json.load(f)
            
            logger.info(f"Loaded template for tag: {tag}")
            return template
            
        except Exception as e:
            logger.error(f"Error loading template {tag}: {e}")
            return None
    
    def integrate_template_and_data(self, template: Dict[str, Any], user_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Integrate template with user data to create complete email structure.
        
        Args:
            template: Email template dictionary
            user_data: User data for template variables
            
        Returns:
            Complete email structure ready for API call
        """
        try:
            # Process template fields with user data
            email_structure = {}
            
            # Format each template field with user data
            for key, value in template.items():
                if isinstance(value, str) and value:
                    try:
                        # Format string with user data variables
                        email_structure[key] = value.format(**user_data)
                    except KeyError as e:
                        logger.warning(f"Missing user data variable {e} for field {key}")
                        email_structure[key] = value
                    except Exception as e:
                        logger.warning(f"Error formatting field {key}: {e}")
                        email_structure[key] = value
                else:
                    email_structure[key] = value
            
            # Convert to email API format
            api_email_data = {
                "to": [email_structure.get("to", "")],
                "cc": self._parse_email_list(email_structure.get("cc", "")),
                "bcc": self._parse_email_list(email_structure.get("bcc", "")),
                "subject": email_structure.get("subject", ""),
                "body": self._build_email_body(email_structure)
            }
            
            # Clean empty recipients
            api_email_data["to"] = [email for email in api_email_data["to"] if email.strip()]
            api_email_data["cc"] = [email for email in api_email_data["cc"] if email.strip()]
            api_email_data["bcc"] = [email for email in api_email_data["bcc"] if email.strip()]
            
            logger.info(f"Email structure created with {len(api_email_data['to'])} recipients")
            return api_email_data
            
        except Exception as e:
            logger.error(f"Error integrating template and data: {e}")
            raise
    
    async def send_support_email(self, tag: str, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main email function - complete support email workflow.
        
        Architecture:
        1. Load template by tag
        2. Integrate template with user data
        3. Make API call to send email
        
        Args:
            tag: Template tag (maps to /app/email_templates/{tag}.json)
            user_data: User data for template variables
            
        Returns:
            Email sending result with success status
        """
        try:
            logger.info(f"Processing support email with tag: {tag}")
            
            # 1. Load template
            template = self.load_template(tag)
            if not template:
                return {
                    "success": False,
                    "error": f"Template not found for tag: {tag}",
                    "tag": tag
                }
            
            # 2. Integrate template and user data
            email_data = self.integrate_template_and_data(template, user_data)
            
            # 3. API call to send email
            email_result = await self.email_service.send_email(email_data)
            
            # Log operation
            self._log_support_operation(tag, user_data, email_result)
            
            return {
                "success": email_result.get("status") == "Success",
                "tag": tag,
                "email_result": email_result,
                "recipients": len(email_data.get("to", [])),
                "subject": email_data.get("subject", "")
            }
            
        except Exception as e:
            logger.error(f"Support email sending failed for tag {tag}: {e}")
            return {
                "success": False,
                "error": str(e),
                "tag": tag
            }
    
   