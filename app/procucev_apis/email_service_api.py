"""
Email Service API.

This service handles email notifications for buyers, sellers, and support team
integrating with the GMT Procucev email backend service.
"""

import logging
from typing import Dict, Any, List
from datetime import datetime

from app.procucev_apis.procucev_api_client import ProcucevAPIClient

logger = logging.getLogger(__name__)

class EmailServiceAPI:
    """
    Service for handling email notification operations.
    
    Provides methods for sending emails to buyers, sellers, and support team
    with support for TO, CC, and BCC recipients.
    """
    
    def __init__(self):
        self.api_client = ProcucevAPIClient()
        
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
            
            logger.info(f"Sending email via GMT API to {payload['to']}")
            response = await self.api_client.post(
                endpoint=email_url,
                json_data=payload,
                require_auth=True
            )
            logger.info(f"GMT API response: {response.get('success')}")
            
            if response["success"]:
                return {
                    "statusCode": "200",
                    "message": "Email Sent Successfully",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None,
                    "data": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": response.get("data", {}).get("message", "Email sending failed"),
                    "errorMsg": response.get("data", {}).get("error", None),
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None,
                    "data": None
                }
                        
        except Exception as e:
            logger.error(f"Email sending error: {e}")
            logger.error(f"Failed payload: {payload if 'payload' in locals() else 'N/A'}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None,
                "data": None
            }