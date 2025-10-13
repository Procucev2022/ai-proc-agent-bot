# RFQ Status Check Intent Implementation

## Overview

This document describes the implementation of **Case 4: Common Intent (Buyer & Seller Applicable)** for RFQ status check functionality. The implementation handles the scenario where both buyers and sellers can check RFQ statuses, with intelligent profile selection and empty RFQ flow handling.

## Key Features Implemented

### 1. Profile Selection Logic
- **Multiple Profiles**: Shows selection menu when user has multiple profiles
- **Single Profile**: Auto-selects and proceeds directly when user has only one profile
- **Role Agnostic**: Both buyers and sellers can check RFQ status

### 2. Empty RFQ Flow
- **Detection**: Automatically detects when no RFQs are found for selected profile
- **Friendly Fallback**: Shows user-friendly message with actionable options
- **Role-Specific Options**: Different options based on user role (buyer vs seller)

### 3. Profile Context Maintenance
- **Session State**: Maintains profile context throughout the flow
- **Workflow Management**: Proper workflow type tracking and session management
- **Error Handling**: Robust error handling with fallback messages

## Implementation Details

### Files Modified

1. **`app/services/profile_selection_service.py`**
   - Enhanced `_handle_rfq_status_check()` method
   - Added single profile auto-selection logic
   - Improved multiple profile selection messaging

2. **`app/services/rfq_service.py`**
   - Added `_handle_empty_rfq_flow()` method
   - Enhanced `process_rfq_status_request()` to detect empty results
   - Role-specific empty flow messaging

3. **`app/services/rfq_status_service.py`**
   - Added `handle_empty_rfq_flow_response()` method
   - Enhanced `handle_rfq_status_inquiry()` to handle empty flows
   - Session state management for empty flow responses

4. **`app/services/chat_service.py`**
   - Updated `_handle_rfq_status_inquiry()` to handle empty flow redirects
   - Added proper flow routing for empty RFQ responses

## Bot Message Examples

### Multiple Profiles Selection
```
I understand you'd like to check an RFQ status.
Please choose which profile you'd like to use:
 1️⃣ priya@gmail.com — Buyer
 2️⃣ priya@bluecrestsoft.com — Seller

Reply with the number to continue.
```

### Single Profile (Auto-Selected)
```
Got it! Checking RFQ status for your Buyer profile (john@company.com).
[Proceeds directly to RFQ status check]
```

### Empty RFQ Flow - Buyer
```
It seems there are no RFQs available under priya@gmail.com (Buyer).
Would you like to:
 1️⃣ Create a new RFQ
 2️⃣ Go back to the main menu
```

### Empty RFQ Flow - Seller
```
It seems there are no RFQs available under priya@bluecrestsoft.com (Seller).
Would you like to:
 1️⃣ View available RFQs to quote
 2️⃣ Go back to the main menu
```

## Flow Diagram

```
User: "Check RFQ status"
         ↓
   Intent Classification
    (rfq_status_check)
         ↓
   Profile Selection Service
         ↓
    ┌─────────────────┐
    │ Multiple        │ Single Profile
    │ Profiles?       │ Auto-select
    └─────────────────┘      ↓
         ↓                   │
   Show Selection Menu      │
         ↓                   │
   User Selects Profile ────┘
         ↓
   RFQ Status Service
         ↓
    ┌─────────────────┐
    │ RFQs Found?     │ No RFQs
    └─────────────────┘ Empty Flow
         ↓                   ↓
   Show RFQ Status     Show Options Menu
   + Dashboard Link         ↓
                      User Chooses Action
                           ↓
                    ┌─────────────────┐
                    │ 1: Create/View  │ 2: Main Menu
                    │ 2: Main Menu    │ General Inquiry
                    └─────────────────┘      ↓
                           ↓
                    Route to Appropriate Flow
```

## Expected Developer Actions

### 1. Intent Detection
- ✅ Detect `rfq_status_check` intent with confidence > 70%
- ✅ Route to profile selection service

### 2. Profile Management
- ✅ Fetch user profiles from cache/API
- ✅ Handle single vs multiple profile scenarios
- ✅ Maintain profile context in session

### 3. RFQ Status Processing
- ✅ Extract RFQ IDs from user message
- ✅ Fetch RFQ status from GMT API
- ✅ Detect empty results and trigger Empty RFQ Flow

### 4. Empty RFQ Flow Handling
- ✅ Show role-specific options when no RFQs found
- ✅ Handle user responses (1: Create/View, 2: Main Menu)
- ✅ Route to appropriate flows based on user choice

### 5. Session Management
- ✅ Update workflow type to `rfq_status_check`
- ✅ Store empty flow state in session
- ✅ Clear state after user makes choice
- ✅ Maintain profile context throughout flow

## Testing

A comprehensive test suite has been created in `test_rfq_status_check.py` that covers:

1. **Multiple Profiles Selection**: Verifies correct message format and options
2. **Single Profile Auto-Select**: Confirms direct proceeding without selection
3. **Empty RFQ Flow - Buyer**: Tests buyer-specific empty flow messaging
4. **Empty RFQ Flow Response - Create New**: Tests redirect to RFQ creation
5. **Empty RFQ Flow Response - Main Menu**: Tests redirect to general inquiry

### Running Tests
```bash
python test_rfq_status_check.py
```

## Configuration

The implementation uses existing configuration settings:
- `rfq_max_allowed`: Maximum number of RFQ IDs to process (default: 5)
- `rfq_followup_note`: Additional note for RFQ status responses

## Error Handling

- **API Failures**: Graceful fallback with user-friendly messages
- **Invalid Selections**: Retry prompts with clear instructions
- **Session Corruption**: Automatic session restart with preserved context
- **Network Issues**: Technical failure handler integration

## Future Enhancements

1. **Interactive Buttons**: Replace numbered options with WhatsApp interactive buttons
2. **RFQ Filtering**: Add filters by date, status, or category
3. **Bulk Operations**: Support for bulk RFQ status checks
4. **Notifications**: Proactive RFQ status update notifications
5. **Analytics**: Track empty RFQ flow conversion rates

## Conclusion

The implementation successfully handles Case 4 requirements:
- ✅ Both buyers and sellers can check RFQ status
- ✅ Intelligent profile selection (single auto-select, multiple choice)
- ✅ Empty RFQ flow with friendly fallback options
- ✅ Profile context maintenance throughout the flow
- ✅ Proper workflow management and session handling

The solution is minimal, focused, and integrates seamlessly with the existing architecture while providing a smooth user experience for RFQ status inquiries.