"""
Database connection and session management for the AI Procurement Agent.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
import json
import logging

from .config import get_settings
from .models import Base, ProductCategory, Vendor, ConversationSession
from .utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

engine = None
SessionLocal = None

# Remote database connection for item categorization
remote_engine = None
RemoteSessionLocal = None


def init_database():
    """Initialize database tables and create sample data."""
    global engine, SessionLocal

    settings = get_settings()
    
    # SSL configuration handled in connection URL
    connect_args = {}
    
    engine = create_engine(
        settings.get_database_url(),
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=300
    )
    SessionLocal = sessionmaker(bind=engine)

    # Create all tables
    Base.metadata.create_all(bind=engine)

    # Add sample data
    db = SessionLocal()
    try:
        # Check if data exists
        if db.query(Vendor).count() == 0:
            # Add sample categories from actual BFS API
            categories = [
                "Agriculture Equipments",
                "Air Logistics",
                "Automotive",
                "Batteries & UPS",
                "Bearings & Accessories",
                "Building Works",
                "Cables",
                "CCTV & BMS",
            ]
            for cat in categories:
                db.add(ProductCategory(category_name=cat))

            # Add realistic vendors with API-based categories
            vendors = [
                Vendor(
                    vendor_name="Chennai Motors & Equipment",
                    geographic_coverage=["Chennai", "Bangalore", "Coimbatore"],
                    vendor_services=[
                        "Agriculture Equipments",
                        "Automotive",
                        "Bearings & Accessories",
                    ],
                ),
                Vendor(
                    vendor_name="Mumbai Industrial Solutions",
                    geographic_coverage=["Mumbai", "Pune", "Nashik"],
                    vendor_services=["Air Logistics", "Cables", "Building Works"],
                ),
                Vendor(
                    vendor_name="Delhi Safety Systems",
                    geographic_coverage=["Delhi", "Gurgaon", "Noida"],
                    vendor_services=["CCTV & BMS", "Batteries & UPS"],
                ),
                Vendor(
                    vendor_name="Hyderabad Tech Solutions",
                    geographic_coverage=["Hyderabad", "Secunderabad", "Warangal"],
                    vendor_services=["Agriculture Equipments", "CCTV & BMS", "Cables"],
                ),
                Vendor(
                    vendor_name="Kolkata Engineering Works",
                    geographic_coverage=["Kolkata", "Durgapur", "Asansol"],
                    vendor_services=[
                        "Automotive",
                        "Building Works",
                        "Bearings & Accessories",
                    ],
                ),
            ]
            for vendor in vendors:
                db.add(vendor)

            db.commit()
    finally:
        db.close()


def get_db_session():
    """Get database session."""
    global engine, SessionLocal
    if SessionLocal is None:
        # Initialize with SSL configuration based on database mode
        settings = get_settings()
        
        # SSL configuration handled in connection URL
        connect_args = {}
        
        engine = create_engine(
            settings.get_database_url(),
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_recycle=300
        )
        SessionLocal = sessionmaker(bind=engine)
        
    return SessionLocal()


def get_remote_db_session():
    """Get remote database session for item categorization."""
    global remote_engine, RemoteSessionLocal
    
    settings = get_settings()
    
    # Check if remote categorization is enabled
    if not settings.enable_remote_categorization:
        raise ValueError("Remote categorization is not enabled. Set ENABLE_REMOTE_CATEGORIZATION=true in .env")
    
    if RemoteSessionLocal is None:
        remote_database_url = settings.get_remote_database_url()
        
        if not remote_database_url:
            raise ValueError("Remote database URL not configured")
        
        # Create remote engine with connection pooling
        remote_engine = create_engine(
            remote_database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=10,
            echo=settings.sql_debug
        )
        
        RemoteSessionLocal = sessionmaker(bind=remote_engine)
        
        logger.info("Remote database connection initialized for item categorization")
        
    return RemoteSessionLocal()


def execute_remote_query(query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """
    Execute a query on the remote database and return results as dictionaries.
    
    Args:
        query: SQL query to execute
        params: Query parameters (optional)
        
    Returns:
        List of dictionaries with column names as keys
    """
    db = get_remote_db_session()
    try:
        result = db.execute(text(query), params or {})
        # Convert result to list of dictionaries
        columns = result.keys()
        return [dict(zip(columns, row)) for row in result.fetchall()]
    except Exception as e:
        logger.error(f"Remote query execution failed: {e}")
        raise
    finally:
        db.close()


def get_remote_item_categories(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get item categories from remote database for auto-categorization.
    
    Args:
        limit: Optional limit on number of records
        
    Returns:
        List of item category records with uuid, category, item, division, serial_no
    """
    query = """
        SELECT uuid, category, item, division, serial_no, 
               created_by, created_ts, last_modified_by, last_modified_ts
        FROM item_category 
        ORDER BY serial_no
    """
    
    params = {}
    if limit:
        query += " LIMIT :limit"
        params['limit'] = limit
    
    return execute_remote_query(query, params)


def get_remote_category_stats() -> Dict[str, Any]:
    """
    Get statistics about the remote item_category table.
    
    Returns:
        Dictionary with category statistics
    """
    stats = {}
    
    # Total count
    count_result = execute_remote_query("SELECT COUNT(*) as total FROM item_category")
    stats['total_items'] = count_result[0]['total'] if count_result else 0
    
    # Unique categories
    category_result = execute_remote_query(
        "SELECT COUNT(DISTINCT category) as unique_categories FROM item_category"
    )
    stats['unique_categories'] = category_result[0]['unique_categories'] if category_result else 0
    
    # Unique divisions
    division_result = execute_remote_query(
        "SELECT COUNT(DISTINCT division) as unique_divisions FROM item_category"
    )
    stats['unique_divisions'] = division_result[0]['unique_divisions'] if division_result else 0
    
    # Top categories
    stats['top_categories'] = execute_remote_query("""
        SELECT category, COUNT(*) as item_count 
        FROM item_category 
        GROUP BY category 
        ORDER BY item_count DESC 
        LIMIT 10
    """)
    
    return stats


def test_remote_connection() -> bool:
    """
    Test remote database connection.
    
    Returns:
        True if connection successful, False otherwise
    """
    try:
        settings = get_settings()
        if not settings.enable_remote_categorization:
            return False
            
        result = execute_remote_query("SELECT 1 as test")
        return len(result) > 0 and result[0].get('test') == 1
        
    except Exception as e:
        logger.error(f"Remote connection test failed: {e}")
        return False


class DatabaseManager:
    """
    Database manager class for advanced database operations.

    Provides utilities for database maintenance, monitoring,
    and administrative operations.
    """

    def __init__(self, session=None):
        self.session = session or get_db_session()

    def get_connection_pool_status(self):
        """
        Get current connection pool status and metrics.

        Returns dictionary with pool statistics and health information.
        """
        pass

    def execute_health_check(self):
        """
        Execute comprehensive database health check.

        Returns health status with detailed information.
        """
        pass

    def cleanup_expired_sessions(self, hours: int = None):
        """
        Clean up expired conversation sessions.

        Removes old conversation sessions and associated data
        to maintain database performance.
        """
        from .config import get_settings
        
        if hours is None:
            hours = get_settings().session_timeout_hours
        
        # Calculate cutoff time
        cutoff_time = utc_now().replace(tzinfo=None) - timedelta(hours=hours)
        
        # Query expired sessions
        expired_sessions = self.session.query(ConversationSession).filter(
            ConversationSession.created_at < cutoff_time,
            ConversationSession.outcome.is_(None)  # Only cleanup incomplete sessions
        ).all()
        
        # Update expired sessions to timeout outcome
        for session in expired_sessions:
            session.outcome = 'timeout'
            session.completed_at = utc_now().replace(tzinfo=None)
            # Clear workflow state to free up space
            session.workflow_state = {"extracted_entities": {}}
            session.conversation_history = {"messages": []}
            session.extracted_entities = {}
        
        # Commit changes
        self.session.commit()
        
        return len(expired_sessions)

    def cleanup_completed_sessions(self):
        """
        Clean up completed RFQ sessions to free up space.
        
        Clears workflow state and conversation history from sessions
        that have been completed successfully.
        """
        from .config import get_settings
        settings = get_settings()
        
        if not settings.cleanup_completed_sessions:
            return 0
            
        # Find completed sessions that still have data
        completed_sessions = self.session.query(ConversationSession).filter(
            ConversationSession.outcome == 'completed',
            ConversationSession.workflow_state.isnot(None)
        ).all()
        
        # Clear data from completed sessions
        for session in completed_sessions:
            # Keep minimal data for audit purposes
            session.workflow_state = {"extracted_entities": {}, "cleaned_up": True}
            session.conversation_history = {"messages": [], "cleaned_up": True}
            session.extracted_entities = {}
            # Mark as cleaned up
            flag_modified(session, 'workflow_state')
            flag_modified(session, 'conversation_history')
        
        # Commit changes
        self.session.commit()
        
        return len(completed_sessions)

    def backup_learning_data(self):
        """
        Create backup of learning records and vendor associations.

        Exports learning data for backup and analysis purposes.
        """
        pass

    def save_conversation_session(self, session_data: dict) -> ConversationSession:
        """Save or update a conversation session."""
        
        session = self.session.query(ConversationSession).filter_by(
            session_id=session_data['session_id']
        ).first()
        
        if session:
            # Update existing session
            for key, value in session_data.items():
                setattr(session, key, value)
                # For JSONB fields, explicitly mark as modified
                if key in ['workflow_state', 'conversation_history', 'extracted_entities', 'whatsapp_context', 'error_details', 'performance_metrics', 'bfs_products_searched', 'bfs_price_accepted', 'bfs_counter_offers', 'products_bid_for', 'bids_received', 'bids_accepted', 'counter_offers_made', 'counter_offers_accepted', 'rfqs_with_response']:
                    flag_modified(session, key)
        else:
            # Create new session
            session = ConversationSession(**session_data)
            self.session.add(session)
        
        self.session.commit()
        return session

    def get_conversation_session(self, session_id: str) -> Optional[ConversationSession]:
        """Get a conversation session by ID."""
        session = self.session.query(ConversationSession).filter_by(session_id=session_id).first()
        
        # Fix potential JSON deserialization issues
        if session and session.workflow_state:
            try:
                # Ensure workflow_state is properly deserialized as dict
                if isinstance(session.workflow_state, str):
                    session.workflow_state = json.loads(session.workflow_state)
            except (json.JSONDecodeError, TypeError) as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to deserialize workflow_state for session {session_id}: {e}")
                # Reset to empty dict to prevent further errors
                session.workflow_state = {"extracted_entities": []}
        
        return session