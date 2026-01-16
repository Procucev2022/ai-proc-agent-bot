"""
Helper utilities for handling image and document attachments.

Contains methods for downloading, processing, and managing attachments
in the WhatsApp RFQ workflow.
"""

import logging
import aiohttp
import base64
import os
from typing import Dict, Any, Optional
from datetime import datetime
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# Set up WhatsApp payload logger with file handler
whatsapp_payload_logger = logging.getLogger("whatsapp_payload")
whatsapp_payload_logger.setLevel(logging.DEBUG)

# Create whatsapp_logs directory if it doesn't exist
log_dir = "whatsapp_logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Add file handler if not already present
if not whatsapp_payload_logger.handlers:
    log_file = os.path.join(log_dir, f"whatsapp_media_{datetime.now().strftime('%Y-%m-%d')}.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes= 3 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    whatsapp_payload_logger.addHandler(file_handler)


class AttachmentHelpers:
    """Helper methods for attachment processing."""

    # Maximum number of attachments allowed per RFQ
    MAX_ATTACHMENTS_PER_RFQ = 4
    # Maximum file size in bytes (2MB)
    MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2MB

    @staticmethod
    async def download_and_encode_attachment(file_url: str, filename: str = None, mime_type: str = None) -> Dict[str, Any]:
        """
        Download attachment from URL and encode as base64.
        
        Args:
            file_url: URL to download the file from or media ID for ICS API
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
            
            # Fix ICS media download URL construction
            download_url = AttachmentHelpers._construct_media_download_url(file_url)
            
            logger.info(f"Downloading attachment: {filename} from {download_url}")

            # Log to WhatsApp payload file
            whatsapp_payload_logger.info("="*80)
            whatsapp_payload_logger.info("MEDIA DOWNLOAD STARTED")
            whatsapp_payload_logger.info(f"Media ID: {file_url}")
            whatsapp_payload_logger.info(f"MIME Type: {mime_type}")
            whatsapp_payload_logger.info("REQUEST DETAILS:")
            whatsapp_payload_logger.info(f"  Method: GET")
            whatsapp_payload_logger.info(f"  URL: {download_url}")
            whatsapp_payload_logger.info(f"  Headers: {{'wanumber': '917090170855'}}")
            whatsapp_payload_logger.info(f"  Timeout: 30 seconds")

            async with aiohttp.ClientSession() as session:
                # Add required header for ICS API
                from app.config import get_settings
                settings = get_settings()
                headers = {'wanumber': settings.WHATSAPP_FROM_NUMBER}
                async with session.get(download_url, headers=headers, timeout=30) as response:
                    # Log response details for debugging
                    logger.info(f"Media download response - Status: {response.status}, Content-Type: {response.headers.get('Content-Type', 'unknown')}")

                    # Log full response to WhatsApp payload file
                    whatsapp_payload_logger.info(f"Response Status: {response.status}")
                    whatsapp_payload_logger.info(f"Response Content-Type: {response.headers.get('Content-Type', 'unknown')}")
                    whatsapp_payload_logger.debug(f"Response Headers: {dict(response.headers)}")

                    if response.status == 200:
                        file_data = await response.read()
                        file_content_b64 = base64.b64encode(file_data).decode('utf-8')

                        # Log download payload details
                        logger.info(f"Media download payload - Size: {len(file_data)} bytes, MIME: {mime_type}, Filename: {filename}")

                        # Log full payload to WhatsApp file
                        whatsapp_payload_logger.info(f"File Size: {len(file_data)} bytes ({len(file_data)/1024:.2f} KB)")
                        whatsapp_payload_logger.info(f"Base64 Length: {len(file_content_b64)} characters")
                        whatsapp_payload_logger.debug(f"Base64 Preview (first 200 chars): {file_content_b64[:200]}...")
                        whatsapp_payload_logger.debug(f"Full Payload Structure: {{'file_name': '{filename}', 'file_type': '{mime_type}', 'file_size': {len(file_data)}}}")

                        # Validate file size (limit to 1MB)
                        if len(file_data) > AttachmentHelpers.MAX_FILE_SIZE_BYTES:
                            size_in_mb = len(file_data) / (1024 * 1024)
                            logger.warning(f"File size exceeds 1MB limit: {len(file_data)} bytes ({size_in_mb:.2f}MB)")
                            whatsapp_payload_logger.warning(f"File size exceeds 1MB limit: {len(file_data)} bytes ({size_in_mb:.2f}MB)")
                            whatsapp_payload_logger.info("="*80)
                            return {
                                "success": False,
                                "error": f"File size ({size_in_mb:.2f}MB) exceeds 1MB limit. Please upload a smaller file."
                            }

                        logger.info(f"Successfully downloaded and encoded attachment: {filename} ({len(file_data)} bytes)")
                        whatsapp_payload_logger.info("DOWNLOAD SUCCESS")
                        whatsapp_payload_logger.info("="*80)

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
                        # Log error response details
                        error_body = await response.text()
                        logger.error(f"Failed to download attachment: HTTP {response.status}")
                        logger.error(f"Error response body: {error_body[:500]}")

                        # Log full error to WhatsApp payload file
                        whatsapp_payload_logger.error("DOWNLOAD FAILED")
                        whatsapp_payload_logger.error(f"Status Code: {response.status}")
                        whatsapp_payload_logger.error(f"Error Body (first 500 chars): {error_body[:500]}")
                        whatsapp_payload_logger.debug(f"Error Body (full): {error_body}")
                        whatsapp_payload_logger.debug(f"Error Response Headers: {dict(response.headers)}")
                        whatsapp_payload_logger.error(f"Full Error Payload Structure: {{'status': {response.status}, 'url': '{file_url}', 'filename': '{filename}', 'mime_type': '{mime_type}', 'error_body_length': {len(error_body)}}}")
                        whatsapp_payload_logger.info("="*80)

                        return {
                            "success": False,
                            "error": f"Failed to download file: HTTP {response.status}"
                        }
                        
        except aiohttp.ClientTimeout:
            logger.error("Timeout downloading attachment")
            whatsapp_payload_logger.error("DOWNLOAD TIMEOUT")
            whatsapp_payload_logger.error(f"URL: {file_url}")
            whatsapp_payload_logger.error(f"Filename: {filename}")
            whatsapp_payload_logger.error(f"Full Timeout Payload Structure: {{'error': 'timeout', 'url': '{file_url}', 'filename': '{filename}', 'mime_type': '{mime_type}', 'timeout_seconds': 30}}")
            whatsapp_payload_logger.info("="*80)
            return {
                "success": False,
                "error": "Timeout downloading file"
            }
        except Exception as e:
            logger.error(f"Error downloading attachment: {e}")
            whatsapp_payload_logger.error("DOWNLOAD EXCEPTION")
            whatsapp_payload_logger.error(f"Error: {str(e)}")
            whatsapp_payload_logger.error(f"Error Type: {type(e).__name__}")
            whatsapp_payload_logger.error(f"URL: {file_url}")
            whatsapp_payload_logger.error(f"Filename: {filename}")
            whatsapp_payload_logger.error(f"Full Exception Payload Structure: {{'error': '{str(e)}', 'error_type': '{type(e).__name__}', 'url': '{file_url}', 'filename': '{filename}', 'mime_type': '{mime_type}'}}")
            whatsapp_payload_logger.info("="*80)
            return {
                "success": False,
                "error": f"Download error: {str(e)}"
            }
    
    @staticmethod
    def add_attachment_to_session(session, attachment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add attachment to session workflow state.

        Args:
            session: ConversationSession object
            attachment_data: Attachment data dictionary

        Returns:
            Dict with success status and optional error message
        """
        try:
            if "pending_attachments" not in session.workflow_state:
                session.workflow_state["pending_attachments"] = []

            # Count total attachments (pending non-rejected + already approved)
            pending_attachments = session.workflow_state["pending_attachments"]
            # Only count pending attachments that are not rejected
            pending_count = len([att for att in pending_attachments if att.get("status") != "rejected"])

            # Count approved attachments
            approved_count = 0
            if session.workflow_state.get("extracted_entities"):
                for entity in session.workflow_state["extracted_entities"]:
                    if isinstance(entity, dict) and entity.get("attachments"):
                        approved_count += len(entity.get("attachments", []))

            total_count = pending_count + approved_count

            # Check if limit is exceeded
            if total_count >= AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ:
                logger.warning(f"Attachment limit reached: {total_count}/{AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ}")
                return {
                    "success": False,
                    "error": f"Maximum {AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ} attachments allowed per RFQ. You've already added {total_count} file(s)."
                }

            session.workflow_state["pending_attachments"].append(attachment_data)
            session.workflow_state["awaiting_attachment_decision"] = True

            logger.info(f"Added attachment to session: {attachment_data.get('file_name')} ({total_count + 1}/{AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ})")
            return {
                "success": True,
                "count": total_count + 1,
                "max": AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ
            }

        except Exception as e:
            logger.error(f"Error adding attachment to session: {e}")
            return {
                "success": False,
                "error": f"Failed to add attachment: {str(e)}"
            }
    
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
            
            # Add approved attachments (with limit check)
            approved_count = 0
            current_attachments = session.workflow_state["extracted_entities"][0]["attachments"]
            for attachment in pending_attachments:
                if attachment.get("status") == "approved":
                    # Safety check: don't exceed max attachments
                    if len(current_attachments) >= AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ:
                        logger.warning(f"Skipping attachment '{attachment.get('file_name')}' - limit of {AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ} already reached")
                        continue
                    current_attachments.append(attachment)
                    approved_count += 1

            # Clear pending_attachments after moving them to approved
            session.workflow_state["pending_attachments"] = []

            # Also sync attachments to pending RFQ entities so they're included in schema creation
            all_attachments = session.workflow_state["extracted_entities"][0]["attachments"]
            if session.workflow_state.get("pending_rfq"):
                session.workflow_state["pending_rfq"]["entities"]["attachments"] = all_attachments
                logger.info(f"Synced attachments to pending_rfq.entities")
            if session.workflow_state.get("pending_optional_rfq"):
                session.workflow_state["pending_optional_rfq"]["entities"]["attachments"] = all_attachments
                logger.info(f"Synced attachments to pending_optional_rfq.entities")
            if session.workflow_state.get("pending_combined_rfq"):
                session.workflow_state["pending_combined_rfq"]["combined_schema"]["attachments"] = all_attachments
                logger.info(f"Synced attachments to pending_combined_rfq.combined_schema")
            if session.workflow_state.get("pending_optional_combined_rfq"):
                session.workflow_state["pending_optional_combined_rfq"]["combined_schema"]["attachments"] = all_attachments
                logger.info(f"Synced attachments to pending_optional_combined_rfq.combined_schema")

            logger.info(f"Approved {approved_count} attachments in session - total attachments now: {len(all_attachments)}")
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
            "application/pdf",
            # Excel file types - supported as attachments during optional phase
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
            "application/vnd.ms-excel",  # xls
            "application/vnd.ms-excel.sheet.macroEnabled.12",  # xlsm
            # Word document types
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
            "application/msword",  # doc
            # AutoCAD file types (DWG and DXF)
            "application/acad",  # dwg
            "application/x-acad",  # dwg
            "image/x-dwg",  # dwg
            "image/vnd.dwg",  # dwg
            "application/dxf",  # dxf
            "image/vnd.dxf",  # dxf
            "image/x-dxf",  # dxf
            "application/octet-stream",  # generic binary (often used for CAD files)
        ]

        supported_extensions = [".jpg", ".jpeg", ".png", ".gif", ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".dwg", ".dxf"]
        
        # Check MIME type
        if mime_type not in supported_types:
            return {
                "valid": False,
                "error": f"Unsupported file type: {mime_type}. Supported types: JPG, PNG, GIF, PDF, Excel, Word, AutoCAD"
            }

        # Check file extension
        file_ext = filename.lower().split('.')[-1] if '.' in filename else ""
        if f".{file_ext}" not in supported_extensions:
            return {
                "valid": False,
                "error": f"Unsupported file extension: .{file_ext}. Supported: JPG, PNG, GIF, PDF, XLSX, XLS, DOCX, DOC, DWG, DXF"
            }
        
        return {"valid": True}
    
    @staticmethod
    def _construct_media_download_url(file_url_or_id: str) -> str:
        """
        Construct proper ICS media download URL.
        
        Args:
            file_url_or_id: Either a full URL or just the media ID
            
        Returns:
            Properly constructed ICS media download URL
        """
        from app.config import get_settings
        settings = get_settings()
        
        # If it's already a full URL, return as is
        if file_url_or_id.startswith('http'):
            # Check if it's the old incorrect URL format and fix it
            if 'media.sendmsg.in/wamessage/media/' in file_url_or_id:
                # Extract media ID from old URL format
                media_id = file_url_or_id.split('/')[-1]
                return f"{settings.WHATSAPP_MEDIA_DOWNLOAD_URL}/{media_id}"
            return file_url_or_id
        
        # If it's just a media ID, construct the proper ICS URL
        return f"{settings.WHATSAPP_MEDIA_DOWNLOAD_URL}/{file_url_or_id}"