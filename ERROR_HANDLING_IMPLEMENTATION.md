# Global Error Handling System Implementation

## Overview

This document describes the implementation of the global error handling system for the AI Procurement Agent. The system provides consistent user responses and support team notifications for all technical error scenarios.

## Features

- **Centralized Error Handling**: Single point of error management for all technical issues
- **User-Friendly Responses**: Consistent error messages sent to users via WhatsApp
- **Support Team Notifications**: Automatic WhatsApp and email notifications to support team
- **Fallback Mechanisms**: Email notifications when WhatsApp service fails
- **Session Preservation**: Users don't lose context when errors occur
- **Comprehensive Coverage**: Handles API timeouts, database issues, and server errors

## Architecture

### Core Components

1. **GlobalErrorHandler** (`app/services/global_error_handler.py`)
   - Main error handling service
   - Manages user responses and support notifications
   - Provides fallback email when WhatsApp fails

2. **ErrorContext** (dataclass)
   - Structured error information container
   - Includes user details, error type, and context

3. **Integration Points**
   - API Client (`procucev_api_client.py`)
   - Database Layer (`database.py`)
   - FastAPI Application (`main.py`)

## Error Types Handled

### 1. API Errors
- **Scenarios**: API timeouts, connection failures, HTTP errors
- **Location**: `procucev_api_client.py`
- **Trigger**: After max retries exceeded
- **Information Captured**: API name, endpoint, payload, error message

### 2. Database Errors
- **Scenarios**: Connection lost, query timeouts, SQL errors
- **Location**: `database.py`
- **Trigger**: On connection or query failures
- **Information Captured**: Error message, affected operations

### 3. Server Errors
- **Scenarios**: Unhandled exceptions, internal server errors
- **Location**: `main.py` (global exception handler)
- **Trigger**: Any unhandled exception
- **Information Captured**: Exception details, request context

## Configuration

Add these settings to your `.env` file:

```env
# Support Team Configuration
SUPPORT_EMAIL=support@procucev.com
SUPPORT_TEAM_NUMBERS=919876543210,919876543211

# Error Handling Configuration
ENABLE_ERROR_NOTIFICATIONS=true
ERROR_NOTIFICATION_COOLDOWN_MINUTES=5
```

## User Experience

When technical errors occur, users receive this message:

> "There seems to be a technical issue at the moment. Our team is working on it. Please try again later. For urgent requirements, contact support@procucev.com. We apologize for the inconvenience."

**Key Benefits:**
- Users are informed about the issue
- Clear next steps provided
- Session context is preserved
- Professional, apologetic tone

## Support Team Notifications

### WhatsApp Notification Format

```
Hi Procucev Team,
We wanted to inform you about a technical issue we've encountered:

Issue Details:
API Name: Payment Gateway API
Endpoint: /api/v1/payments/process
Error Type: API Error
Error Message: 500 (Internal Server Error - Timeout)
Request Payload: {"amount": 1000, "currency": "INR"}...
Occurrence Time: 2025-01-27 10:30:45 UTC
Affected User Phone: 919876543210
User Name: John Doe
User Email: john@example.com
Current Flow/Stage: Payment Processing
Environment: client
```

### Email Notification (Fallback)

**Subject**: `[ALERT] WhatsApp Bot Error - API Error - 2025-01-27 10:30:45`

Contains the same detailed information as WhatsApp notification.

## Usage Examples

### 1. API Error Handling

```python
from app.services.global_error_handler import handle_api_error

# In API client code
try:
    response = await api_call()
except Exception as e:
    await handle_api_error(
        api_name="Procucev API",
        endpoint="/authenticate",
        error_message=str(e),
        payload=request_data,
        user_phone=user_phone
    )
```

### 2. Database Error Handling

```python
from app.services.global_error_handler import handle_database_error

# In database code
try:
    session = get_db_session()
except Exception as e:
    await handle_database_error(
        error_message=str(e),
        user_phone=user_phone,
        current_flow="User Registration"
    )
```

### 3. Custom Error Context

```python
from app.services.global_error_handler import ErrorContext, get_global_error_handler

error_context = ErrorContext(
    error_type="Custom Error",
    error_message="Specific error description",
    user_phone="919876543210",
    current_flow="Current Operation"
)

handler = get_global_error_handler()
await handler.handle_error(error_context)
```

## Integration Points

### 1. API Client Integration

The `procucev_api_client.py` automatically handles API failures:

```python
# After max retries exceeded
await handle_api_error(
    api_name=api_title or f"Procucev API {method}",
    endpoint=url_or_endpoint,
    error_message=f"{error_type}: {str(e)}",
    payload=json_data
)
```

### 2. Database Integration

The `database.py` handles connection and query failures:

```python
# On database connection failure
await handle_database_error(
    f"Database connection failed: {str(e)}"
)
```

### 3. FastAPI Integration

The `main.py` includes a global exception handler:

```python
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    await handle_server_error(
        error_message=f"Unhandled exception: {str(exc)}",
        user_phone=user_phone,
        current_flow=f"{request.method} {request.url.path}"
    )
```

## Testing

Run the test script to verify error handling:

```bash
python test_error_handling.py
```

This will:
1. Test API error notifications
2. Test database error notifications  
3. Test server error notifications
4. Test custom error contexts
5. Send notifications to configured support team

## Migration from Existing Code

### Deprecated Methods

The existing `SupportNotificationService.notify_api_service_failure()` is now deprecated. Use the global error handler instead:

```python
# OLD (deprecated)
await support_service.notify_api_service_failure(error_message)

# NEW (recommended)
await handle_api_error(
    api_name="API Name",
    endpoint="/endpoint",
    error_message=error_message
)
```

### Preventing Duplicate Notifications

The system prevents duplicate notifications by:
1. Centralizing all technical error handling
2. Deprecating old notification methods
3. Using consistent error handling patterns

## Monitoring and Maintenance

### Log Messages

The system logs all error handling activities:

```
INFO - Sent error response to user 919876543210
INFO - WhatsApp notification sent to 919876543210
INFO - Email notification sent to support team
ERROR - WhatsApp notification failed: Connection timeout
```

### Configuration Validation

The system validates configuration on startup:
- Support team numbers format
- Email configuration
- Notification settings

## Best Practices

1. **Always Include Context**: Provide user phone, current flow, and relevant details
2. **Use Appropriate Error Types**: Choose the correct error type (API, Database, Server)
3. **Include Payload Information**: For API errors, include request payload for debugging
4. **Test Notifications**: Regularly test the notification system
5. **Monitor Logs**: Check logs for notification failures

## Troubleshooting

### Common Issues

1. **WhatsApp Notifications Not Sent**
   - Check `SUPPORT_TEAM_NUMBERS` configuration
   - Verify WhatsApp service credentials
   - Check network connectivity

2. **Email Notifications Not Sent**
   - Verify `SUPPORT_EMAIL` configuration
   - Check email service API credentials
   - Review email service logs

3. **Duplicate Notifications**
   - Ensure old notification code is removed
   - Use only global error handler functions
   - Check for multiple error handling paths

### Debug Mode

Enable debug logging to troubleshoot:

```env
LOG_LEVEL=DEBUG
DEBUG=true
```

## Security Considerations

1. **Sensitive Data**: Payload data is truncated to prevent sensitive information exposure
2. **User Privacy**: Only necessary user information is included in notifications
3. **Access Control**: Support team numbers should be carefully managed
4. **Rate Limiting**: Built-in cooldown prevents notification spam

## Future Enhancements

1. **Error Analytics**: Track error patterns and frequencies
2. **Smart Notifications**: Reduce noise with intelligent filtering
3. **Recovery Suggestions**: Include automated recovery steps
4. **Integration Monitoring**: Monitor third-party service health
5. **User Feedback**: Collect user feedback on error experiences