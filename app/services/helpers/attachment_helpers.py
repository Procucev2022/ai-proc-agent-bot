"""
Helper utilities for handling image and document attachments.

Contains methods for downloading, processing, and managing attachments
in the WhatsApp RFQ workflow.
"""

import logging
import aiohttp
import base64
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class AttachmentHelpers:
    """Helper methods for attachment processing."""
    
    @staticmethod
    async def download_and_encode_attachment(file_url: str, filename: str = None, mime_type: str = None) -> Dict[str, Any]:
        """
        Download attachment from URL and encode as base64.
        
        Args:
            file_url: URL to download the file from
            filename: Optional filename (auto-generated if not provided)
            mime_type: Optional MIME type (defaults to image/jpeg)
            
        Returns:
            Dict with attachment data or error information
        """
        try:
            if not filename:
                filename = f"attachment_{int(datetime.now().timestamp())}"
            
            if not mime_type:
                # Determine MIME type from filename or default to image/jpeg
                if filename.lower().endswith('.png'):
                    mime_type = "image/png"
                elif filename.lower().endswith('.pdf'):
                    mime_type = "application/pdf"
                elif filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg'):
                    mime_type = "image/jpeg"
                else:
                    mime_type = "image/jpeg"  # Default
            
            logger.info(f"Downloading attachment: {filename} from {file_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, timeout=30) as response:
                    if response.status == 200:
                        file_data = await response.read()
                        file_content_b64 = base64.b64encode(file_data).decode('utf-8')
                        
                        # Validate file size (limit to 10MB)
                        if len(file_data) > 10 * 1024 * 1024:  # 10MB
                            return {
                                "success": False,
                                "error": "File size exceeds 10MB limit"
                            }
                        
                        logger.info(f"Successfully downloaded and encoded attachment: {filename} ({len(file_data)} bytes)")
                        
                        return {
                            "success": True,
                            "attachment": {
                                "file_name": filename,
                                "file_type": mime_type,
                                "file_content": file_content_b64,
                                "uploaded_at": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                "status": "pending",
                                "file_size": len(file_data)
                            }
                        }
                    else:
                        logger.error(f"Failed to download attachment: HTTP {response.status}")
                        return {
                            "success": False,
                            "error": f"Failed to download file: HTTP {response.status}"
                        }
                        
        except aiohttp.ClientTimeout:
            logger.error("Timeout downloading attachment")
            return {
                "success": False,
                "error": "Timeout downloading file"
            }
        except Exception as e:
            logger.error(f"Error downloading attachment: {e}")
            return {
                "success": False,
                "error": f"Download error: {str(e)}"
            }
    
    @staticmethod
    def add_attachment_to_session(session, attachment_data: Dict[str, Any]) -> bool:
        """
        Add attachment to session workflow state.
        
        Args:
            session: ConversationSession object
            attachment_data: Attachment data dictionary
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if "pending_attachments" not in session.workflow_state:
                session.workflow_state["pending_attachments"] = []
            
            session.workflow_state["pending_attachments"].append(attachment_data)
            session.workflow_state["awaiting_attachment_decision"] = True
            
            logger.info(f"Added attachment to session: {attachment_data.get('file_name')}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding attachment to session: {e}")
            return False
    
    @staticmethod
    def approve_pending_attachment(session, attachment_filename: str = None) -> bool:
        """
        Approve a pending attachment for inclusion in RFQ.
        
        Args:
            session: ConversationSession object
            attachment_filename: Specific filename to approve (approves all if None)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"approve_pending_attachment called - filename: {attachment_filename}")
            logger.info(f"Session workflow state keys: {list(session.workflow_state.keys())}")

            pending_attachments = session.workflow_state.get("pending_attachments", [])

            logger.info(f"Found {len(pending_attachments)} pending attachments")
            for i, att in enumerate(pending_attachments):
                logger.info(f"  Attachment {i+1}: {att.get('file_name', 'unknown')} - status: {att.get('status', 'unknown')}")

            if not pending_attachments:
                logger.info("No pending attachments to approve")
                return False
            
            if attachment_filename:
                # Approve specific attachment
                for attachment in pending_attachments:
                    if attachment.get("file_name") == attachment_filename:
                        attachment["status"] = "approved"
                        break
            else:
                # Approve all pending attachments (including those without status set)
                for attachment in pending_attachments:
                    if attachment.get("status") != "rejected":  # Approve anything not explicitly rejected
                        attachment["status"] = "approved"
            
            session.workflow_state["awaiting_attachment_decision"] = False
            
            # Move approved attachments to the RFQ attachments field
            if "extracted_entities" not in session.workflow_state:
                session.workflow_state["extracted_entities"] = [{}]
            
            # Ensure we have at least one entity object
            if not session.workflow_state["extracted_entities"]:
                session.workflow_state["extracted_entities"] = [{}]
            
            if "attachments" not in session.workflow_state["extracted_entities"][0]:
                session.workflow_state["extracted_entities"][0]["attachments"] = []
            
            # Add approved attachments
            approved_count = 0
            for attachment in pending_attachments:
                if attachment.get("status") == "approved":
                    session.workflow_state["extracted_entities"][0]["attachments"].append(attachment)
                    approved_count += 1

            logger.info(f"Approved {approved_count} attachments in session - total attachments now: {len(session.workflow_state['extracted_entities'][0]['attachments'])}")
            return True
            
        except Exception as e:
            logger.error(f"Error approving attachment: {e}")
            return False
    
    @staticmethod
    def reject_pending_attachments(session) -> bool:
        """
        Reject all pending attachments.
        
        Args:
            session: ConversationSession object
            
        Returns:
            True if successful, False otherwise
        """
        try:
            session.workflow_state["pending_attachments"] = []
            session.workflow_state["awaiting_attachment_decision"] = False
            
            logger.info("Rejected all pending attachments")
            return True
            
        except Exception as e:
            logger.error(f"Error rejecting attachments: {e}")
            return False
    
    @staticmethod
    def get_attachment_summary(session) -> Dict[str, Any]:
        """
        Get summary of attachments in session.
        
        Args:
            session: ConversationSession object
            
        Returns:
            Dict with attachment counts and details
        """
        try:
            pending_attachments = session.workflow_state.get("pending_attachments", [])
            
            # Get approved attachments from extracted entities
            approved_attachments = []
            if session.workflow_state.get("extracted_entities"):
                for entity in session.workflow_state["extracted_entities"]:
                    approved_attachments.extend(entity.get("attachments", []))
            
            return {
                "pending_count": len(pending_attachments),
                "approved_count": len(approved_attachments),
                "total_count": len(pending_attachments) + len(approved_attachments),
                "pending_files": [att.get("file_name") for att in pending_attachments],
                "approved_files": [att.get("file_name") for att in approved_attachments],
                "awaiting_decision": session.workflow_state.get("awaiting_attachment_decision", False)
            }
            
        except Exception as e:
            logger.error(f"Error getting attachment summary: {e}")
            return {
                "pending_count": 0,
                "approved_count": 0,
                "total_count": 0,
                "pending_files": [],
                "approved_files": [],
                "awaiting_decision": False
            }
    
    @staticmethod
    def validate_attachment_type(filename: str, mime_type: str) -> Dict[str, Any]:
        """
        Validate if attachment type is supported.
        
        Args:
            filename: Name of the file
            mime_type: MIME type of the file
            
        Returns:
            Dict with validation result
        """
        supported_types = [
            "image/jpeg", "image/jpg", "image/png", "image/gif",
            "application/pdf"
        ]
        
        supported_extensions = [".jpg", ".jpeg", ".png", ".gif", ".pdf"]
        
        # Check MIME type
        if mime_type not in supported_types:
            return {
                "valid": False,
                "error": f"Unsupported file type: {mime_type}. Supported types: JPG, PNG, GIF, PDF"
            }
        
        # Check file extension
        file_ext = filename.lower().split('.')[-1] if '.' in filename else ""
        if f".{file_ext}" not in supported_extensions:
            return {
                "valid": False,
                "error": f"Unsupported file extension: .{file_ext}. Supported: JPG, PNG, GIF, PDF"
            }
        
        return {"valid": True}