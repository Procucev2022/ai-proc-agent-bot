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

from sqlalchemy import Column, String, Text, TIMESTAMP, Date, ForeignKey, Enum, ARRAY, Boolean, Integer, DECIMAL
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
import uuid

Base = declarative_base()

# Enum definitions following DatabaseDoc.md
class RFQStatus(enum.Enum):
    collecting = "collecting"
    ready = "ready"
    submitted = "submitted"
    failed = "failed"

class WorkflowType(enum.Enum):
    product_search = "product_search"
    rfq_creation = "rfq_creation"
    rfq_submitted = "rfq_submitted"
    general_inquiry = "general_inquiry"
    excel_rfq_upload = "excel_rfq_upload"

class ConversationOutcome(enum.Enum):
    completed = "completed"
    abandoned = "abandoned"
    escalated = "escalated"
    timeout = "timeout"

class User:
    """
    User model for managing registered users in the system.
    
    Stores user information, authentication status, and role-based access.
    Supports buyers, vendors, and category managers with different permissions.
    """
    pass

class Vendor(Base):
    """
    Vendor model for managing vendor profiles and capabilities.
    
    Stores comprehensive vendor information including service categories,
    geographic coverage, performance metrics, and learned associations
    from Category Manager assignments.
    """
    __tablename__ = "vendors"
    
    vendor_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_name = Column(String(255), nullable=False)
    geographic_coverage = Column(ARRAY(Text), nullable=False)
    vendor_services = Column(ARRAY(Text), nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    learned_associations = relationship("LearningRecord", back_populates="vendor")

class ProductCategory(Base):
    """
    Product category model for standardized categorization.
    
    Maintains standardized product categories for consistent
    vendor matching and search functionality.
    """
    __tablename__ = "product_categories"
    
    category_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_name = Column(String(255), unique=True, nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class Product:
    """
    Product model for Buy From Stock (BFS) inventory management.
    
    Represents products available for immediate purchase with pricing,
    availability, and specification information for direct sales.
    """
    pass

class RFQ(Base):
    """
    RFQ (Request for Quotation) model for managing procurement requests.
    
    Tracks the complete lifecycle of RFQ creation, validation, submission,
    and vendor assignment process with all collected field information.
    """
    __tablename__ = "rfq_records"
    
    rfq_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_user_id = Column(String(255), nullable=False)
    api_payload = Column(JSONB, nullable=False)
    status = Column(Enum(RFQStatus), default=RFQStatus.collecting)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    submitted_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    conversation_outcome = relationship("ConversationSession", back_populates="rfq", uselist=False)

class RFQVendorAssignment:
    """
    Association model for RFQ-Vendor assignments and learning.
    
    Tracks when Category Managers assign vendors to RFQs, enabling
    the learning feedback loop for automatic vendor categorization.
    """
    pass

class ConversationSession(Base):
    """
    Enhanced model for managing user conversation sessions and outcomes.
    
    Stores comprehensive conversation state, extracted entities, workflow progress,
    WhatsApp context, error details, performance metrics, and session management 
    information for maintaining context across multiple message exchanges.
    """
    __tablename__ = "conversation_sessions"
    
    session_id = Column(String(255), primary_key=True)
    external_user_id = Column(String(255), nullable=False)
    workflow_type = Column(Enum(WorkflowType), nullable=True)
    outcome = Column(Enum(ConversationOutcome), nullable=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfq_records.rfq_id"), nullable=True)
    workflow_state = Column(JSONB, nullable=False)
    conversation_history = Column(JSONB, nullable=False)
    extracted_entities = Column(JSONB, nullable=True)
    whatsapp_context = Column(JSONB, nullable=True)
    error_details = Column(JSONB, nullable=True)
    performance_metrics = Column(JSONB, nullable=True)
    retention_date = Column(Date, nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    completed_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    rfq = relationship("RFQ", back_populates="conversation_outcome")

class LearningRecord(Base):
    """
    Model for tracking learning events and vendor categorization improvements.
    
    Records when the system learns new vendor-category associations from
    Category Manager assignments, supporting continuous improvement of
    vendor matching accuracy.
    """
    __tablename__ = "learned_associations"
    
    association_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.vendor_id"), nullable=False)
    category = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    vendor = relationship("Vendor", back_populates="learned_associations")

class SystemLog(Base):
    """
    Simple system log model for storing application logs in database.
    
    Stores log entries with level, message, and optional context for
    debugging and monitoring purposes.
    """
    __tablename__ = "system_logs"
    
    log_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    level = Column(String(20), nullable=False)  # INFO, ERROR, DEBUG, WARNING
    message = Column(Text, nullable=False)
    service = Column(String(100), nullable=True)  # chat_service, openai_service, etc.
    user_id = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True)
    context = Column(JSONB, nullable=True)  # Additional context data
    created_at = Column(TIMESTAMP, default=func.current_timestamp())