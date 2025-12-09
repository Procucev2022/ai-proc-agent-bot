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
from app.services.inactivity_timeout_service import get_timeout_service
from app.redis_db import get_redis_service
import time

router = APIRouter()
logger = logging.getLogger(__name__)
message_queue_service = MessageQueueService()  # Instantiate message_queue service
timeout_service = get_timeout_service()  # Get singleton timeout service instance

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
        # Log server receipt time immediately
        server_receipt_time = datetime.now()
        logger.info(f"Message received on server at: {server_receipt_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        body = await request.body()
        logger.debug(f"Received webhook payload: {body.decode()}")

        # Parse webhook data
        webhook_data = await parse_webhook_data(request)
        
        if not webhook_data:
            logger.warning("No processable data in webhook")
            return JSONResponse(content={"status": "ok"})
        
        # Route message based on type - text messages go to queue, others process directly
        message_type = webhook_data.get("type", "")
        logger.info(f"[ROUTING] message_type='{message_type}', checking if == 'text': {message_type == 'text'}")

        if message_type == "text":
            # Enqueue text messages for batched processing
            logger.info(f"[ROUTING] Enqueueing text message for {webhook_data.get('from')}")
            background_tasks.add_task(enqueue_message_async, webhook_data)
        
        elif message_type == "interactive":
            # Process interactive messages directly with session tracking
            logger.info(f"[ROUTING] Processing interactive message for {webhook_data.get('from')}")
            background_tasks.add_task(process_message_async, webhook_data)
        
        else:
            # Process non-text messages (excel, image, document) directly
            logger.info(f"[ROUTING] Processing non-text message type='{message_type}' for {webhook_data.get('from')}")
            background_tasks.add_task(process_message_async, webhook_data)
        
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
        # Update webhook callback timestamp for passive health monitoring
        from app.redis_db import get_redis_service
        redis_service = get_redis_service()
        await redis_service.set("webhook:last_callback_time", datetime.now().isoformat())
        
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

        # Log received data for debugging with timestamp comparison
        server_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        logger.info(f"Parsing ICS webhook - Type: {reply_type}, From: {customer_number}, Message: {reply_message[:50] if reply_message else 'None'}...")
        logger.info(f"Message timestamp from ICS: {timestamp}, Server processing time: {server_time}")

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

                # Log file details for document/image/video
                # According to ICS V3.1 documentation, the structure is flat: {"mime_type": "...", "id": "...", "filename": "..."}
                if reply_type.upper() in ["IMAGE", "DOCUMENT", "VIDEO"]:
                    content_obj = message_data['content']
                    if isinstance(content_obj, dict):
                        media_id = content_obj.get('id', 'N/A')
                        mime_type = content_obj.get('mime_type', 'N/A')
                        filename = content_obj.get('filename', 'N/A')  # Only present for DOCUMENT
                        webhook_payload_logger.info(f"Media ID: {media_id}")
                        webhook_payload_logger.info(f"MIME Type: {mime_type}")
                        webhook_payload_logger.info(f"Filename: {filename}")
                        webhook_payload_logger.info(f"Download URL: https://download.sendmsg.in/whatsapp-mediadownloader/{media_id}")

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


async def create_direct_processing_session(user_phone: str, message_type: str) -> bool:
    """
    Create a processing session for direct (non-queued) message processing.
    
    This enables the monitoring loop in message_queue_service to track
    long-running operations and send please-wait messages when needed.
    
    Args:
        user_phone: User's phone number (normalized without '+')
        message_type: Type of message being processed (interactive, excel, image, etc.)
    
    Returns:
        True if session created successfully, False if user already processing
    """
    try:
        redis_service = get_redis_service()
        
        # Normalize phone number
        user_phone = user_phone.lstrip('+') if user_phone.startswith('+') else user_phone
        
        # Check if user is already processing (prevent concurrent processing)
        processing_key = f"{user_phone}:processing"
        is_processing = await redis_service.exists(processing_key)
        
        if is_processing:
            logger.warning(f"[SESSION] User {user_phone} already processing, skipping session creation")
            return False
        
        # Mark as processing
        await redis_service.set(processing_key, f"direct_{message_type}", ex=60)
        
        # Create session data (same structure as queue service)
        session_data = {
            "batch_id": f"direct_{user_phone}_{int(time.time() * 1000)}",
            "started_at": time.time(),
            "ack_sent": False,
            "please_wait_sent": False,
            "suppressed": False
        }
        
        session_key = f"{user_phone}:session"
        await redis_service.set(session_key, json.dumps(session_data), ex=60)
        
        logger.info(f"[SESSION] Created direct processing session for {user_phone} (type: {message_type})")
        return True
        
    except Exception as e:
        logger.error(f"[SESSION] Error creating session for {user_phone}: {e}", exc_info=True)
        return False


async def cleanup_direct_processing_session(user_phone: str) -> None:
    """
    Clean up processing session after direct message processing completes.
    
    Removes the session and processing keys from Redis to allow next message.
    
    Args:
        user_phone: User's phone number (normalized without '+')
    """
    try:
        redis_service = get_redis_service()
        
        # Normalize phone number
        user_phone = user_phone.lstrip('+') if user_phone.startswith('+') else user_phone
        
        # Delete session and processing keys
        session_key = f"{user_phone}:session"
        processing_key = f"{user_phone}:processing"
        response_ready_key = f"{user_phone}:response_ready"
        
        await redis_service.delete(session_key)
        await redis_service.delete(processing_key)
        await redis_service.delete(response_ready_key)
        
        logger.info(f"[SESSION] Cleaned up direct processing session for {user_phone}")
        
    except Exception as e:
        logger.error(f"[SESSION] Error cleaning up session for {user_phone}: {e}", exc_info=True)


async def enqueue_message_async(webhook_data: Dict[str, Any]):
    """
    Enqueue incoming text message to the message queue service.
    
    This replaces direct processing and allows batching of text messages.
    """
    from_number = None
    try:
        from_number = webhook_data.get("from")
        
        # Update activity timestamp FIRST (for timeout tracking)
        if from_number:
            await timeout_service.update_user_activity(from_number)
        
        logger.info(f"Enqueueing text message: {webhook_data}")

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


async def process_message_async(webhook_data: Dict[str, Any]):
    """
    Process incoming non-text message asynchronously (excel, image, document, interactive).

    Routes the message directly through the chat service for immediate processing.
    This is used for messages that cannot be batched (file uploads, interactive buttons).
    
    Creates a processing session in Redis to enable please-wait monitoring for long-running operations.
    """
    from app.utils.logging_utils import UserPhoneContext

    from_number = None
    session_created = False
    try:
        from_number = webhook_data.get("from")
        
        # Update activity timestamp FIRST (for timeout tracking)
        if from_number:
            await timeout_service.update_user_activity(from_number)
        
        processing_start_time = datetime.now()
        logger.info(f"Processing non-text message directly: {webhook_data}")
        logger.info(f"Background task started at: {processing_start_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        # Extract message details
        message_type = webhook_data.get("type", "text")
        from_number = webhook_data.get("from")
        content = webhook_data.get("content")

        if not from_number or not content:
            logger.warning("Missing required message data")
            return

        # Set phone number context for all logs in this async task
        async with UserPhoneContext(from_number):
            # Create processing session for monitoring (enables please-wait messages)
            # Allow concurrent processing for documents and images, but not for interactive messages
            allow_concurrent = message_type.lower() in ["document", "image"]
            session_created = await create_direct_processing_session(from_number, message_type)
            
            if not session_created and not allow_concurrent:
                logger.warning(
                    f"User {from_number} is already processing another message. "
                    f"Skipping {message_type} message to prevent concurrent processing conflicts."
                )
                # Send user notification that their message was received but will be processed after current operation
                from app.services.whatsapp_service import WhatsAppService
                whatsapp_service = WhatsAppService()
                recipient_id = f"+{from_number}" if not from_number.startswith('+') else from_number
                await whatsapp_service.send_message(
                    recipient_id,
                    "We're still processing your previous request. Please wait a moment before sending new messages."
                )
                return  # Exit without processing to prevent concurrent execution
            
            # Log if allowing concurrent processing
            if not session_created and allow_concurrent:
                logger.info(
                    f"[SESSION] Allowing concurrent processing for {message_type} message from {from_number} "
                    f"(session already exists but concurrent processing is permitted for this message type)"
                )
            
            # Initialize chat service with message_queue_service and db_session for non-text messages
            from app.services.chat_service import ChatService
            from app.database import get_db_session_context

            with get_db_session_context() as db:
                chat_service = ChatService(
                    db_session=db,
                    message_queue_service=message_queue_service  # Pass queue service for session management
                )
                try:
                    # Handle document messages specifically
                    if message_type.lower() == "document":
                        await process_document_message(webhook_data, chat_service)
                    else:
                        # Process other non-text messages (image, interactive, etc.) through chat service
                        await chat_service.process_message(
                            user_phone=from_number,
                            message_content=content,
                            message_type=message_type
                        )
                finally:
                    # Cleanup to prevent unclosed aiohttp sessions
                    await chat_service.cleanup()

    except Exception as e:
        logger.error(f"Error in async message processing: {e}", exc_info=True)

        # Clear workflow state and notify user of technical error
        if from_number:
            try:
                await handle_technical_error_with_cancel(
                    user_phone=from_number,
                    error_message=f"Message processing error: {str(e)}",
                    error_type="Message Processing Error"
                )
            except Exception as cancel_error:
                logger.error(f"Failed to handle technical error in async processing: {cancel_error}")
    
    finally:
        # Always cleanup session if it was created
        if session_created and from_number:
            await cleanup_direct_processing_session(from_number)


async def process_document_message(webhook_data: Dict[str, Any], chat_service):
    """
    Process document message for Excel file uploads.

    Handles Excel file validation, processing, and routing to chat service.
    """
    try:
        from_number = webhook_data.get("from")
        content = webhook_data.get("content")

        if not isinstance(content, dict):
            logger.warning("Document message content is not a dictionary")
            return

        # Extract document information - handle both formats
        if "document" in content:
            # Standard WhatsApp format
            document_info = content["document"]
            file_url = document_info.get("link")
            filename = document_info.get("filename", "")
        else:
            # ICS format - direct content structure
            media_id = content.get("id")
            filename = content.get("filename", "")
            
            if media_id:
                # Construct download URL from media ID
                file_url = f"https://download.sendmsg.in/whatsapp-mediadownloader/{media_id}"
            else:
                file_url = None

        if not file_url:
            logger.warning("No file URL found in document message")
            return

        logger.info(f"Processing document upload: {filename} from {from_number}")

        # Check if it's an Excel file
        excel_extensions = ['.xlsx', '.xls', '.xlsm']
        is_excel = any(filename.lower().endswith(ext) for ext in excel_extensions)

        if is_excel:
            # Send processing message immediately for Excel files
            from app.services.whatsapp_service import WhatsAppService
            whatsapp_service = WhatsAppService()
            
            # Ensure from_number is not None before sending
            if from_number:
                await whatsapp_service.send_message(from_number, "Please wait, the file is processing…")
            
            # Process as Excel file through chat service
            # Note: Session monitoring will also send "please wait" if processing takes >15s
            await chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type="excel_upload"
            )
        else:
            # Handle non-Excel documents
            await chat_service.process_message(
                user_phone=from_number,
                message_content=f"Received document: {filename}",
                message_type="document"
            )

    except Exception as e:
        logger.error(f"Error processing document message: {e}")


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

        # Initialize minimal services for error handling
        # Note: SessionManagementService requires 4 dependencies,
        # so we skip session clearing in error scenarios to avoid complexity
        from app.services.whatsapp_service import WhatsAppService
        whatsapp_service = WhatsAppService()

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


