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

from sqlalchemy import Column, String, Text, TIMESTAMP, Date, ForeignKey, Enum, Boolean, Integer, DECIMAL, JSON, UniqueConstraint
from sqlalchemy.types import CHAR
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

class UserType(enum.Enum):
    buyer = "buyer"
    seller = "seller"
    unknown = "unknown"

class SessionState(enum.Enum):
    active = "active"
    inactive = "inactive"
    completed = "completed"
    abandoned = "abandoned"

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
    
    vendor_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vendor_name = Column(String(255), nullable=False)
    geographic_coverage = Column(JSON, nullable=False)
    vendor_services = Column(JSON, nullable=False)
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
    
    category_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
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
    
    rfq_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    external_user_id = Column(String(255), nullable=False)
    api_payload = Column(JSON, nullable=False)
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
    WhatsApp context, error details, performance metrics, session management 
    information, and new B2B WhatsApp architecture fields for business analytics.
    """
    __tablename__ = "conversation_sessions"
    
    session_id = Column(String(255), primary_key=True)
    external_user_id = Column(String(255), nullable=False)
    user_type = Column(Enum(UserType), default=UserType.unknown)
    session_state = Column(Enum(SessionState), default=SessionState.active)
    workflow_type = Column(Enum(WorkflowType), nullable=True)
    outcome = Column(Enum(ConversationOutcome), nullable=True)
    
    # Enhanced RFQ support
    rfq_id = Column(CHAR(36), ForeignKey("rfq_records.rfq_id"), nullable=True)  # Keep for backward compatibility
    rfq_ids = Column(JSON, nullable=True)  # Array of multiple RFQ IDs per session
    rfq_metadata = Column(JSON, nullable=True)  # Value estimates, priority, terms
    
    # Enhanced session data
    started_at = Column(TIMESTAMP, default=func.current_timestamp())
    product_items = Column(JSON, nullable=True)  # Detailed product information per RFQ
    seller_responses = Column(JSON, nullable=True)  # All seller interactions with RFQ
    interaction_metrics = Column(JSON, nullable=True)  # Message counts, duration, navigation
    parent_session_id = Column(String(255), nullable=True)  # Link to previous session if continuation
    
    # BFS (Buy From Stock) tracking
    bfs_products_searched = Column(JSON, nullable=True)  # BFS products searched during session
    bfs_search_count = Column(Integer, default=0)  # Number of BFS searches performed
    bfs_price_accepted = Column(JSON, nullable=True)  # BFS prices accepted by buyers
    bfs_counter_offers = Column(JSON, nullable=True)  # Counter offers made on BFS products
    
    # Bidding and RFQ lifecycle tracking
    products_bid_for = Column(JSON, nullable=True)  # Products that received bids in this session
    bids_received = Column(JSON, nullable=True)  # All bids received on RFQs
    bids_accepted = Column(JSON, nullable=True)  # Bids accepted by buyers
    counter_offers_made = Column(JSON, nullable=True)  # Counter offers made by sellers
    counter_offers_accepted = Column(JSON, nullable=True)  # Counter offers accepted by buyers
    rfqs_with_response = Column(JSON, nullable=True)  # RFQs that received responses
    avg_products_per_rfq = Column(DECIMAL(5,2), nullable=True)  # Average products per RFQ
    avg_categories_per_rfq = Column(DECIMAL(5,2), nullable=True)  # Average categories per RFQ
    
    # Existing fields
    workflow_state = Column(JSON, nullable=False)
    conversation_history = Column(JSON, nullable=False)
    extracted_entities = Column(JSON, nullable=True)
    whatsapp_context = Column(JSON, nullable=True)
    error_details = Column(JSON, nullable=True)
    performance_metrics = Column(JSON, nullable=True)
    retention_date = Column(Date, nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    last_activity_at = Column(TIMESTAMP, default=func.current_timestamp())
    completed_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    rfq = relationship("RFQ", back_populates="conversation_outcome")

class SessionEvent(Base):
    """
    Model for tracking session events for audit trail and business analytics.
    
    Records all significant actions within sessions for comprehensive
    business intelligence and debugging capabilities.
    """
    __tablename__ = "session_events"
    
    event_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(255), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)  # session_start, rfq_created, product_added, etc
    event_timestamp = Column(TIMESTAMP, default=func.current_timestamp(), index=True)
    event_data = Column(JSON, nullable=True)  # Event-specific payload
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class LearningRecord(Base):
    """
    Model for tracking learning events and vendor categorization improvements.
    
    Records when the system learns new vendor-category associations from
    Category Manager assignments, supporting continuous improvement of
    vendor matching accuracy.
    """
    __tablename__ = "learned_associations"
    
    association_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vendor_id = Column(CHAR(36), ForeignKey("vendors.vendor_id"), nullable=False)
    category = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    vendor = relationship("Vendor", back_populates="learned_associations")

class ChatSummary(Base):
    """
    Chat session summary model for storing AI-generated session summaries.
    
    Stores rich, personalized summaries of user sessions including extracted
    entities, RFQ information, and narrative context for future conversations.
    """
    __tablename__ = "chat_summaries"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(255), nullable=False, index=True)
    external_user_id = Column(String(255), nullable=False, index=True)
    ai_generated_summary = Column(Text, nullable=False)
    extracted_entities = Column(JSON, nullable=True)
    rfq_ids = Column(JSON, nullable=True)
    session_outcome = Column(Enum(ConversationOutcome), nullable=True)
    session_duration = Column(Integer, nullable=True)  # Duration in minutes
    created_at = Column(TIMESTAMP, default=func.current_timestamp(), index=True)

class DailySummary(Base):
    """
    Enhanced daily activity summary model for storing user's daily procurement metrics.
    
    Stores comprehensive daily statistics including session counts, RFQ counts,
    user type patterns, session states, and interaction metrics for individual users.
    """
    __tablename__ = "daily_summaries"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    external_user_id = Column(String(255), nullable=False, index=True)
    
    # Enhanced user insights
    user_type = Column(Enum(UserType), nullable=True)  # Predominant user type for the day
    sessions_count = Column(Integer, default=0)
    rfqs_created_count = Column(Integer, default=0)
    session_states_breakdown = Column(JSON, nullable=True)  # Count by state: {"completed": 5, "abandoned": 1}
    seller_interaction_count = Column(Integer, default=0)  # Number of seller responses received
    avg_session_duration = Column(Integer, nullable=True)  # Average session duration in minutes
    
    # Existing fields enhanced
    primary_product_categories = Column(JSON, nullable=True)
    total_rfq_value_estimate = Column(DECIMAL(15,2), nullable=True)
    
    # Additional insights
    product_items_count = Column(Integer, default=0)  # Total product items across all RFQs
    unique_categories_count = Column(Integer, default=0)  # Number of unique categories touched
    session_continuation_count = Column(Integer, default=0)  # Sessions linked to previous sessions
    
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('external_user_id', 'date', name='unique_user_date'),
    )

class CategoryMapping(Base):
    """
    Simplified category mapping table for 2-level categorization.
    
    Stores simple Category → Item mapping for streamlined categorization.
    """
    __tablename__ = "category_mappings"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    category = Column(String(255), nullable=False, index=True)  # Main category name
    item = Column(String(255), nullable=False, index=True)      # Specific item/product
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class AutoCategorizationLog(Base):
    """
    Log table for auto-categorization attempts and results.
    
    Tracks each auto-categorization attempt for monitoring and improvement.
    """
    __tablename__ = "auto_categorization_log"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rfq_id = Column(CHAR(36), nullable=True)
    session_id = Column(String(255), nullable=True)
    user_id = Column(String(255), nullable=False)
    input_description = Column(Text, nullable=False)
    predicted_category = Column(String(255), nullable=True)
    confidence_score = Column(DECIMAL(5,4), default=0.0)
    similarity_score = Column(DECIMAL(5,4), nullable=True)  # For vector search
    method_used = Column(String(50), default="vector_search")  # vector_search, ai_fallback
    processing_time_ms = Column(Integer, default=0)
    user_confirmed_category = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class SystemLog(Base):
    """
    Simple system log model for storing application logs in database.
    
    Stores log entries with level, message, and optional context for
    debugging and monitoring purposes.
    """
    __tablename__ = "system_logs"
    
    log_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    level = Column(String(20), nullable=False)  # INFO, ERROR, DEBUG, WARNING
    message = Column(Text, nullable=False)
    service = Column(String(100), nullable=True)  # chat_service, openai_service, etc.
    user_id = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True)
    context = Column(JSON, nullable=True)  # Additional context data
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class LearningCategory(Base):
    """
    AI-generated 3-level category taxonomy independent of client mappings.
    
    Stores structured 3-level categorization created by the learning system
    with confidence scores and usage tracking for continuous improvement.
    """
    __tablename__ = "learning_categories"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    level_1_category = Column(String(255), nullable=False, index=True)  # Top-level category
    level_2_category = Column(String(255), nullable=False, index=True)  # Mid-level category
    level_3_category = Column(String(255), nullable=False, index=True)  # Specific category
    confidence_score = Column(DECIMAL(5,4), default=0.0)  # AI confidence in this categorization
    usage_frequency = Column(Integer, default=0)  # How often this category is used
    created_by = Column(String(50), default="ai_learning")  # ai_learning, manual, etc.
    ai_reasoning = Column(Text, nullable=True)  # OpenAI explanation for the categorization
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())
    
    # Relationships
    category_items = relationship("LearningCategoryItem", back_populates="learning_category")

class LearningCategoryItem(Base):
    """
    Items mapped to learning categories with client category cross-references.
    
    Links specific items to learning categories while maintaining traceability
    to original client categorizations for comparison and validation.
    """
    __tablename__ = "learning_category_items"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    learning_category_id = Column(CHAR(36), ForeignKey("learning_categories.id"), nullable=False)
    item_description = Column(String(500), nullable=False, index=True)  # Processed item description
    normalized_keywords = Column(JSON, nullable=True)  # Extracted keywords for search
    confidence_score = Column(DECIMAL(5,4), default=0.0)  # Item-specific confidence
    user_feedback = Column(String(20), nullable=True)  # correct, incorrect, partial for future ML
    similarity_score = Column(DECIMAL(5,4), nullable=True)  # Vector similarity when added
    client_category_mapping_id = Column(CHAR(36), ForeignKey("category_mappings.id"), nullable=True)
    client_category_name = Column(String(255), nullable=True, index=True)  # Denormalized for quick access
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())
    
    # Relationships
    learning_category = relationship("LearningCategory", back_populates="category_items")
    client_mapping_cross_refs = relationship("ClientCategoryMapping", back_populates="learning_item")

class DailyAggregatedMetrics(Base):
    """
    Model for storing comprehensive daily business metrics and analytics.
    
    Stores all aggregated business intelligence metrics calculated at end-of-day
    including buyer patterns, seller activities, and category performance.
    """
    __tablename__ = "daily_aggregated_metrics"
    
    metric_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    metric_type = Column(String(50), nullable=False, index=True)  # buyer_summary, seller_summary, category_summary
    metric_data = Column(JSON, nullable=False)  # All aggregated values for the day
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    is_complete = Column(Boolean, default=False)  # Whether all sessions were processed
    
    __table_args__ = (
        UniqueConstraint('date', 'metric_type', name='unique_daily_metric'),
    )

class RollingWindowMetrics(Base):
    """
    Model for storing rolling window aggregated metrics (7/30/90 day windows).
    
    Maintains pre-calculated rolling window metrics for performance and
    trend analysis across different time periods.
    """
    __tablename__ = "rolling_window_metrics"
    
    window_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    window_type = Column(Enum("7day", "30day", "90day", name="window_type_enum"), nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    buyer_metrics = Column(JSON, nullable=True)  # Aggregated buyer data
    seller_metrics = Column(JSON, nullable=True)  # Aggregated seller data  
    category_metrics = Column(JSON, nullable=True)  # Aggregated category data
    last_updated = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('window_type', 'end_date', name='unique_window_metric'),
    )

class ClientCategoryMapping(Base):
    """
    Cross-reference tracking between learning categories and client categories.
    
    Maintains connections between AI-generated learning categories and original
    client category mappings for performance analysis and gradual transition.
    """
    __tablename__ = "client_category_mapping"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    learning_category_item_id = Column(CHAR(36), ForeignKey("learning_category_items.id"), nullable=False)
    category_mapping_id = Column(CHAR(36), ForeignKey("category_mappings.id"), nullable=False)
    learning_category_path = Column(String(500), nullable=False, index=True)  # level1 > level2 > level3
    client_category_name = Column(String(255), nullable=False, index=True)  # Original client category
    similarity_score = Column(DECIMAL(5,4), default=0.0)  # How similar learning vs client categories are
    mapping_confidence = Column(Enum("high", "medium", "low", name="mapping_confidence_enum"), default="medium")
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    learning_item = relationship("LearningCategoryItem", back_populates="client_mapping_cross_refs")