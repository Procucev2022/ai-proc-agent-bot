"""
Email Service for handling template-based email notifications.

This service manages email templates, processes dynamic content,
and sends emails through the GMT Procucev email API.
"""

import json
import logging
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

from app.config import get_settings
from app.procucev_apis.email_service_api import EmailServiceAPI

logger = logging.getLogger(__name__)

class EmailService:
    """
    Service for handling template-based email notifications.
    
    Manages email templates, processes dynamic content, and sends emails
    with support for buyer/seller role-specific templates.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.email_api = EmailServiceAPI()
        self.templates_cache = {}
        
    def _load_template(self, template_name: str) -> Optional[Dict[str, Any]]:
        """Load email template from JSON file."""
        if template_name in self.templates_cache:
            return self.templates_cache[template_name]
            
        # Get the base directory of the application
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Build template paths to try
        paths_to_try = [
            # Relative to current working directory
            os.path.normpath(os.path.join(self.settings.email_templates_path, f"{template_name}.json")),
            # Absolute path from current working directory
            os.path.abspath(os.path.join(self.settings.email_templates_path, f"{template_name}.json")),
            # Relative to app directory
            os.path.join(app_dir, "app", "email_templates", f"{template_name}.json"),
            # Direct path from app directory
            os.path.join(app_dir, "email_templates", f"{template_name}.json")
        ]
        
        for path in paths_to_try:
            try:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        template = json.load(f)
                        self.templates_cache[template_name] = template
                        logger.info(f"Successfully loaded email template: {path}")
                        return template
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in template {path}: {e}")
                return None
            except Exception as e:
                logger.error(f"Error loading template {path}: {e}")
                continue
        
        # If all paths fail, log detailed error
        logger.error(f"Email template not found: {template_name}")
        logger.error(f"Tried paths: {paths_to_try}")
        logger.error(f"Current working directory: {os.getcwd()}")
        logger.error(f"App directory: {app_dir}")
        logger.error(f"Template directory exists: {os.path.exists(self.settings.email_templates_path)}")
        
        return None
    
    def _process_template(self, template: Dict[str, Any], variables: Dict[str, Any], user_role: str = None) -> Dict[str, Any]:
        """Process template with variables and role-specific logic."""
        # Check if template supports the user role
        if user_role:
            if user_role == "buyer" and not template.get("supports_buyer", True):
                logger.warning(f"Template {template.get('scenario_id')} does not support buyer role")
            elif user_role == "seller" and not template.get("supports_seller", True):
                logger.warning(f"Template {template.get('scenario_id')} does not support seller role")
        
        # Process template fields
        processed = {}
        
        # Replace hardcoded support email with config value
        to_field = template.get("to", "")
        if to_field == "support@procucev.com":
            to_field = self.settings.support_email
        
        # Process recipients
        processed["to"] = self._process_recipients(to_field, variables)
        processed["cc"] = self._process_recipients(template.get("cc", ""), variables)
        processed["bcc"] = self._process_recipients(template.get("bcc", ""), variables)
        
        # Process subject and body
        processed["subject"] = self._substitute_variables(template.get("subject", ""), variables)
        processed["body"] = self._substitute_variables(template.get("body", ""), variables)
        
        # Add signature
        signature = template.get("signature", self.settings.email_signature)
        processed["body"] += f"\n\n{signature}"
        
        return processed
    
    def _process_recipients(self, recipients: str, variables: Dict[str, Any]) -> List[str]:
        """Process recipient field and return list of email addresses."""
        if not recipients:
            return []
        
        # Substitute variables in recipients
        processed_recipients = self._substitute_variables(recipients, variables)
        
        # Split by comma and clean up
        recipient_list = [email.strip() for email in processed_recipients.split(",") if email.strip()]
        
        return recipient_list
    
    def _substitute_variables(self, text: str, variables: Dict[str, Any]) -> str:
        """Substitute variables in text using {variable_name} format."""
        try:
            # Replace hardcoded support email in text
            text = text.replace("support@procucev.com", self.settings.support_email)
            return text.format(**variables)
        except KeyError as e:
            logger.warning(f"Missing variable in template: {e}")
            return text
        except Exception as e:
            logger.error(f"Error substituting variables: {e}")
            return text
    
    async def send_support_email(self, template_name: str, variables: Dict[str, Any], user_role: str = None) -> Dict[str, Any]:
        """Send support email using template name directly."""
        return await self.send_email_by_template(template_name, variables, user_role)
    
    async def send_email_by_template(self, template_name: str, variables: Dict[str, Any], user_role: str = None, attachment: str = None) -> Dict[str, Any]:
        """Send email using specified template."""
        try:
            # Load template
            template = self._load_template(template_name)
            if not template:
                return {
                    "statusCode": "404",
                    "message": f"Template not found: {template_name}",
                    "status": "Failure"
                }
            
            # Process template
            email_data = self._process_template(template, variables, user_role)
            
            # Add attachment if provided
            if attachment:
                email_data["attachment"] = attachment
            
            # Validate email data
            if not email_data["to"]:
                logger.error("No recipients specified in email")
                return {
                    "statusCode": "400",
                    "message": "No recipients specified",
                    "status": "Failure"
                }
            
            logger.info(f"Sending email with template {template_name} to {email_data['to']}")
            
            # Send email
            result = await self.email_api.send_email(email_data)
            
            # Log email sending
            logger.info(f"Email sent using template {template_name} to {email_data['to']}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending email with template {template_name}: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "status": "Failure"
            }
    
    def get_template_info(self, template_name: str) -> Optional[Dict[str, Any]]:
        """Get template information by template name."""
        return self._load_template(template_name)
    
    def list_available_templates(self) -> List[Dict[str, Any]]:
        """List all available email templates."""
        templates = []
        template_dir = self.settings.email_templates_path
        
        try:
            for filename in os.listdir(template_dir):
                if filename.endswith('.json'):
                    template_name = filename[:-5]  # Remove .json extension
                    template = self._load_template(template_name)
                    if template:
                        templates.append({
                            "name": template_name,
                            "scenario_id": template.get("scenario_id"),
                            "subject": template.get("subject"),
                            "supports_buyer": template.get("supports_buyer", True),
                            "supports_seller": template.get("supports_seller", True)
                        })
        except Exception as e:
            logger.error(f"Error listing templates: {e}")
        
        return templates