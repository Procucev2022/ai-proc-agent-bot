"""
Pydantic schemas for API request and response validation.

This module defines Pydantic models for validating API requests and responses
throughout the AI Procurement Agent system. It provides type safety, automatic
validation, and serialization for all data exchanged between API endpoints
and client applications.

Key responsibilities:
- Define request and response schemas for API endpoints
- Validate incoming webhook data from WhatsApp
- Serialize database models for API responses
- Provide type hints and documentation for API contracts
- Handle data transformation between API and internal formats
- Support API versioning and backward compatibility
"""

class WhatsAppMessageSchema:
    """
    Schema for incoming WhatsApp webhook messages.
    
    Validates the structure and content of webhook messages
    received from WhatsApp Business API.
    """
    pass

class UserRegistrationSchema:
    """
    Schema for user registration requests.
    
    Validates user registration data including contact information,
    company details, and role assignments.
    """
    pass

class VendorSearchRequestSchema:
    """
    Schema for vendor search API requests.
    
    Validates vendor search parameters including product categories,
    locations, and filtering criteria.
    """
    pass

class VendorResponseSchema:
    """
    Schema for vendor search API responses.
    
    Defines the structure of vendor information returned
    in search results and recommendations.
    """
    pass

class RFQCreateRequestSchema:
    """
    Schema for RFQ creation requests.
    
    Validates RFQ creation data including all required fields
    and business rule constraints.
    """
    pass

class RFQStatusResponseSchema:
    """
    Schema for RFQ status and update responses.
    
    Defines the structure of RFQ status information and
    workflow progress updates.
    """
    pass

class ConversationContextSchema:
    """
    Schema for conversation context and session data.
    
    Validates conversation state, extracted entities,
    and workflow progress information.
    """
    pass