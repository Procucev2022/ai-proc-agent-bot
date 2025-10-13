# Registration Detection Implementation

## Overview
This implementation adds registration detection functionality to the AI Procurement Agent, allowing users to register as buyers or sellers by simply saying "register me as buyer" or "register me as seller".

## Changes Made

### 1. Updated `user_selection_analysis.json`
- Added `register` field to the function schema
- The field returns `{"type": "buyer"}`, `{"type": "seller"}`, or `{"type": null}` based on user input
- Updated description to include registration intent detection

### 2. Updated `UserSelectionTool` (`app/tools/user_selection_tool.py`)
- Added `_detect_registration_intent()` method to detect buyer/seller registration phrases
- Updated all return statements to include the `register` field
- Added registration detection to both rule-based and AI-based analysis

### 3. Updated `ProfileSelectionService` (`app/services/profile_selection_service.py`)
- Modified `_detect_registration_intent()` to use the new UserSelectionTool functionality
- Updated `_redirect_to_buyer_registration()` and `_redirect_to_seller_registration()` to use the existing RegistrationService
- Updated `_handle_registration_type_response()` to use the new detection method

### 4. Updated User Selection Analysis Prompt
- Enhanced `app/prompts/profile_selection/user_selection_analysis.txt` to include registration detection examples
- Added guidelines for detecting registration intent even when no profile options are provided

## How It Works

### Registration Detection Flow
1. User says "register me as buyer" or similar phrase
2. `UserSelectionTool.analyze_user_selection()` is called
3. The tool detects registration intent and returns `{"register": {"type": "buyer"}}`
4. `ProfileSelectionService._detect_registration_intent()` extracts the registration type
5. If buyer registration is detected, `_redirect_to_buyer_registration()` is called
6. The existing `RegistrationService` handles the complete registration flow

### Supported Phrases
**Buyer Registration:**
- "register me as buyer"
- "register as buyer" 
- "register me as a buyer"
- "sign me up as buyer"
- "sign up as buyer"
- "create buyer account"
- "i want to register as buyer"
- "register buyer account"
- "add me as buyer"

**Seller Registration:**
- "register me as seller"
- "register as seller"
- "register me as a seller" 
- "sign me up as seller"
- "sign up as seller"
- "create seller account"
- "i want to register as seller"
- "register seller account"
- "add me as seller"

## Integration with Existing System

The implementation seamlessly integrates with the existing registration system:

1. **RegistrationService**: Handles the complete registration workflow including data collection, API calls, and OTP verification
2. **WorkflowManager**: Manages workflow transitions to registration flow
3. **EntityService**: Extracts registration data from user messages
4. **WhatsAppService**: Sends messages and interactive buttons

## Testing

A test script `test_registration_detection.py` has been created to verify the functionality works correctly.

## Usage Example

```
User: "register me as buyer"
System: Detects registration intent → {"register": {"type": "buyer"}}
System: Redirects to buyer registration flow
System: "Hello Buyer. To get started, please share..."
```

This implementation provides a natural and intuitive way for users to register without needing to navigate through profile selection menus.