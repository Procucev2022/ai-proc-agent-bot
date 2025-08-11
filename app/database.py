"""
Database connection and session management for the AI Procurement Agent.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
import json

from .config import get_settings
from .models import Base, ProductCategory, Vendor, ConversationSession
from .utils.datetime_utils import utc_now

engine = None
SessionLocal = None


def init_database():
    """Initialize database tables and create sample data."""
    global engine, SessionLocal

    settings = get_settings()
    
    # Configure SSL connection args for Azure MySQL
    import os
    ssl_cert_path = os.path.abspath('DigiCertGlobalRootCA.crt.pem')
    connect_args = {
        'ssl_ca': ssl_cert_path,
        'ssl_disabled': False
    }
    
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
        # Initialize with SSL configuration
        settings = get_settings()
        
        # Configure SSL connection args for Azure MySQL
        import os
        ssl_cert_path = os.path.abspath('DigiCertGlobalRootCA.crt.pem')
        connect_args = {
            'ssl_ca': ssl_cert_path,
            'ssl_disabled': False
        }
        
        engine = create_engine(
            settings.get_database_url(),
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_recycle=300
        )
        SessionLocal = sessionmaker(bind=engine)
        
    return SessionLocal()


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
        return session
