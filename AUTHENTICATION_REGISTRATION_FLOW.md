# Complete Authentication & Registration Flow Integration

## Architecture Overview

The authentication and registration flows are seamlessly integrated with the existing RFQ creation architecture, using the same session management, workflow state tracking, and service orchestration patterns.

## Core Components

### Services
- **AuthenticationService**: Handles token validation, user lookup, intent clarification, email confirmation, and OTP verification
- **RegistrationService**: Manages entity-based data collection, registration API calls, and email OTP verification
- **SessionManagementService**: Maintains workflow state across all flows
- **ChatService**: Main orchestrator routing between authentication, registration, and RFQ flows

### Session State Management

```python
# Authentication Flow State
session.workflow_state = {
    "authentication_stage": "intent_clarification|user_lookup|email_confirmation|email_otp|completed",
    "user_intent": "buy|sell",
    "authentication_data": {...},
    "selected_email": "user@company.com",
    "unique_id": "user_123",
    "otp_retry_count": 0
}

# Registration Flow State  
session.workflow_state = {
    "registration_stage": "introduction|data_collection|confirmation|email_otp",
    "registration_intent": "buy|sell", 
    "extracted_entities": {...},
    "registration_data": {...},
    "otp_retry_count": 0
}
```

## Complete Flow Walkthrough

### Message 1: "Hi" (New User)

**Step 1: Token Validation**
```python
# ChatService.process_message()
user = await self._validate_token_and_get_user(user_phone)  # Returns None (no token)
```

**Step 2: Authentication Flow Initiation**
```python
# ChatService._handle_authentication_and_registration_flow()
# No current stage, start authentication
return await self.authentication_service.validate_token_and_authenticate(user_phone, "Hi", session)
```

**Step 3: Intent Classification**
```python
# AuthenticationService.validate_token_and_authenticate()
intent_result = await self._classify_user_intent("Hi")  # Returns "unclear"
```

**Step 4: Intent Clarification Request**
```python
# AuthenticationService._request_intent_clarification()
session.workflow_state = {
    "authentication_stage": "intent_clarification"
}

# Bot sends: "Welcome, and thank you for reaching out to QUA! How can I assist you today — are you looking to Buy or Sell?"
```

### Message 2: "I want to buy laptop"

**Step 1: Session Retrieval**
```python
# Same session retrieved, workflow_state contains authentication_stage: "intent_clarification"
```

**Step 2: Authentication Workflow Routing**
```python
# ChatService._handle_authentication_workflow_routing()
# stage == "intent_clarification"
result = await self.authentication_service.handle_intent_clarification_response(user_phone, message, session)
```

**Step 3: Intent Re-classification**
```python
# AuthenticationService.handle_intent_clarification_response()
intent_result = await self._classify_user_intent("I want to buy laptop")  # Returns "buy"
session.workflow_state["user_intent"] = "buy"
session.workflow_state["authentication_stage"] = "user_lookup"
```

**Step 4: Authentication API Call**
```python
# AuthenticationService._call_authentication_api()
api_result = await self._call_authentication_api(user_phone)
# Returns: {"found": False} (new user)
```

**Step 5: Redirect to Registration**
```python
# AuthenticationService._handle_authentication_response()
# User not found, redirect to registration
session.workflow_state["authentication_stage"] = "redirect_to_registration"
session.workflow_state["registration_intent"] = "buy"

return {"status": "redirect_to_registration", "user_intent": "buy"}
```

### Message 3: "John Doe, ABC Company, john@abc.com, 560001"

**Step 1: Registration Flow Routing**
```python
# ChatService._handle_authentication_workflow_routing()
# stage == "redirect_to_registration"
result = await self.registration_service.initiate_registration(user_phone, session, "buy", message)
```

**Step 2: Registration Initiation**
```python
# RegistrationService.initiate_registration()
session.workflow_type = "registration"
session.workflow_state = {
    "registration_stage": "data_collection",
    "registration_intent": "buy",
    "extracted_entities": {}
}

# Bot sends buyer introduction message with required fields
```

**Step 3: Entity Extraction**
```python
# RegistrationService.handle_registration_data_collection()
entity_result = await self._extract_registration_entities(message, {}, "buy")
# Extracts: {"name": "John Doe", "companyName": "ABC Company", "email": "john@abc.com", "pincode": "560001"}
```

**Step 4: Completeness Check**
```python
# RegistrationService._get_missing_registration_fields()
missing_fields = []  # All required fields present for buyer
```

**Step 5: Registration Confirmation**
```python
# RegistrationService._request_registration_confirmation()
session.workflow_state["registration_stage"] = "confirmation"

# Bot sends: "Please confirm your registration details: • Name: John Doe • Company: ABC Company..."
```

### Message 4: "Yes"

**Step 1: Registration Confirmation Handling**
```python
# RegistrationService.handle_registration_confirmation()
confirmation_result = {"confirmed": True}
```

**Step 2: Registration API Call**
```python
# RegistrationService._call_registration_api()
api_result = await self._mock_registration_api_call(registration_data)
# Returns: {"success": True, "unique_id": "user_123", "email": "john@abc.com"}
```

**Step 3: Email OTP Initiation**
```python
# RegistrationService._initiate_registration_email_otp()
session.workflow_state["registration_stage"] = "email_otp"
session.workflow_state["registration_data"] = {"email": "john@abc.com", "unique_id": "user_123"}

# Bot sends: "Registration initiated! OTP sent to your email: john@abc.com. Please enter the OTP:"
```

### Message 5: "123456" (OTP)

**Step 1: OTP Verification**
```python
# RegistrationService.handle_registration_otp_verification()
verification_result = await self._verify_registration_otp("123456", "john@abc.com", "user_123")
# Returns: {"success": True}
```

**Step 2: Registration Completion**
```python
# RegistrationService._complete_registration()
# For buyer: Check domain approval
domain_check = await self._check_buyer_domain_approval("john@abc.com")
# Returns: {"approved": True} (abc.com is approved domain)

await self._update_buyer_approval_status("user_123", True)

# Bot sends: "Registration successful—thank you! How can I help you today? Want to raise an RFQ or any other support?"
```

**Step 3: Transition to Main Flow**
```python
# RegistrationService._complete_registration()
return {"status": "buyer_registration_completed", "ready_for_main_flow": True}

# ChatService._handle_registration_workflow_routing()
# ready_for_main_flow == True
mock_user = self._create_mock_authenticated_user(user_phone, session)
session.workflow_type = None  # Reset to allow RFQ flow
session.workflow_state = {"extracted_entities": []}  # Reset state

# Process message through main RFQ flow
return await self._process_text_message(mock_user, session, "123456")
```

### Message 6: "I need 10 laptops for IT division"

**Step 1: Main Flow Processing**
```python
# Now user is authenticated and registered, processes through normal RFQ flow
# ChatService._process_text_message() with authenticated mock_user

# Intent classification: "buy_something"
# Entity extraction: {"description": "laptops", "quantity": "10", "division": "IT"}
# Products array handling: Missing fields detected
# Bot asks for delivery details, etc.
```

## Key Integration Points

### 1. Session Continuity
- Same session ID used across authentication, registration, and RFQ flows
- Workflow state persists and transitions between flows
- Conversation history maintained throughout

### 2. Workflow Type Transitions
```python
# Flow progression:
None → "authentication" → "registration" → None (ready for RFQ) → "rfq_creation" → "rfq_submitted"
```

### 3. State Management Patterns
- Same `workflow_state` structure used across all flows
- Entity extraction patterns consistent with RFQ flow
- Error handling and support redirection unified

### 4. Service Architecture
- Authentication and Registration services follow same patterns as RFQ services
- Dependency injection and service composition consistent
- Response helpers and OpenAI integration unified

## API Integration Points

### Authentication APIs
- **API 1**: User lookup by phone number
- **API 3**: OTP verification  
- **API 4**: Approval status updates
- **API 5**: Send OTP

### Registration APIs
- **API 2**: User registration
- **API 3**: OTP verification (shared)
- **API 4**: Approval status updates (shared)

## Error Handling & Support

### Support Redirection Scenarios
- Authentication API failures
- Registration API failures  
- OTP verification failures (after 2 attempts)
- Domain approval failures
- Unknown workflow states

### Error Recovery
- Session state preservation during errors
- Graceful fallbacks to previous stages
- Automatic retry mechanisms for transient failures

## Testing Scenarios

### Buyer Flow
1. New user → Intent clarification → User not found → Registration → Domain approved → RFQ creation
2. Existing user → Email confirmation → OTP verification → RFQ creation
3. Domain mismatch → Manual approval required → Support contact

### Seller Flow  
1. New user → Intent clarification → Registration with GSTIN → Complementary RFQ offer
2. Existing user → Email confirmation → OTP verification → RFQ opportunities
3. Registration failure → Support redirection

This architecture ensures seamless integration between authentication, registration, and RFQ creation flows while maintaining the same high-quality session management and workflow orchestration patterns established in the RFQ system.