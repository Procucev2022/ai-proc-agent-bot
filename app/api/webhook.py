"""
WhatsApp webhook endpoints for receiving and processing messages.

This module handles incoming WhatsApp messages through webhook endpoints.
It serves as the entry point for all user interactions, routing messages
to the appropriate chat service for processing and orchestration.

Key responsibilities:
- Receive WhatsApp webhook POST requests
- Validate webhook signatures and message formats
- Extract message content and user information
- Route messages to ChatService for processing
- Handle webhook verification for WhatsApp setup
- Return appropriate HTTP responses for webhook requirements
"""

from fastapi import APIRouter, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse
from urllib.parse import unquote
import logging
import os
from typing import Dict, Any, Optional
import json
from slowapi import Limiter
from slowapi.util import get_remote_address
from logging.handlers import RotatingFileHandler
from datetime import datetime

from app.config import get_settings
from app.services.chat_service import ChatService
from app.services.cancel_service import CancelService
from app.services.session_management_service import SessionManagementService
from app.services.message_queue_service import MessageQueueService 

router = APIRouter()
logger = logging.getLogger(__name__)
# chat_service = ChatService()
message_queue_service = MessageQueueService()  # Instantiate message_queue service

# Set up WhatsApp webhook payload logger
webhook_payload_logger = logging.getLogger("whatsapp_webhook")
webhook_payload_logger.setLevel(logging.DEBUG)

# Create whatsapp_logs directory if it doesn't exist
log_dir = "whatsapp_logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Add file handler if not already present
if not webhook_payload_logger.handlers:
    log_file = os.path.join(log_dir, f"whatsapp_webhook_{datetime.now().strftime('%Y-%m-%d')}.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    webhook_payload_logger.addHandler(file_handler)

# Initialize rate limiter for webhook endpoints
settings = get_settings()
limiter = Limiter(key_func=get_remote_address)


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """
    Verify WhatsApp webhook during setup process.
    
    WhatsApp requires webhook verification with challenge response
    to confirm the endpoint is valid and controlled by the developer.
    """
    logger.info(f"Webhook verification attempt: mode={hub_mode}, token={hub_verify_token}")
    
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook verification successful")
        return PlainTextResponse(content=hub_challenge)
    
    logger.warning("Webhook verification failed")
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/whatsapp")
@limiter.limit("60/minute")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Process incoming WhatsApp messages.
    
    This endpoint receives webhook calls from WhatsApp containing
    user messages, processes them through the chat service, and
    returns appropriate responses.
    """
    try:
        body = await request.body()
        logger.debug(f"Received webhook payload: {body.decode()}")
        
        # Parse webhook data
        webhook_data = await parse_webhook_data(request)
        
        if not webhook_data:
            logger.warning("No processable data in webhook")
            return JSONResponse(content={"status": "ok"})
        
        # Enqueue message to message queue service instead of direct processing
        background_tasks.add_task(enqueue_message_async, webhook_data)
        
        # Return success immediately
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        # Handle critical webhook processing errors with automatic cancellation
        logger.error(f"Critical error processing webhook: {e}", exc_info=True)

        # Try to extract user phone for error handling
        user_phone = None
        try:
            body = await request.body()
            webhook_data = await parse_webhook_data(request)
            if webhook_data:
                user_phone = webhook_data.get("from")
        except Exception as parse_error:
            logger.error(f"Failed to parse webhook data for error handling: {parse_error}")

        # Clear workflow state and notify user if we have their phone number
        if user_phone:
            try:
                await handle_technical_error_with_cancel(
                    user_phone=user_phone,
                    error_message=f"Webhook processing error: {str(e)}",
                    error_type="Critical Webhook Error"
                )
            except Exception as cancel_error:
                # Don't let error handling failure break webhook response
                logger.error(f"Failed to handle technical error with cancel: {cancel_error}")

        # Return 200 to acknowledge webhook receipt (prevents retries)
        # The error has been logged and user notified
        return JSONResponse(content={"status": "error_handled", "message": "Error logged and user notified"}, status_code=200)


@router.get("/delivery")
@router.post("/delivery")
async def handle_delivery_callback(request: Request):
    """
    Handle delivery status callbacks from WhatsApp based on ICS documentation.

    Expected ICS format:
    ?qStatus=STATUS&qMobile=MOBILENO&qMsgRef=MESSAGEID&qDTime=DATETIME&SMSMSGID=SMSMSGID&SENDERID=SENDERID&NOTES=NOTES

    Sample: ?qStatus=read&qMobile=919036149941&qMsgRef=80890044684334105532272789261449227204&qDTime=2025-09-13%2013%3A53%3A25.980895&SMSMSGID=test13sep&NOTES=NA
    """
    try:
        # Parse query parameters from URL
        query_params = dict(request.query_params)
        logger.info(f"ICS delivery callback received: {query_params}")

        # Extract ICS delivery callback fields
        status = query_params.get("qStatus")
        mobile = query_params.get("qMobile")
        msg_ref = query_params.get("qMsgRef")
        date_time = query_params.get("qDTime")
        sms_msg_id = query_params.get("SMSMSGID")
        sender_id = query_params.get("SENDERID")
        notes = query_params.get("NOTES")

        if status and mobile and msg_ref:
            logger.info(f"ICS delivery status - Message {msg_ref} to {mobile}: {status} at {date_time}")
            if notes and notes != "NA":
                logger.info(f"Delivery notes: {notes}")

            # Here you could update message status in database
            # Example: await update_message_status(msg_ref, status, date_time)
        else:
            logger.warning(f"Missing required delivery callback fields: status={status}, mobile={mobile}, msg_ref={msg_ref}")

        return JSONResponse(content={"status": "ok"})

    except Exception as e:
        logger.error(f"Error processing ICS delivery callback: {e}")
        return JSONResponse(content={"status": "error"}, status_code=500)


async def parse_webhook_data(request: Request) -> Optional[Dict[str, Any]]:
    """
    Parse incoming webhook data based on content type.
    
    Handles both URL-encoded callbacks and JSON session messages.
    """
    content_type = request.headers.get("content-type", "")
    
    if "application/x-www-form-urlencoded" in content_type:
        # Handle URL-encoded webhook (user responses)
        form_data = await request.form()
        return parse_user_response_callback(dict(form_data))
    
    elif "application/json" in content_type:
        # Handle JSON webhook (if any)
        json_data = await request.json()
        return parse_json_webhook(json_data)
    
    else:
        # Try to parse as query parameters
        query_params = dict(request.query_params)
        if query_params:
            return parse_user_response_callback(query_params)
    
    return None


def parse_user_response_callback(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse user response callback from WhatsApp based on ICS documentation.

    Expected format from ICS:
    replytype, customernumber, replymessage, timestamp, wabanumber, mid, smsgid

    Sample: ?replytype=TEXT&customernumber=919036149941&replymessage=I+want+laptop&timestamp=2025-09-13%2013%3A54%3A22.125300&wabanumber=917090170855&mid=80890044684334105532272789261449227204&smsgid=NA
    """
    try:
        reply_type = data.get("replytype")
        customer_number = data.get("customernumber")
        reply_message = data.get("replymessage")
        timestamp = data.get("timestamp")
        waba_number = data.get("wabanumber")
        mid = data.get("mid")
        smsgid = data.get("smsgid")

        # Log received data for debugging
        logger.info(f"Parsing ICS webhook - Type: {reply_type}, From: {customer_number}, Message: {reply_message[:50] if reply_message else 'None'}...")

        # Check essential fields - only customer_number and reply_message are mandatory
        if not customer_number:
            logger.warning("Missing customer number in ICS webhook data")
            return None

        if not reply_message:
            logger.warning("Missing reply message in ICS webhook data")
            return None

        # URL decode the reply message (+ signs should become spaces)
        decoded_message = unquote(reply_message.replace('+', ' '))

        # Handle different message types based on ICS documentation
        message_data = {
            "type": reply_type.lower() if reply_type else "text",
            "from": customer_number,
            "to": waba_number,
            "timestamp": timestamp,
            "content": decoded_message,
            "message_id": mid,
            "sms_id": smsgid
        }

        # Parse JSON content for media/interactive messages per ICS documentation
        if reply_type and reply_type.upper() in ["IMAGE", "VIDEO", "DOCUMENT", "INTERACTIVE"]:
            try:
                # ICS sends JSON data URL-encoded for media messages
                message_data["content"] = json.loads(decoded_message)
                logger.info(f"Successfully parsed {reply_type.upper()} message content as JSON")

                # Log media message payload to separate file
                webhook_payload_logger.info("="*80)
                webhook_payload_logger.info(f"MEDIA WEBHOOK RECEIVED - Type: {reply_type.upper()}")
                webhook_payload_logger.info(f"From: {customer_number}")
                webhook_payload_logger.info(f"Timestamp: {timestamp}")
                webhook_payload_logger.info(f"Message ID: {mid}")
                webhook_payload_logger.debug(f"Full Content: {message_data['content']}")

                # Log file details for document/image
                if reply_type.upper() in ["IMAGE", "DOCUMENT"]:
                    content_obj = message_data['content']
                    media_key = "image" if reply_type.upper() == "IMAGE" else "document"
                    if isinstance(content_obj, dict) and media_key in content_obj:
                        media_info = content_obj[media_key]
                        webhook_payload_logger.info(f"File URL: {media_info.get('link', 'N/A')}")
                        webhook_payload_logger.info(f"Filename: {media_info.get('filename', 'N/A')}")
                        webhook_payload_logger.info(f"MIME Type: {media_info.get('mime_type', 'N/A')}")

                webhook_payload_logger.info("="*80)

            except json.JSONDecodeError:
                # Keep as string if JSON parsing fails
                logger.info(f"Keeping {reply_type.upper()} message content as string")
                webhook_payload_logger.warning(f"Failed to parse {reply_type.upper()} content as JSON")
                pass

        logger.info(f"Successfully parsed ICS webhook: Type={message_data['type']}, From={message_data['from']}")
        return message_data

    except Exception as e:
        logger.error(f"Error parsing ICS user response callback: {e}")
        return None


def parse_json_webhook(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse JSON webhook data if applicable.
    """
    # Add JSON webhook parsing logic if needed
    return data


async def enqueue_message_async(webhook_data: Dict[str, Any]):
    """
    Enqueue incoming message to the message queue service.
    
    This replaces direct processing and allows batching of messages.
    """
    from_number = None
    try:
        logger.info(f"Enqueueing message: {webhook_data}")

        # Enqueue the message - the service will handle batching and processing
        await message_queue_service.enqueue_message(webhook_data)

    except Exception as e:
        logger.error(f"Error enqueueing message: {e}", exc_info=True)

        # Extract user phone for error handling
        from_number = webhook_data.get("from")
        
        # Clear workflow state and notify user of technical error
        if from_number:
            try:
                await handle_technical_error_with_cancel(
                    user_phone=from_number,
                    error_message=f"Message enqueueing error: {str(e)}",
                    error_type="Message Queue Error"
                )
            except Exception as cancel_error:
                logger.error(f"Failed to handle technical error in async enqueueing: {cancel_error}")

# async def process_document_message(webhook_data: Dict[str, Any]):
#     """
#     Process document message for Excel file uploads.

#     Handles Excel file validation, processing, and routing to chat service.
#     """
#     try:
#         from_number = webhook_data.get("from")
#         content = webhook_data.get("content")

#         if not isinstance(content, dict):
#             logger.warning("Document message content is not a dictionary")
#             return

#         # Extract document information
#         document_info = content.get("document", {})
#         if not document_info:
#             logger.warning("No document information found in message")
#             return

#         file_url = document_info.get("link")
#         filename = document_info.get("filename", "")

#         if not file_url:
#             logger.warning("No file URL found in document message")
#             return

#         logger.info(f"Processing document upload: {filename} from {from_number}")

#         # Check if it's an Excel file
#         excel_extensions = ['.xlsx', '.xls', '.xlsm']
#         is_excel = any(filename.lower().endswith(ext) for ext in excel_extensions)

#         if is_excel:
#             # Process as Excel file through chat service
#             await chat_service.process_message(
#                 user_phone=from_number,
#                 message_content=content,
#                 message_type="excel_upload"
#             )
#         else:
#             # Handle non-Excel documents
#             await chat_service.process_message(
#                 user_phone=from_number,
#                 message_content=f"Received document: {filename}",
#                 message_type="document"
#             )

#     except Exception as e:
#         logger.error(f"Error processing document message: {e}")


async def handle_technical_error_with_cancel(user_phone: str, error_message: str, error_type: str = "Technical Error"):
    """
    Handle technical errors by clearing workflow state and notifying user.

    This function:
    1. Clears the user's workflow state using cancel service
    2. Sends a user-friendly error message
    3. Logs the error details for debugging

    Args:
        user_phone: User's phone number
        error_message: Technical error message for logging
        error_type: Type of error for categorization
    """
    try:
        logger.error(f"{error_type} for user {user_phone}: {error_message}")

        # Initialize services for error handling
        # Use direct WhatsAppService since this is an error scenario outside normal batch flow
        from app.services.whatsapp_service import WhatsAppService
        from app.services.chat_summary_service import ChatSummaryService
        from app.services.daily_summary_service import DailySummaryService
        from app.database import DatabaseManager
        
        whatsapp_service = WhatsAppService()
        db_manager = DatabaseManager()
        chat_summary_service = ChatSummaryService()
        daily_summary_service = DailySummaryService()
        
        session_manager = SessionManagementService(
            db_manager, whatsapp_service,
            chat_summary_service, daily_summary_service
        )
        cancel_service = CancelService(
            whatsapp_service=whatsapp_service,
            session_manager=session_manager
        )

        # Get user's current session
        session = await session_manager.get_conversation_context(user_phone)

        if session:
            # Clear workflow state using cancel service internal method
            logger.info(f"Clearing workflow state for user {user_phone} due to technical error")
            await cancel_service._clear_workflow_state(session)

        # Send user-friendly error message
        error_notification = (
            "Due to a technical error, your request could not be processed. "
            "Your current session has been cleared. Please try again later or contact support if the issue persists."
        )

        await whatsapp_service.send_message(user_phone, error_notification)
        logger.info(f"Technical error notification sent to user {user_phone}")

        # Also send to technical failure handler for admin notification
        from app.utils.technical_failure_handler import handle_technical_failure
        await handle_technical_failure(
            user_phone=user_phone,
            error_message=error_message,
            error_type=error_type
        )

    except Exception as e:
        # Don't let error handling fail the webhook response
        logger.error(f"Error in handle_technical_error_with_cancel: {e}")


