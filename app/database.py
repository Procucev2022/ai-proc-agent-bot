"""
Database connection and session management for the AI Procurement Agent.
"""

import pymysql
pymysql.install_as_MySQLdb()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import SQLAlchemyError, DisconnectionError, TimeoutError as SQLTimeoutError
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
import json
import logging
import asyncio

from .config import get_settings
from .models import Base, ProductCategory, Vendor, ConversationSession, RFQNotificationFact
from .utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


def _get_ssl_connect_args(settings) -> dict:
    """Return SQLAlchemy connect_args with SSL config based on database_mode."""
    import os
    if settings.database_mode == "client":
        cert_paths = [
            "/app/ssl/DigiCertGlobalRootCA.crt.pem",
            "/data/procucev_project/procucev_proc_agent/ssl/DigiCertGlobalRootCA.crt.pem",
            os.path.join(settings.PROJECT_ROOT, "ssl/DigiCertGlobalRootCA.crt.pem"),
        ]
        existing_cert = next((p for p in cert_paths if os.path.exists(p)), None)
        if existing_cert:
            return {"ssl": {"ca": existing_cert}}
        return {"ssl": {"ssl_disabled": False, "ssl_check_hostname": False, "ssl_verify_cert": False}}
    else:
        return {"ssl": {"ssl_disabled": False, "ssl_check_hostname": False, "ssl_verify_cert": False}}


engine = None
SessionLocal = None

# Remote database connection for item categorization
remote_engine = None
RemoteSessionLocal = None


def _log_pool_status(context: str = ""):
    """
    Internal helper to log current connection pool status.

    Args:
        context: Context description for the log message (e.g., "after creating session")
    """
    global engine

    if not engine or not hasattr(engine, 'pool'):
        return

    try:
        pool = engine.pool
        checked_out = pool.checkedout()
        checked_in = pool.checkedin()
        total_size = pool.size()
        overflow = pool.overflow()

        # Debug: Check pool type and attributes
        pool_type = type(pool).__name__

        # SQLAlchemy overflow() returns: current_overflow - max_overflow
        # For QueuePool, this means:
        # - Negative values indicate unused overflow capacity
        # - Positive values indicate active overflow connections beyond pool_size
        # So: overflow = -8 means we have 8 overflow slots unused (out of max_overflow=20)
        #     This actually means: max_overflow(20) - current_overflow(12) = 8 available overflow slots
        #     Wait, that's wrong. Let me check the actual calculation:
        #     overflow() returns: len(self._overflow) which is the current number of overflow connections
        #     But in our case it's negative, which suggests it's returning overflow_used - max_overflow

        # Calculate actual available: pool_size - checked_out + overflow_available
        # If overflow is negative, it means we have overflow capacity available
        # The true available connections = (pool_size - checked_out) + overflow_capacity_used
        actual_available = total_size - checked_out

        logger.info(
            f"[DB-POOL] {context} | "
            f"Type: {pool_type} | "
            f"InUse: {checked_out} | "
            f"InPool: {checked_in} | "
            f"PoolSize: {total_size} | "
            f"Overflow: {overflow} | "
            f"Available: {actual_available} | "
            f"Status: {'⚠️ DEPLETED' if checked_in == 0 and checked_out >= total_size else '✓ OK'}"
        )
    except Exception as e:
        logger.debug(f"Failed to get pool status: {e}")


def log_connection_pool_status(context: str = "manual check"):
    """
    Public function to log current connection pool status.

    Can be called from anywhere in the application to monitor database connections.

    Args:
        context: Context description for the log message

    Example:
        from app.database import log_connection_pool_status
        log_connection_pool_status("before processing batch")
    """
    _log_pool_status(context)


def init_database():
    """Initialize database tables and create sample data."""
    global engine, SessionLocal

    settings = get_settings()

    connect_args = _get_ssl_connect_args(settings)

    engine = create_engine(
        settings.get_database_url(),
        connect_args=connect_args,
        pool_pre_ping=True,  # Test connections before using
        pool_recycle=3600,  # Recycle connections after 1 hour (MySQL timeout is 8h)
        pool_size=10,
        max_overflow=20,
        pool_timeout=60,
        echo_pool=False,  # Set to True for pool debugging
        isolation_level="READ COMMITTED"  # See latest committed data across workers
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    # Create all tables
    Base.metadata.create_all(bind=engine)

    # Log initial pool status
    logger.info("Database engine initialized successfully")
    _log_pool_status("after init_database engine creation")

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
        _log_pool_status("after init_database completion")


def get_db_session():
    """
    Get database session with error handling.

    Returns a new database session. Caller is responsible for closing the session.
    """
    global engine, SessionLocal

    if SessionLocal is None:
        # Initialize with SSL configuration based on database mode
        settings = get_settings()

        connect_args = _get_ssl_connect_args(settings)

        try:
            engine = create_engine(
                settings.get_database_url(),
                connect_args=connect_args,
                pool_pre_ping=False,  # Disabled for performance - rely on pool_recycle instead
                pool_recycle=1800,  # Recycle connections after 30 min (more frequent than before)
                pool_size=20,  # Increased from 10 - more ready connections
                max_overflow=10,  # Decreased from 20 - reduce connection creation overhead
                pool_timeout=30,  # Decreased from 60 - fail faster if pool exhausted
                echo_pool=False,
                isolation_level="READ COMMITTED"  # See latest committed data across workers
            )
            SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
            logger.info("Initialized database session factory (lazy init)")
        except Exception as e:
            logger.error(f"Database engine creation failed: {e}")
            # Import here to avoid circular imports
            from .services.global_error_handler import handle_database_error
            asyncio.create_task(handle_database_error(f"Database engine creation failed: {str(e)}"))
            raise

    try:
        # Return new session - caller must close it when done
        session = SessionLocal()
        # Note: Removed redundant SELECT 1 health check - pool_pre_ping already validates connections

        # Log connection pool status
        _log_pool_status("after creating session")

        logger.debug(f"Returning database session: {id(session)}")
        return session
    except (SQLAlchemyError, DisconnectionError, SQLTimeoutError) as e:
        logger.error(f"Database connection failed: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Database connection failed: {str(e)}"))
        raise
    except Exception as e:
        logger.error(f"Unexpected database error: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Unexpected database error: {str(e)}"))
        raise


@contextmanager
def get_db_session_context():
    """
    Context manager for database sessions - ensures proper cleanup.

    Usage:
        with get_db_session_context() as db:
            # use db session
            result = db.query(Model).all()
        # session automatically closed when exiting 'with' block

    This prevents database connection leaks by ensuring sessions are always closed.
    """
    session = get_db_session()
    try:
        yield session
        session.commit()  # Auto-commit on successful completion
    except Exception as e:
        session.rollback()  # Rollback on error
        logger.error(f"Database session error, rolling back: {e}")
        raise
    finally:
        session.close()  # Always close session
        logger.debug(f"Closed database session: {id(session)}")
        _log_pool_status("after closing session")


def get_remote_db_session():
    """Get remote database session for item categorization with error handling."""
    global remote_engine, RemoteSessionLocal
    
    settings = get_settings()
    
    # Check if remote categorization is enabled
    if not settings.enable_remote_categorization:
        raise ValueError("Remote categorization is not enabled. Set ENABLE_REMOTE_CATEGORIZATION=true in .env")
    
    if RemoteSessionLocal is None:
        remote_database_url = settings.get_remote_database_url()
        
        if not remote_database_url:
            raise ValueError("Remote database URL not configured")
        
        connect_args = _get_ssl_connect_args(settings)

        try:
            # Create remote engine with connection pooling
            remote_engine = create_engine(
                remote_database_url,
                connect_args=connect_args,
                pool_pre_ping=True,  # Test connections before using
                pool_recycle=3600,  # Recycle connections after 1 hour
                pool_size=5,
                max_overflow=10,
                echo=settings.sql_debug
            )
            
            RemoteSessionLocal = sessionmaker(bind=remote_engine, autoflush=False, autocommit=False)
            
            logger.info("Remote database connection initialized for item categorization")
        except Exception as e:
            logger.error(f"Remote database engine creation failed: {e}")
            # Import here to avoid circular imports
            from .services.global_error_handler import handle_database_error
            asyncio.create_task(handle_database_error(f"Remote database engine creation failed: {str(e)}"))
            raise
        
    try:
        session = RemoteSessionLocal()
        # Test connection
        session.execute(text("SELECT 1"))
        return session
    except (SQLAlchemyError, DisconnectionError, SQLTimeoutError) as e:
        logger.error(f"Remote database connection failed: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Remote database connection failed: {str(e)}"))
        raise
    except Exception as e:
        logger.error(f"Unexpected remote database error: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Unexpected remote database error: {str(e)}"))
        raise


def execute_remote_query(query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """
    Execute a query on the remote database and return results as dictionaries.
    
    Args:
        query: SQL query to execute
        params: Query parameters (optional)
        
    Returns:
        List of dictionaries with column names as keys
    """
    db = None
    try:
        db = get_remote_db_session()
        result = db.execute(text(query), params or {})
        # Convert result to list of dictionaries
        columns = result.keys()
        return [dict(zip(columns, row)) for row in result.fetchall()]
    except (SQLAlchemyError, DisconnectionError, SQLTimeoutError) as e:
        logger.error(f"Remote query execution failed: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Remote query execution failed: {str(e)}"))
        raise
    except Exception as e:
        logger.error(f"Unexpected error in remote query execution: {e}")
        # Import here to avoid circular imports
        from .services.global_error_handler import handle_database_error
        asyncio.create_task(handle_database_error(f"Unexpected error in remote query execution: {str(e)}"))
        raise
    finally:
        if db:
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

    Usage:
        # Option 1: Context manager (recommended - auto cleanup)
        with DatabaseManager() as db_manager:
            db_manager.cleanup_expired_sessions()

        # Option 2: Manual cleanup
        db_manager = DatabaseManager()
        try:
            db_manager.cleanup_expired_sessions()
        finally:
            db_manager.close()

        # Option 3: Share existing session (no auto-close)
        with get_db_session_context() as db:
            db_manager = DatabaseManager(session=db)
            db_manager.cleanup_expired_sessions()
            # Session closed by context manager
    """

    def __init__(self, session=None):
        """
        Initialize DatabaseManager.

        Args:
            session: Optional database session. If not provided, creates a new one.
                    When session is provided, DatabaseManager will NOT close it.
                    When session is None, DatabaseManager creates and owns the session,
                    and MUST close it via close() or context manager.
        """
        self._owns_session = session is None
        self.session = session or get_db_session()

        if self._owns_session:
            logger.debug(f"DatabaseManager created and owns session: {id(self.session)}")
        else:
            logger.debug(f"DatabaseManager using provided session: {id(self.session)}")

    def close(self):
        """
        Close the database session if we own it.

        This should be called when done using DatabaseManager to prevent
        connection leaks. Alternatively, use DatabaseManager as a context manager.
        """
        if self._owns_session and self.session:
            try:
                self.session.close()
                logger.debug(f"DatabaseManager closed owned session: {id(self.session)}")
                _log_pool_status("after DatabaseManager.close()")
            except Exception as e:
                logger.error(f"Error closing DatabaseManager session: {e}")
            finally:
                self.session = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures session is closed."""
        self.close()
        return False  # Don't suppress exceptions

    def __del__(self):
        """Destructor - cleanup session if still open."""
        if self._owns_session and self.session:
            logger.warning(f"DatabaseManager being garbage collected with unclosed session: {id(self.session)}. Use context manager or call close() explicitly.")
            self.close()

    def get_connection_pool_status(self):
        """
        Get current connection pool status and metrics.

        Returns dictionary with pool statistics and health information.
        """
        global engine, remote_engine

        status = {}

        # Main database pool status
        if engine and hasattr(engine, 'pool'):
            pool = engine.pool
            status['main_db'] = {
                'size': pool.size(),
                'checked_in': pool.checkedin(),
                'checked_out': pool.checkedout(),
                'overflow': pool.overflow(),
                'invalid': pool.invalid(),
                'pool_status': 'healthy' if pool.checkedin() > 0 else 'depleted'
            }
        else:
            status['main_db'] = {'status': 'not_initialized'}

        # Remote database pool status
        if remote_engine and hasattr(remote_engine, 'pool'):
            pool = remote_engine.pool
            status['remote_db'] = {
                'size': pool.size(),
                'checked_in': pool.checkedin(),
                'checked_out': pool.checkedout(),
                'overflow': pool.overflow(),
                'invalid': pool.invalid(),
                'pool_status': 'healthy' if pool.checkedin() > 0 else 'depleted'
            }
        else:
            status['remote_db'] = {'status': 'not_initialized'}

        return status

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
        from app.models import ConversationOutcome
        for session in expired_sessions:
            session.outcome = ConversationOutcome.timeout
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

    def append_session_data(self, session_data: dict) -> ConversationSession:
        """
        Append session data to existing database record (preserves history across multiple workflows).

        This method loads existing session and:
        1. APPENDS to array fields (conversation_history, rfq_ids, product_items, etc.)
        2. MERGES object fields (workflow_state, extracted_entities, etc.)
        3. UPDATES scalar fields (outcome, completed_at, etc.)

        Use for: timeout, exit, completion to maintain complete audit trail.

        Args:
            session_data: New session data to append/merge

        Returns:
            Updated ConversationSession with merged data
        """
        from sqlalchemy.exc import SQLAlchemyError
        import os

        worker_pid = os.getpid()
        session_id = session_data.get('session_id', 'UNKNOWN')

        try:
            # Load existing session from database
            existing_session = self.session.query(ConversationSession).filter_by(
                session_id=session_id
            ).first()

            if not existing_session:
                # No existing session, just save as new
                logger.info(f"[WORKER-{worker_pid}] [SESSION-APPEND] No existing session for {session_id}, creating new")
                return self.save_conversation_session(session_data)

            logger.info(f"[WORKER-{worker_pid}] [SESSION-APPEND] Appending to existing session {session_id}")

            # APPEND conversation_history messages with deduplication
            existing_history = existing_session.conversation_history or {"messages": [], "metadata": [], "openai_messages": []}
            new_history = session_data.get('conversation_history', {})

            if new_history:
                # Deduplicate messages based on timestamp + content to prevent duplicates
                def deduplicate_messages(existing_msgs, new_msgs):
                    """Deduplicate messages using timestamp + content/role as unique key."""
                    import json
                    # Create set of existing message signatures
                    existing_sigs = set()
                    for msg in existing_msgs:
                        # Use timestamp + content + role as unique signature
                        # Convert content to JSON string if it's a dict to make it hashable
                        content = msg.get('content')
                        if isinstance(content, dict):
                            content = json.dumps(content, sort_keys=True)
                        sig = (
                            msg.get('timestamp'),
                            content,
                            msg.get('role')
                        )
                        existing_sigs.add(sig)

                    # Only add messages that don't already exist
                    unique_new = []
                    for msg in new_msgs:
                        # Convert content to JSON string if it's a dict to make it hashable
                        content = msg.get('content')
                        if isinstance(content, dict):
                            content = json.dumps(content, sort_keys=True)
                        sig = (
                            msg.get('timestamp'),
                            content,
                            msg.get('role')
                        )
                        if sig not in existing_sigs:
                            unique_new.append(msg)
                            existing_sigs.add(sig)  # Prevent duplicates within new_msgs too

                    return existing_msgs + unique_new

                # Deduplicate each message array type
                existing_messages = existing_history.get("messages", [])
                new_messages = new_history.get("messages", [])
                merged_messages = deduplicate_messages(existing_messages, new_messages)

                existing_metadata = existing_history.get("metadata", [])
                new_metadata = new_history.get("metadata", [])
                merged_metadata = deduplicate_messages(existing_metadata, new_metadata)

                # For openai_messages, deduplicate by content + role only (no timestamp)
                import json
                existing_openai = existing_history.get("openai_messages", [])
                new_openai = new_history.get("openai_messages", [])
                # Convert dict content to JSON string for hashing
                openai_sigs = set()
                for m in existing_openai:
                    content = m.get('content')
                    if isinstance(content, dict):
                        content = json.dumps(content, sort_keys=True)
                    openai_sigs.add((content, m.get('role')))

                unique_openai = []
                for m in new_openai:
                    content = m.get('content')
                    if isinstance(content, dict):
                        content = json.dumps(content, sort_keys=True)
                    if (content, m.get('role')) not in openai_sigs:
                        unique_openai.append(m)
                        openai_sigs.add((content, m.get('role')))

                merged_openai = existing_openai + unique_openai

                merged_history = {
                    "messages": merged_messages,
                    "metadata": merged_metadata,
                    "openai_messages": merged_openai
                }

                existing_session.conversation_history = merged_history
                flag_modified(existing_session, 'conversation_history')

                new_msg_count = len(merged_messages) - len(existing_messages)
                logger.info(f"[SESSION-APPEND] Appended {new_msg_count} unique messages (total: {len(merged_messages)}, deduplicated: {len(new_messages) - new_msg_count})")

            # APPEND array fields (avoid duplicates for IDs)
            array_fields = ['rfq_ids', 'product_items', 'bfs_products_searched', 'bfs_price_accepted', 'bfs_counter_offers',
                           'products_bid_for', 'bids_received', 'bids_accepted', 'counter_offers_made',
                           'counter_offers_accepted', 'rfqs_with_response', 'seller_responses']

            for field in array_fields:
                if field in session_data and session_data[field]:
                    existing_array = getattr(existing_session, field, None) or []
                    new_array = session_data[field] if isinstance(session_data[field], list) else [session_data[field]]

                    # For ID fields, avoid duplicates; for data fields, append all
                    if field in ['rfq_ids', 'rfqs_with_response']:
                        merged_array = existing_array + [item for item in new_array if item and item not in existing_array]
                    else:
                        merged_array = existing_array + new_array

                    setattr(existing_session, field, merged_array)
                    flag_modified(existing_session, field)
                    logger.info(f"[SESSION-APPEND] {field}: {len(existing_array)} -> {len(merged_array)}")

            # MERGE object fields (workflow_state, extracted_entities, etc.)
            object_fields = ['workflow_state', 'extracted_entities', 'rfq_metadata', 'interaction_metrics', 'whatsapp_context']

            for field in object_fields:
                if field in session_data and session_data[field]:
                    existing_obj = getattr(existing_session, field, None) or {}
                    new_obj = session_data[field]
                    merged_obj = {**existing_obj, **new_obj}  # New values override old
                    setattr(existing_session, field, merged_obj)
                    flag_modified(existing_session, field)

            # UPDATE scalar fields (latest values)
            scalar_fields = ['workflow_type', 'outcome', 'completed_at', 'last_activity_at', 'user_type', 'session_state']

            for field in scalar_fields:
                if field in session_data and session_data[field] is not None:
                    setattr(existing_session, field, session_data[field])

            # INCREMENT counters
            if 'bfs_search_count' in session_data and session_data['bfs_search_count']:
                existing_session.bfs_search_count = (existing_session.bfs_search_count or 0) + session_data['bfs_search_count']

            # Commit changes
            self.session.commit()
            self.session.refresh(existing_session)

            logger.info(f"[WORKER-{worker_pid}] [SESSION-APPEND] Successfully appended data to {session_id}")
            return existing_session

        except SQLAlchemyError as e:
            logger.error(f"Database error in append_session_data for {session_id}: {e}")
            self.session.rollback()
            # Fallback to regular save
            return self.save_conversation_session(session_data)

    def save_conversation_session(self, session_data: dict) -> ConversationSession:
        """Save or update a conversation session (REPLACES existing data)."""
        from sqlalchemy.exc import SQLAlchemyError, IntegrityError
        import os

        worker_pid = os.getpid()
        session_id = session_data.get('session_id', 'UNKNOWN')

        # Log what we're saving
        extracted_entities = session_data.get('extracted_entities', [])
        workflow_state_keys = list(session_data.get('workflow_state', {}).keys()) if isinstance(session_data.get('workflow_state'), dict) else []
        logger.info(f"[WORKER-{worker_pid}] [SESSION-SAVE] {session_id} | extracted_entities count: {len(extracted_entities)} | workflow_state keys: {workflow_state_keys}")

        try:
            # First, try to get existing session
            session = self.session.query(ConversationSession).filter_by(
                session_id=session_data['session_id']
            ).first()
            
            if session:
                # Update existing session
                for key, value in session_data.items():
                    if key != 'session_id':  # Don't update primary key
                        setattr(session, key, value)
                    # For JSONB fields, explicitly mark as modified
                    if key in ['workflow_state', 'conversation_history', 'extracted_entities', 'whatsapp_context', 'error_details', 'performance_metrics', 'bfs_products_searched', 'bfs_price_accepted', 'bfs_counter_offers', 'products_bid_for', 'bids_received', 'bids_accepted', 'counter_offers_made', 'counter_offers_accepted', 'rfqs_with_response']:
                        flag_modified(session, key)
            else:
                # Create new session
                session = ConversationSession(**session_data)
                self.session.add(session)
            
            self.session.commit()
            # Refresh to ensure we return the latest state
            self.session.refresh(session)
            return session
            
        except IntegrityError as e:
            # Handle duplicate key error specifically
            logger.error(f"Duplicate session detected, updating existing: {e}")
            self.session.rollback()
            
            # Fetch and update existing session
            existing_session = self.session.query(ConversationSession).filter_by(
                session_id=session_data['session_id']
            ).first()
            
            if existing_session:
                # Update existing session
                for key, value in session_data.items():
                    if key != 'session_id':  # Don't update primary key
                        setattr(existing_session, key, value)
                    if key in ['workflow_state', 'conversation_history', 'extracted_entities', 'whatsapp_context', 'error_details', 'performance_metrics', 'bfs_products_searched', 'bfs_price_accepted', 'bfs_counter_offers', 'products_bid_for', 'bids_received', 'bids_accepted', 'counter_offers_made', 'counter_offers_accepted', 'rfqs_with_response']:
                        flag_modified(existing_session, key)
                self.session.commit()
                self.session.refresh(existing_session)
                return existing_session
            else:
                # Fallback: return session object without saving
                return ConversationSession(**session_data)
                
        except SQLAlchemyError as e:
            logger.error(f"Database error in save_conversation_session: {e}")
            self.session.rollback()
            
            # Try to fetch existing session after rollback
            try:
                existing_session = self.session.query(ConversationSession).filter_by(
                    session_id=session_data['session_id']
                ).first()
                
                if existing_session:
                    # Update existing session
                    for key, value in session_data.items():
                        if key != 'session_id':  # Don't update primary key
                            setattr(existing_session, key, value)
                        if key in ['workflow_state', 'conversation_history', 'extracted_entities', 'whatsapp_context', 'error_details', 'performance_metrics', 'bfs_products_searched', 'bfs_price_accepted', 'bfs_counter_offers', 'products_bid_for', 'bids_received', 'bids_accepted', 'counter_offers_made', 'counter_offers_accepted', 'rfqs_with_response']:
                            flag_modified(existing_session, key)
                    self.session.commit()
                    self.session.refresh(existing_session)
                    return existing_session
                else:
                    # Return original session object to prevent data loss
                    return ConversationSession(**session_data)
                    
            except Exception as retry_error:
                logger.error(f"Retry failed in save_conversation_session: {retry_error}")
                return ConversationSession(**session_data)

    def get_conversation_session(self, session_id: str) -> Optional[ConversationSession]:
        """Get a conversation session by ID."""
        from sqlalchemy.exc import SQLAlchemyError
        import os

        worker_pid = os.getpid()

        try:
            # Force expiration of any cached objects to prevent stale data
            self.session.expire_all()

            session = self.session.query(ConversationSession).filter_by(session_id=session_id).first()

            # Refresh the session object to ensure latest data from database
            if session:
                self.session.refresh(session)

                # Log what we loaded
                extracted_entities_count = len(session.extracted_entities) if session.extracted_entities else 0
                workflow_state_keys = list(session.workflow_state.keys()) if session.workflow_state else []
                logger.info(f"[WORKER-{worker_pid}] [SESSION-LOAD] {session_id} | extracted_entities count: {extracted_entities_count} | workflow_state keys: {workflow_state_keys}")

            # Fix potential JSON deserialization issues
            if session and session.workflow_state:
                try:
                    # Ensure workflow_state is properly deserialized as dict
                    if isinstance(session.workflow_state, str):
                        session.workflow_state = json.loads(session.workflow_state)

                    # Debug logging for optional fields - ENHANCED
                    if 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state:
                        logger.info(f"[SESSION_LOAD_DEBUG] Loaded session {session_id} with optional fields: {list(session.workflow_state.keys())}")
                        if 'pending_optional_combined_rfq' in session.workflow_state:
                            optional_data = session.workflow_state['pending_optional_combined_rfq']
                            logger.info(f"[SESSION_LOAD_DEBUG] pending_optional_combined_rfq has keys: {list(optional_data.keys()) if isinstance(optional_data, dict) else 'Not a dict'}")
                    else:
                        # Log when optional fields are NOT present
                        logger.info(f"[SESSION_LOAD_DEBUG] Loaded session {session_id} WITHOUT optional fields. Keys: {list(session.workflow_state.keys())}")

                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(f"Failed to deserialize workflow_state for session {session_id}: {e}")
                    # Reset to empty dict to prevent further errors
                    session.workflow_state = {"extracted_entities": []}
            elif session:
                logger.info(f"[SESSION_LOAD_DEBUG] Loaded session {session_id} with NO workflow_state")

            return session

        except SQLAlchemyError as e:
            logger.error(f"Database error in get_conversation_session: {e}")
            self.session.rollback()
            return None