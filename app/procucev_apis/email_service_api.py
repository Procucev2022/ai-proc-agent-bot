"""
Email Service API.

This service handles email notifications for buyers, sellers, and support team
integrating with the GMT Procucev email backend service.
"""

import logging
from typing import Dict, Any, List
from datetime import datetime

from app.procucev_apis.procucev_api_client import get_procucev_api_client

logger = logging.getLogger(__name__)

class EmailServiceAPI:
    """
    Service for handling email notification operations.

    Provides methods for sending emails to buyers, sellers, and support team
    with support for TO, CC, and BCC recipients.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()
        
    async def send_email(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send email notification through GMT Procucev email service.
        
        Args:
            email_data: Dictionary containing email details:
                - to: List of recipient email addresses
                - cc: List of CC email addresses (optional)
                - bcc: List of BCC email addresses (optional)
                - subject: Email subject line
                - body: Email body content
        
        Returns:
            Dict containing API response with status and message
        """
        try:
            email_url = "/rest/gmt/sendEmail"
            
            # Prepare request payload
            payload = {
                "to": email_data.get("to", []),
                "cc": email_data.get("cc", []),
                "bcc": email_data.get("bcc", []),
                "subject": email_data.get("subject", ""),
                "body": email_data.get("body", "")
            }
            
            # Add attachments if provided
            if email_data.get("attachments"):
                payload["attachments"] = email_data.get("attachments")
            
            logger.info(f"Sending email via GMT API to {payload['to']}")
            response = await self.api_client.post(
                endpoint=email_url,
                json_data=payload,
                require_auth=True,
                api_title="send_email"
            )
            logger.info(f"GMT API response: {response.get('success')}")
            
            if response["status"] == "Success" and response.get("statusCode") == "200":
                return {
                    "statusCode": response.get("statusCode", "200"),
                    "message": response.get("message", "Email Sent Successfully"),
                    "errorMsg": response.get("errorMsg", None),
                    "timestamp": response["timestamp"],
                    "status": response.get("status", "Success"),
                    "type": response.get("data", None),
                    "data": response.get("data", None)
                }
            else:
                return {
                    "statusCode": response.get("statusCode", "400"),
                    "message": response.get("message", "Email sending failed"),
                    "errorMsg": response.get("errorMsg", None),
                    "timestamp": response["timestamp"],
                    "status": response.get("status", "Failure"),
                    "type": response.get("data", None),
                    "data": response.get("data", None)
                }
                        
        except Exception as e:
            logger.error(f"Email sending error: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None,
                "data": None
            }