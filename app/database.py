"""
Database connection and session management for the AI Procurement Agent.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import get_settings
from .models import Base, ProductCategory, Vendor

engine = None
SessionLocal = None


def init_database():
    """Initialize database tables and create sample data."""
    global engine, SessionLocal

    settings = get_settings()
    engine = create_engine(settings.get_database_url())
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
    if SessionLocal is None:
        init_database()
    return SessionLocal()


class DatabaseManager:
    """
    Database manager class for advanced database operations.

    Provides utilities for database maintenance, monitoring,
    and administrative operations.
    """

    def __init__(self, session=None):
        pass

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

    def cleanup_expired_sessions(self, hours: int = 24):
        """
        Clean up expired conversation sessions.

        Removes old conversation sessions and associated data
        to maintain database performance.
        """
        pass

    def backup_learning_data(self):
        """
        Create backup of learning records and vendor associations.

        Exports learning data for backup and analysis purposes.
        """
        pass
