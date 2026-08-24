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
from starlette.requests import ClientDisconnect
from urllib.parse import unquote
import asyncio
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
from app.services.whatsapp_service import reply_sent_key
from app.redis_db import get_redis_service
from app.utils.logging_utils import UserPhoneContext
from app.utils.turn_trace import (
    finish_turn,
    resume_turn,
    stage,
    stamp_turn,
    start_turn,
)
import time

router = APIRouter()
logger = logging.getLogger(__name__)
message_queue_service = MessageQueueService()  # Instantiate message_queue service
timeout_service = get_timeout_service()  # Get singleton timeout service instance

# Set up WhatsApp webhook payload logger
webhook_payload_logger = logging.getLogger("whatsapp_webhook")
webhook_payload_logger.setLevel(logging.DEBUG)

# Create logs/whatsapp directory if it doesn't exist
log_dir = os.path.join("logs", "whatsapp")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Add file handler if not already present
if not webhook_payload_logger.handlers:
    log_file = os.path.join(log_dir, f"webhook_{datetime.now().strftime('%Y-%m-%d')}.log")
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
    webhook_data = None
    try:
        # Log server receipt time immediately
        server_receipt_time = datetime.now()
        t0 = time.time()
        logger.info(f"Message received on server at: {server_receipt_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        body = await request.body()
        body_str = body.decode(errors='replace')
        logger.info(f"[WEBHOOK_IN] Payload received ({len(body)} bytes): {body_str}")

        # Parse webhook data
        parse_start = time.time()
        webhook_data = await parse_webhook_data(request)
        parse_dur = time.time() - parse_start
        
        if not webhook_data:
            logger.warning(f"[WEBHOOK_IN] No processable data in webhook (parsed in {parse_dur:.3f}s)")
            return JSONResponse(content={"status": "ok"})
        
        # Route message based on type - text messages go to queue, others process directly
        message_type = webhook_data.get("type", "")
        from_phone = webhook_data.get("from", "unknown")
        total_recv_time = time.time() - t0

        # Open the turn here, at the first point where the sender is known, and
        # stamp its id into the payload. The id then travels with the message
        # through Redis into whichever worker answers it, so a single grep for
        # the turn id returns the whole story of one reply.
        trace = start_turn(
            from_phone,
            source=f"webhook:{message_type or 'unknown'}",
            gateway_timestamp=webhook_data.get("timestamp"),
            message_preview=str(webhook_data.get("content", "")),
        )
        trace.record("webhook_parse", parse_dur)
        trace.annotate(
            msg_id=webhook_data.get("message_id"),
            bytes=len(body),
        )
        turn_id = stamp_turn(webhook_data, trace)

        if message_type == "text":
            logger.info(f"[ROUTING] [TURN:{turn_id}] Enqueueing text message for {from_phone} (msg_id={webhook_data.get('message_id')}, parse_time={parse_dur:.3f}s, total_recv_time={total_recv_time:.3f}s)")
            background_tasks.add_task(enqueue_message_async, webhook_data)
        
        elif message_type == "interactive":
            logger.info(f"[ROUTING] [TURN:{turn_id}] Processing interactive message for {from_phone} (msg_id={webhook_data.get('message_id')}, parse_time={parse_dur:.3f}s, total_recv_time={total_recv_time:.3f}s)")
            background_tasks.add_task(process_message_async, webhook_data)
        
        else:
            logger.info(f"[ROUTING] [TURN:{turn_id}] Processing non-text message type='{message_type}' for {from_phone} (msg_id={webhook_data.get('message_id')}, parse_time={parse_dur:.3f}s, total_recv_time={total_recv_time:.3f}s)")
            background_tasks.add_task(process_message_async, webhook_data)
        
        # Return success immediately. The ACK is measured separately from the
        # reply: an ACK slower than a few milliseconds means the event loop was
        # blocked, which is a different fault from a slow reply.
        logger.info(
            f"[WEBHOOK_ACK] [TURN:{turn_id}] Acknowledged in {(time.time() - t0) * 1000:.1f}ms "
            f"for {from_phone} (handed to background task, reply follows)"
        )
        return JSONResponse(content={"status": "ok"})

    except ClientDisconnect:
        # The sender hung up before the body arrived. There is no message to process and
        # no user to notify, so this is not an application error - it was previously
        # logged as a critical failure with an empty message and then retried against the
        # same dead stream, producing two useless error lines per occurrence.
        logger.info("Webhook client disconnected before the request body was received")
        return JSONResponse(content={"status": "ok"})

    except Exception as e:
        # Handle critical webhook processing errors with automatic cancellation.
        # Include the exception type: several exceptions here carry an empty str(), which
        # left the log line with nothing after the colon.
        logger.error(f"Critical error processing webhook: {type(e).__name__}: {e}", exc_info=True)

        # Extract user phone from already parsed webhook_data or fallback parsing
        user_phone = webhook_data.get("from") if isinstance(webhook_data, dict) else None
        if not user_phone:
            try:
                fallback_data = await parse_webhook_data(request)
                if fallback_data:
                    user_phone = fallback_data.get("from")
            except Exception as parse_error:
                logger.error(
                    "Failed to parse webhook data for error handling: "
                    f"{type(parse_error).__name__}: {parse_error}"
                )

        if not user_phone:
            try:
                query_params = dict(request.query_params)
                user_phone = query_params.get("customernumber") or query_params.get("from")
            except Exception:
                pass

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
        
        # Parse query parameters or body data
        query_params = dict(request.query_params)
        
        # Fallback to form data or json if empty query params
        if not query_params:
            try:
                form_data = await request.form()
                # FormData is a multidict, not a Mapping, so build the dict from
                # its items rather than relying on dict(mapping).
                query_params = {key: value for key, value in form_data.items()}
                if not query_params:
                    json_data = await request.json()
                    if isinstance(json_data, dict):
                        query_params = json_data
            except Exception:
                try:
                    json_data = await request.json()
                    if isinstance(json_data, dict):
                        query_params = json_data
                except Exception:
                    pass

        # Extract ICS delivery callback fields
        status = query_params.get("qStatus") or query_params.get("status")
        mobile = query_params.get("qMobile") or query_params.get("mobile")
        msg_ref = query_params.get("qMsgRef") or query_params.get("msg_ref") or query_params.get("mid")
        date_time = query_params.get("qDTime") or query_params.get("timestamp")
        notes = query_params.get("NOTES") or query_params.get("notes")

        if status and mobile and msg_ref:
            logger.debug(f"ICS delivery callback - Message {msg_ref[:20]}... to {mobile}: {status} at {date_time}")
        else:
            logger.debug(f"Delivery callback received: status={status}, mobile={mobile}, msg_ref={msg_ref}")

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
        # FormData is a multidict, not a Mapping, so build the dict from its items.
        return parse_user_response_callback({key: value for key, value in form_data.items()})
    
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

        # Log received data for debugging with timestamp comparison (DEBUG level to reduce noise)
        server_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        logger.debug(f"Parsing ICS webhook - Type: {reply_type}, From: {customer_number}, Message: {reply_message[:50] if reply_message else 'None'}...")
        logger.debug(f"Message timestamp from ICS: {timestamp}, Server processing time: {server_time}")

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

        logger.debug(f"Successfully parsed ICS webhook: Type={message_data['type']}, From={message_data['from']}")
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
            "please_wait_sent_count": 0,
            "please_wait_last_sent": 0.0,
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

        # Remove interval dedupe markers used by monitor loop.
        await redis_service.delete_pattern(f"{user_phone}:please_wait:interval:*")
        
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
        # Starlette runs background tasks in a context copied from the request,
        # so re-attach the turn explicitly rather than relying on that copy.
        trace = resume_turn(webhook_data, source="enqueue", user_phone=from_number or "")

        # Bind the phone number for the whole hand-off. Without this the phone
        # column of every log line on the text path reads 'N/A', because this
        # task runs outside the request that knew who the sender was.
        async with UserPhoneContext(from_number or "unknown"):
            # Update activity timestamp FIRST (for timeout tracking)
            if from_number:
                with stage("timeout_activity_update"):
                    await timeout_service.update_user_activity(from_number)

                # Set pending_reply flag to track that user is waiting for a response
                redis_service = get_redis_service()
                settings = get_settings()
                normalized_phone = from_number.lstrip('+') if from_number.startswith('+') else from_number
                # A new turn starts here: the previous turn's reply must not suppress
                # an error notice that belongs to this message. Both keys are
                # independent, and this runs before the message is enqueued, so the
                # two round trips overlap instead of delaying processing twice.
                with stage("pending_reply_flags"):
                    await asyncio.gather(
                        redis_service.set(
                            f"{normalized_phone}:pending_reply",
                            "1",
                            ex=settings.pending_reply_ttl_seconds
                        ),
                        redis_service.delete(reply_sent_key(from_number)),
                    )
                logger.debug(f"[WORKER_TIMEOUT] Set pending_reply flag for {normalized_phone}")

            logger.info(f"[ENQUEUE_IN] [TURN:{trace.turn_id}] Enqueueing text message: {webhook_data}")

            # Enqueue the message - the service will handle batching and processing
            with stage("enqueue_message"):
                await message_queue_service.enqueue_message(webhook_data)

    except Exception as e:
        logger.error(f"Error enqueueing message: {e}", exc_info=True)
        finish_turn("error", error=type(e).__name__, at="enqueue")

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
            
            # Set pending_reply flag to track that user is waiting for a response
            redis_service = get_redis_service()
            settings = get_settings()
            normalized_phone = from_number.lstrip('+') if from_number.startswith('+') else from_number
            # A new turn starts here: the previous turn's reply must not suppress
            # an error notice that belongs to this message. Both keys are
            # independent, so the two round trips overlap instead of delaying
            # processing twice.
            await asyncio.gather(
                redis_service.set(
                    f"{normalized_phone}:pending_reply",
                    "1",
                    ex=settings.pending_reply_ttl_seconds
                ),
                redis_service.delete(reply_sent_key(from_number)),
            )
            logger.debug(f"[WORKER_TIMEOUT] Set pending_reply flag for {normalized_phone} (non-text)")
        
        processing_start_time = datetime.now()
        trace = resume_turn(webhook_data, source="direct", user_phone=from_number or "")
        logger.info(f"[DIRECT_IN] [TURN:{trace.turn_id}] Processing non-text message directly: {webhook_data}")
        logger.info(f"Background task started at: {processing_start_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        # Extract message details
        message_type = webhook_data.get("type", "text")
        from_number = webhook_data.get("from")
        content = webhook_data.get("content")

        if not from_number or not content:
            logger.warning("Missing required message data")
            trace.finish("dropped", reason="missing_phone_or_content")
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
                    f"Skipping {message_type} message to prevent concurrent processing conflicts. "
                    f"The user receives no reply to this message."
                )
                trace.finish("dropped", reason="already_processing")
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
                        with stage("process_document", type=message_type):
                            await process_document_message(webhook_data, chat_service)
                    else:
                        # Process other non-text messages (image, interactive, etc.) through chat service
                        with stage("chat_process_message", type=message_type):
                            await chat_service.process_message(
                                user_phone=from_number,
                                message_content=content,
                                message_type=message_type
                            )
                finally:
                    # Cleanup to prevent unclosed aiohttp sessions
                    with stage("chat_cleanup"):
                        await chat_service.cleanup()
            trace.finish("complete")

    except Exception as e:
        logger.error(f"Error in async message processing: {e}", exc_info=True)
        finish_turn("error", error=type(e).__name__)

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
            # Process as Excel file through chat service
            # Note: "Please wait" message will be sent by service layer after validation checks
            # Note: Session monitoring will also send "please wait" if processing takes >15s
            await chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type="excel_upload"
            )
        else:
            # Handle non-Excel documents (PDFs, images sent as docs, DOCX, etc.)
            await chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type="document"
            )

    except Exception as e:
        logger.error(f"Error processing document message: {e}")


async def handle_technical_error_with_cancel(user_phone: str, error_message: str, error_type: str = "Technical Error"):
    """
    Handle technical errors by logging them and notifying the user once.

    Notification is delegated entirely to handle_technical_failure, which owns
    the single user-facing error message and the support-team alert. This used to
    send its own notice first, which delivered two error bubbles for one failure
    and claimed the session had been cleared even though nothing here clears it.

    Args:
        user_phone: User's phone number
        error_message: Technical error message for logging
        error_type: Type of error for categorization
    """
    try:
        logger.error(f"{error_type} for user {user_phone}: {error_message}")

        from app.utils.technical_failure_handler import handle_technical_failure
        await handle_technical_failure(
            user_phone=user_phone,
            error_message=error_message,
            error_type=error_type
        )

    except Exception as e:
        # Don't let error handling fail the webhook response
        logger.error(f"Error in handle_technical_error_with_cancel: {e}")

