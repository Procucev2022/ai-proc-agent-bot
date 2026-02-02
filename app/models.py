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
from sqlalchemy.orm import validates
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
    authentication = "authentication"
    registration = "registration"
    product_search = "product_search"
    rfq_creation = "rfq_creation"
    rfq_submitted = "rfq_submitted"
    general_inquiry = "general_inquiry"
    excel_rfq_upload = "excel_rfq_upload"
    rfq_status_check = "rfq_status_check"
    seller_rfq_view = "seller_rfq_view"
    user_exit = "user_exit"
    # Additional workflow types found in codebase
    buy_something = "buy_something"
    modification_request = "modification_request"
    # Seller RFQ intimation - only initiated from "I'm Interested" button handler
    seller_rfq_intimation = "seller_rfq_intimation"
    # BFS seller bid - initiated from Accept/Reject bid buttons
    bfs_seller_bid = "bfs_seller_bid"

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

    WORKFLOW_STATE SCHEMA:
    =====================
    The workflow_state JSON column structure varies based on USE_TRACK2_RFQ_FLOW flag.

    LEGACY FLOW (USE_TRACK2_RFQ_FLOW = False):
    {
        "extracted_entities": [...],         # List of extracted product entities
        "incomplete_products": [...],        # Products with missing mandatory fields
        "complete_products": [...],          # Products with all mandatory fields
        "pending_rfq": {...},               # Single product pending confirmation
        "pending_combined_rfq": {...},      # Multiple products pending confirmation
        "pending_optional_rfq": {...},      # Single product with optional fields
        "pending_optional_combined_rfq": {...},  # Multiple with optional fields
        "optional_fields_asked": False,     # Whether optional fields were asked
        "stage": "collecting",              # Workflow stage (collecting/confirming/submitting)
        "last_activity_at": "ISO datetime"
    }

    TRACK 2 FLOW (USE_TRACK2_RFQ_FLOW = True):
    {
        # Legacy fields (may still exist but not actively used in Track 2)
        "extracted_entities": [...],
        "stage": "collecting",

        # Track 2 Delivery Module (Delivery-first approach)
        "delivery_details": {
            "delivery_date": "2025-11-12",  # ISO date format
            "pincode": "411005",
            "city": "Pune",                 # Auto-filled from pincode lookup
            "state": "Maharashtra"          # Auto-filled from pincode lookup
        },
        "delivery_confirmed": False,        # True after user confirms delivery details

        # Track 2 Items Module (One-time extraction)
        "initial_items_extracted": False,   # True after first successful extraction (prevents re-extraction)

        # Track 2 Format Modification (Structured text editing)
        "awaiting_delivery_modification": False,  # True when user is editing delivery format
        "awaiting_items_modification": False,     # True when user is editing items format
        "format_modification_subtype": None,      # "delivery" or "items"
        "original_format": None,                  # Stores formatted text for retry display
        "format_retry_count": 0                   # Increments on parse errors (max 3)
    }

    Track 2 Key Principles:
    - Delivery-first: Delivery must be confirmed before items collection
    - One-time extraction: initial_items_extracted flag prevents AI re-extraction on modifications
    - Format-based modifications: Users edit structured text (parsed by Track 1), not AI interpretation
    - Retry limits: Max 3 format parsing attempts before cancellation
    - Interruption handling: FAQ/greeting during RFQ flow supported
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
    
    # New metrics based on Excel requirements - COMMENTED OUT until database migration
    # subscription_plans_requested = Column(Integer, default=0)  # For sellers requesting subscription info
    # products_searched_count = Column(Integer, default=0)  # For buyers searching products
    # total_rfq_responses_received = Column(Integer, default=0)  # Total responses received on user's RFQs
    # bfs_counter_offers_by_buyer = Column(JSON, nullable=True)  # Counter offers made by buyers on BFS
    # unregistered_user_bfs_searches = Column(JSON, nullable=True)  # BFS searches by unregistered users
    
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

    # Validators to automatically convert strings to enums
    @validates('workflow_type')
    def validate_workflow_type(self, key, value):
        """Auto-convert string to WorkflowType enum."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return WorkflowType(value)
            except ValueError:
                import logging
                logging.getLogger(__name__).warning(f"Invalid workflow_type string: {value}, setting to None")
                return None
        return value

    @validates('outcome')
    def validate_outcome(self, key, value):
        """Auto-convert string to ConversationOutcome enum."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return ConversationOutcome(value)
            except ValueError:
                import logging
                logging.getLogger(__name__).warning(f"Invalid outcome string: {value}, setting to None")
                return None
        return value

    @validates('user_type')
    def validate_user_type(self, key, value):
        """Auto-convert string to UserType enum."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return UserType(value)
            except ValueError:
                import logging
                logging.getLogger(__name__).warning(f"Invalid user_type string: {value}, setting to UserType.unknown")
                return UserType.unknown
        return value

    @validates('session_state')
    def validate_session_state(self, key, value):
        """Auto-convert string to SessionState enum."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return SessionState(value)
            except ValueError:
                import logging
                logging.getLogger(__name__).warning(f"Invalid session_state string: {value}, setting to SessionState.active")
                return SessionState.active
        return value

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
    Stores both client category results and learning taxonomy matches.
    """
    __tablename__ = "auto_categorization_log"

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rfq_id = Column(CHAR(36), nullable=True)
    session_id = Column(String(255), nullable=True)
    user_id = Column(String(255), nullable=False)
    input_description = Column(Text, nullable=False)
    predicted_category = Column(String(255), nullable=True)  # Client category returned
    confidence_score = Column(DECIMAL(5,4), default=0.0)
    similarity_score = Column(DECIMAL(5,4), nullable=True)  # For vector search
    method_used = Column(String(50), default="vector_search")  # vector_search, ai_fallback
    processing_time_ms = Column(Integer, default=0)
    user_confirmed_category = Column(String(255), nullable=True)

    # Learning taxonomy match information
    learning_item_id = Column(String(100), nullable=True)  # ID from vector store
    learning_level_1 = Column(String(255), nullable=True)  # Level 1 category match
    learning_level_2 = Column(String(255), nullable=True)  # Level 2 category match
    learning_level_3 = Column(String(255), nullable=True)  # Level 3 category match
    learning_category_path = Column(String(500), nullable=True)  # Full path: level1 > level2 > level3
    learning_match_level = Column(String(10), nullable=True)  # Which level matched: "level_1", "level_2", "level_3"
    learning_confidence = Column(DECIMAL(5,4), nullable=True)  # Confidence from learning system

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

class LearningCategorySeller(Base):
    """
    Maps sellers to learning categories using AI analysis.
    
    Links sellers with their simple categories to the sophisticated 3-level learning
    categorization system, enabling semantic search and better matching.
    """
    __tablename__ = "learning_category_sellers"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id = Column(CHAR(36), ForeignKey("sellers.seller_id"), nullable=False)
    learning_category_id = Column(CHAR(36), ForeignKey("learning_categories.id"), nullable=False)
    original_seller_category = Column(String(255), nullable=False)  # Original simple category from seller
    confidence_score = Column(DECIMAL(5,4), default=0.0)  # AI confidence in this mapping
    ai_reasoning = Column(Text, nullable=True)  # OpenAI explanation for the mapping
    mapping_method = Column(String(50), default="openai_analysis")  # openai_analysis, manual, etc.
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())
    
    # Relationships
    seller = relationship("Seller")
    learning_category = relationship("LearningCategory")


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

# ========================================
# SELLER RECOMMENDATION SYSTEM MODELS
# ========================================

class SellerRanking(enum.Enum):
    """Seller ranking categories for prioritization."""
    Diamond = "Diamond"
    Platinum = "Platinum"
    Gold = "Gold"
    Titanium = "Titanium"

class NotificationType(enum.Enum):
    """Types of RFQ notifications sent to sellers."""
    initial_notification = "initial_notification"
    reminder = "reminder"

class ResponseType(enum.Enum):
    """Seller response types to RFQ notifications."""
    credit_purchase = "credit_purchase"
    rfq_request = "rfq_request"
    ignored = "ignored"

class InteractionType(enum.Enum):
    """Types of seller-RFQ interactions."""
    notification_sent = "notification_sent"
    credit_check = "credit_check"
    payment_initiated = "payment_initiated"
    rfq_requested = "rfq_requested"
    email_sent = "email_sent"

class SubscriptionPlan(enum.Enum):
    """Available subscription plans for sellers."""
    basic = "basic"
    pro = "pro"

class JobStatus(enum.Enum):
    """Status of background categorization jobs."""
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"

class Seller(Base):
    """
    Seller model for managing seller profiles in the recommendation system.
    
    Stores seller information, categories, location, subscription status, and 
    performance metrics for the RFQ notification system.
    """
    __tablename__ = "sellers"
    
    seller_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_name = Column(String(255), nullable=False)
    phone_number = Column(String(15), unique=True, nullable=False)
    email = Column(String(255), nullable=True)
    categories = Column(JSON, nullable=False)  # Simple array: ["Electronics", "Medical Equipment"]
    location = Column(JSON, nullable=False)    # {lat: 12.97, lng: 77.59, city: "Bangalore", state: "Karnataka"}
    geographic_coverage_km = Column(Integer, default=200)
    subscription_credits = Column(Integer, default=0)
    ranking = Column(Enum(SellerRanking), default=SellerRanking.Gold)
    last_active_at = Column(TIMESTAMP, nullable=True)
    opted_out_notifications = Column(Boolean, nullable=True, default=None)

    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())
    
    # Relationships
    notifications = relationship("RFQSellerNotification", back_populates="seller")
    interactions = relationship("SellerRFQInteraction", back_populates="seller")
    subscriptions = relationship("SellerSubscription", back_populates="seller")
    # learning_mappings removed - now handled by SellerDataAdapter for remote sellers

class RFQSellerNotification(Base):
    """
    Track RFQ notifications sent to sellers.
    
    Records when notifications are sent, delivered, read, and responded to
    for rate limiting and performance tracking.
    """
    __tablename__ = "rfq_seller_notifications"
    
    notification_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rfq_id = Column(CHAR(36), nullable=False)
    seller_id = Column(CHAR(36), ForeignKey("sellers.seller_id"), nullable=False)
    notification_type = Column(Enum(NotificationType), default=NotificationType.initial_notification)
    sent_at = Column(TIMESTAMP, default=func.current_timestamp())
    delivered_at = Column(TIMESTAMP, nullable=True)
    read_at = Column(TIMESTAMP, nullable=True)
    responded_at = Column(TIMESTAMP, nullable=True)
    response_type = Column(Enum(ResponseType), nullable=True)
    
    # Relationships
    seller = relationship("Seller", back_populates="notifications")

class SellerRFQInteraction(Base):
    """
    Track detailed seller-RFQ interactions and conversation flow.
    
    Records each step in the seller notification and conversation process
    for analytics and debugging.
    """
    __tablename__ = "seller_rfq_interactions"
    
    interaction_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id = Column(CHAR(36), ForeignKey("sellers.seller_id"), nullable=False)
    rfq_id = Column(CHAR(36), nullable=True)
    session_id = Column(String(255), nullable=True)
    interaction_type = Column(Enum(InteractionType), nullable=False)
    interaction_data = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    seller = relationship("Seller", back_populates="interactions")

class SellerSubscription(Base):
    """
    Track seller subscription plans and credit usage.
    
    Manages seller subscription status, credit balances, and plan details
    for the RFQ notification system.
    """
    __tablename__ = "seller_subscriptions"
    
    subscription_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id = Column(CHAR(36), ForeignKey("sellers.seller_id"), nullable=False)
    plan_type = Column(Enum(SubscriptionPlan), nullable=False)
    credits_purchased = Column(Integer, nullable=False)
    credits_remaining = Column(Integer, nullable=False)
    purchased_at = Column(TIMESTAMP, default=func.current_timestamp())
    expires_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    seller = relationship("Seller", back_populates="subscriptions")

class SystemConfiguration(Base):
    """
    Store configurable system parameters for the seller recommendation system.
    
    Allows runtime configuration of business rules, thresholds, and
    operational parameters without code changes.
    """
    __tablename__ = "system_configurations"
    
    config_key = Column(String(100), primary_key=True)
    config_value = Column(JSON, nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())

class MockRFQ(Base):
    """
    Mock RFQ data for testing the seller recommendation system.
    
    Provides test RFQ data with categories, locations, and other
    attributes needed for seller selection testing.
    """
    __tablename__ = "mock_rfqs"
    
    rfq_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rfq_title = Column(String(500), nullable=False)
    rfq_description = Column(Text, nullable=True)
    categories = Column(JSON, nullable=False)       # ["Electronics", "Medical Equipment"]
    delivery_location = Column(JSON, nullable=False)  # {lat, lng, city, state}
    quantity_info = Column(String(255), nullable=True)
    deadline = Column(Date, nullable=True)
    division = Column(String(100), nullable=True)
    status = Column(Enum(RFQStatus), default=RFQStatus.ready)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

# ========================================
# OFFLINE CATEGORIZATION SYSTEM MODELS  
# ========================================

class SellerLearningMapping(Base):
    """
    Map sellers to 3-level learning categories through offline OpenAI processing.

    Links seller's simple categories to the sophisticated 3-level learning
    categorization system for enhanced matching capabilities.
    """
    __tablename__ = "seller_learning_mappings"

    mapping_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id = Column(CHAR(36), nullable=False)  # References remote seller, no FK constraint
    original_category = Column(String(255), nullable=False)  # Original simple category from seller
    learning_category_id = Column(CHAR(36), ForeignKey("learning_categories.id"), nullable=True)
    level_1_category = Column(String(255), nullable=True)
    level_2_category = Column(String(255), nullable=True)
    level_3_category = Column(String(255), nullable=True)
    confidence_score = Column(DECIMAL(5,4), default=0.0)
    ai_reasoning = Column(Text, nullable=True)
    mapping_method = Column(String(50), default="openai_analysis")
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp())
    
    # Relationships (seller removed since it references remote data)
    learning_category = relationship("LearningCategory")

class SellerCategorizationJob(Base):
    """
    Track background jobs for seller categorization processing.
    
    Manages the offline process of mapping sellers to the 3-level
    learning categorization system using OpenAI.
    """
    __tablename__ = "seller_categorization_jobs"
    
    job_id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    seller_id = Column(CHAR(36), ForeignKey("sellers.seller_id"), nullable=False)
    job_status = Column(Enum(JobStatus), default=JobStatus.pending)
    input_categories = Column(JSON, nullable=False)  # Original seller categories
    output_mappings = Column(JSON, nullable=True)    # Generated 3-level mappings
    processing_time_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    completed_at = Column(TIMESTAMP, nullable=True)
    
    # Relationships
    seller = relationship("Seller")

class BuyerDailyMetrics(Base):
    """
    Daily buyer metrics table for joined buyer data analytics.
    
    Stores comprehensive daily buyer activity metrics including RFQ counts,
    registration status, chat activity, and organizational information.
    """
    __tablename__ = "buyer_daily_metrics"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    session_id = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone_number = Column(String(15), nullable=True)
    total_rfq_raised = Column(Integer, default=0)
    total_items_in_rfqs = Column(Integer, default=0)
    total_distinct_categories_in_rfq = Column(Integer, default=0)
    total_incomplete_rfq = Column(Integer, default=0)
    buyers_started_but_not_raised_rfq = Column(Integer, default=0)
    failed_registration = Column(Integer, default=0)
    successfully_registered = Column(Integer, default=0)
    number_of_chats = Column(Integer, default=0)
    successful_rfqs_ai = Column(Integer, default=0)
    avg_products_per_rfq = Column(DECIMAL(5,2), nullable=True)
    avg_categories_per_rfq = Column(DECIMAL(5,2), nullable=True)
    bfs_searches= Column(Integer, default=0)
    products_bid_for= Column(Integer, default=0)
    no_of_products_searched= Column(Integer, default=0)
    rfq_response_count= Column(Integer,default=0)
    bfs_stock_products_bid_placed_count= Column(Integer,default=0)
    bfs_products_searched_list = Column(JSON,nullable=True)
    org_id = Column(String(255), nullable=True)
    uuid = Column(String(255), nullable=True)
    ai_reasoning = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('date', 'email', 'phone_number','session_id', name='unique_buyer_daily_metrics'),
    )

class SellerDailyMetrics(Base):
    """
    Daily seller metrics table for joined seller data analytics.
    
    Stores comprehensive daily seller activity metrics including RFQ requests,
    registration status, chat activity, and subscription information.
    """
    __tablename__ = "seller_daily_metrics"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    session_id = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone_number = Column(String(15), nullable=True)
    number_of_chats = Column(Integer, default=0)
    seller_failed_registration = Column(Integer, default=0)
    seller_successful_registration = Column(Integer, default=0)
    ai_reasoning = Column(Text, nullable=True)
    rfq_requested_ai = Column(Integer, default=0)
    rfq_response_ai = Column(Integer, default=0)
    subscription_plans_requested = Column(Integer, default=0)
    zero_credit_rfq_attempt = Column(Integer, default=0)
    org_id = Column(String(255), nullable=True)
    uuid = Column(String(255), nullable=True)
    total_rfqs_requested = Column(Integer, default=0)  # From joined query
    bids_accepted_ai = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('date', 'email', 'phone_number','session_id', name='unique_seller_daily_metrics'),
    )

class MetricMaster(Base):
    """
    Master table for metric definitions and formulas.
    
    Stores metric definitions, descriptions, formulas, and active status
    for standardized metric calculation and reporting.
    """
    __tablename__ = "metric_master"
    
    s_no = Column(Integer, primary_key=True, autoincrement=True)
    metric = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    formula = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

class DailyAggregates(Base):
    """
    Daily aggregates table for storing calculated metrics by role.
    
    Stores aggregated metrics calculated from base tables like buyer_daily_metrics
    with date, role, metric_name, and value structure.
    """
    __tablename__ = "daily_aggregates"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    role = Column(String(50), nullable=False, index=True)
    metric_s_no = Column(Integer, ForeignKey("metric_master.s_no"), nullable=True)
    metric_name = Column(String(255), nullable=False, index=True)
    value = Column(DECIMAL(15,4), nullable=False)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    # Relationships
    metric_master = relationship("MetricMaster")
    
    __table_args__ = (
        UniqueConstraint('date', 'role', 'metric_name', name='unique_daily_aggregate'),
    )

class CategoryAggregates(Base):
    """
    Category aggregates table for storing daily RFQ category metrics.
    
    Stores daily category-wise RFQ counts including total RFQs raised
    and RFQs with quotations for category performance analysis.
    """
    __tablename__ = "category_aggregates"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    category_name = Column(String(255), nullable=False, index=True)
    total_rfq_raised_category = Column(Integer, default=0)
    total_rfqs_with_quotations = Column(Integer, default=0)
    total_rfqs_intimated = Column(Integer, default=0)
    bids_requested = Column(Integer, default=0)
    bids_accepted = Column(Integer, default=0)
    bfs_products_searched_count = Column(Integer, default=0)
    bfs_products_searched_by_unregistered_count = Column(Integer, default=0)
    bfs_counter_offer_by_buyer= Column(Integer, default=0)
    bfs_counter_offer_accepted_by_seller= Column(Integer, default=0)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('date', 'category_name', name='unique_category_aggregate'),
    )

class UnknownDailyMetrics(Base):
    """
    Daily unknown user metrics table for users with no buyer/seller activity.
    
    Stores daily metrics for users who had conversations but showed no clear
    buyer or seller behavior patterns.
    """
    __tablename__ = "unknown_daily_metrics"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    session_id = Column(String(255), nullable=False)
    phone_number = Column(String(15), nullable=True)
    user_type = Column(String(50), default='unknown')
    email = Column(String(255), nullable=True)
    confidence_score = Column(DECIMAL(5,2), nullable=True)
    ai_reasoning = Column(Text, nullable=True)
    unregistered_seller_initiated_chat = Column(Integer, default=0)
    unregistered_seller_requested_rfq = Column(Integer, default=0)
    unregistered_buyer_bfs_only = Column(Integer, default=0)
    number_of_faq_or_general_queries = Column(Integer, default=0)
    bfs_products_searched_by_unregistered = Column(JSON,nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('date', 'session_id', 'phone_number', name='unique_unknown_daily_metrics'),
    )

class RFQNotificationFact(Base):
    """
    RFQ notification fact table for tracking seller notifications and responses.
    
    Stores comprehensive data about RFQ notifications sent to sellers including
    notification timing, seller responses, and AI reasoning for analytics.
    """
    __tablename__ = "rfq_notification_fact"
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    date = Column(Date, nullable=False, index=True)
    session_id = Column(String(255), nullable=False, index=True)
    rfq_id = Column(CHAR(36), nullable=False, index=True)
    seller_id = Column(CHAR(36), nullable=False, index=True)
    category = Column(String(255), nullable=True, index=True)
    rfq_notified_at = Column(TIMESTAMP, nullable=True)
    seller_response_at = Column(TIMESTAMP, nullable=True)
    ai_reasoning = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=func.current_timestamp())
    
    __table_args__ = (
        UniqueConstraint('date', 'session_id', 'rfq_id', 'seller_id', 'category', name='unique_rfq_notification_fact'),
    )