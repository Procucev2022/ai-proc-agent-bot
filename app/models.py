"""
Database models for the AI Procurement Agent system.

This module defines SQLAlchemy ORM models for all database entities including
users, vendors, products, RFQs, conversation sessions, and learning records.
These models represent the core data structures used throughout the application
for persistence and data management.

Key responsibilities:
- Define database table structures and relationships
- Implement data validation and constraints
- Support vendor profile management and categorization
- Track RFQ lifecycle and status management
- Store conversation context and session data
- Maintain learning records for vendor-category associations
- Provide query interfaces for service layer operations
"""

class User:
    """
    User model for managing registered users in the system.
    
    Stores user information, authentication status, and role-based access.
    Supports buyers, vendors, and category managers with different permissions.
    """
    pass

class Vendor:
    """
    Vendor model for managing vendor profiles and capabilities.
    
    Stores comprehensive vendor information including service categories,
    geographic coverage, performance metrics, and learned associations
    from Category Manager assignments.
    """
    pass

class Product:
    """
    Product model for Buy From Stock (BFS) inventory management.
    
    Represents products available for immediate purchase with pricing,
    availability, and specification information for direct sales.
    """
    pass

class RFQ:
    """
    RFQ (Request for Quotation) model for managing procurement requests.
    
    Tracks the complete lifecycle of RFQ creation, validation, submission,
    and vendor assignment process with all collected field information.
    """
    pass

class RFQVendorAssignment:
    """
    Association model for RFQ-Vendor assignments and learning.
    
    Tracks when Category Managers assign vendors to RFQs, enabling
    the learning feedback loop for automatic vendor categorization.
    """
    pass

class ConversationSession:
    """
    Model for managing user conversation sessions and context.
    
    Stores conversation state, extracted entities, workflow progress,
    and session management information for maintaining context across
    multiple message exchanges.
    """
    pass

class LearningRecord:
    """
    Model for tracking learning events and vendor categorization improvements.
    
    Records when the system learns new vendor-category associations from
    Category Manager assignments, supporting continuous improvement of
    vendor matching accuracy.
    """
    pass