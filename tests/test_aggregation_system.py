#!/usr/bin/env python3
"""
Test script to verify the entire aggregation system with sample data.

This script will:
1. Create sample session data with the new fields
2. Run the daily aggregation service
3. Check the aggregated metrics
4. Export to Excel for verification
"""

import sys
import os
from datetime import date, datetime, timedelta
from uuid import uuid4

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import get_db_session
from app.models import (
    ConversationSession, UserType, SessionState, WorkflowType, 
    ConversationOutcome, DailyAggregatedMetrics, DailySummary
)
from app.services.daily_aggregation_service import DailyAggregationService
from app.services.daily_summary_service import DailySummaryService
from app.services.chat_summary_service import ChatSummaryService

def create_sample_session_data(target_date: date):
    """Create sample session data for testing."""
    print(f"Creating sample session data for {target_date}")
    
    with get_db_session() as db:
        # Clear existing data for the test date
        existing_sessions = db.query(ConversationSession)\
            .filter(ConversationSession.created_at >= target_date)\
            .filter(ConversationSession.created_at < target_date + timedelta(days=1))\
            .all()
        
        for session in existing_sessions:
            db.delete(session)
        
        # Create sample buyer sessions
        buyer_sessions = []
        
        # Buyer 1: Active user with multiple RFQs and BFS activity
        session1 = ConversationSession(
            session_id=str(uuid4()),
            external_user_id="+919876543210",
            user_type=UserType.buyer,
            session_state=SessionState.completed,
            workflow_type=WorkflowType.rfq_creation,
            outcome=ConversationOutcome.completed,
            rfq_ids=["rfq_001", "rfq_002"],
            product_items=[
                {"name": "Steel Pipes", "quantity": 100, "category": "Construction"},
                {"name": "Cement Bags", "quantity": 50, "category": "Construction"}
            ],
            seller_responses=[
                {"seller_id": "seller_001", "bid_amount": 15000, "status": "pending"},
                {"seller_id": "seller_002", "bid_amount": 14500, "status": "accepted"}
            ],
            # BFS tracking
            bfs_products_searched=[
                {"product": "Steel Pipes 2inch", "category": "Construction"},
                {"product": "Cement Grade 43", "category": "Construction"}
            ],
            bfs_search_count=2,
            bfs_price_accepted=[
                {"product": "Cement Grade 43", "price": 350, "quantity": 50}
            ],
            bfs_counter_offers=[
                {"product": "Steel Pipes 2inch", "original_price": 120, "counter_price": 110}
            ],
            # Bidding lifecycle
            products_bid_for=[
                {"product": "Steel Pipes", "rfq_id": "rfq_001"},
                {"product": "Cement Bags", "rfq_id": "rfq_002"}
            ],
            bids_received=[
                {"rfq_id": "rfq_001", "seller": "seller_001", "amount": 15000},
                {"rfq_id": "rfq_002", "seller": "seller_002", "amount": 14500}
            ],
            bids_accepted=[
                {"rfq_id": "rfq_002", "seller": "seller_002", "amount": 14500}
            ],
            rfqs_with_response=["rfq_001", "rfq_002"],
            avg_products_per_rfq=1.0,
            avg_categories_per_rfq=1.0,
            interaction_metrics={"messages_sent": 12, "navigation_steps": 8},
            extracted_entities={"category": "Construction", "description": "Building Materials"},
            workflow_state={"step": "completed"},
            conversation_history=[{"message": "I need construction materials"}],
            retention_date=target_date + timedelta(days=365),
            created_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=9),
            last_activity_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=9, minutes=45),
            completed_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=9, minutes=45)
        )
        buyer_sessions.append(session1)
        
        # Buyer 2: Single RFQ user with BFS activity
        session2 = ConversationSession(
            session_id=str(uuid4()),
            external_user_id="+919876543211",
            user_type=UserType.buyer,
            session_state=SessionState.completed,
            workflow_type=WorkflowType.product_search,
            outcome=ConversationOutcome.completed,
            rfq_ids=["rfq_003"],
            product_items=[{"name": "Office Chairs", "quantity": 20, "category": "Furniture"}],
            seller_responses=[{"seller_id": "seller_003", "bid_amount": 8000, "status": "pending"}],
            # BFS tracking
            bfs_products_searched=[
                {"product": "Executive Chair", "category": "Furniture"},
                {"product": "Conference Table", "category": "Furniture"}
            ],
            bfs_search_count=3,
            bfs_price_accepted=[
                {"product": "Executive Chair", "price": 15000, "quantity": 5}
            ],
            # Bidding lifecycle
            products_bid_for=[
                {"product": "Office Chairs", "rfq_id": "rfq_003"}
            ],
            bids_received=[
                {"rfq_id": "rfq_003", "seller": "seller_003", "amount": 8000}
            ],
            rfqs_with_response=["rfq_003"],
            avg_products_per_rfq=1.0,
            avg_categories_per_rfq=1.0,
            interaction_metrics={"messages_sent": 6, "navigation_steps": 4},
            extracted_entities={"category": "Furniture", "description": "Office Equipment"},
            workflow_state={"step": "completed"},
            conversation_history=[{"message": "Looking for office chairs"}],
            retention_date=target_date + timedelta(days=365),
            created_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=14),
            last_activity_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=14, minutes=30),
            completed_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=14, minutes=30)
        )
        buyer_sessions.append(session2)
        
        # Seller sessions
        seller_sessions = []
        
        # Seller 1: Active seller with counter offers
        session3 = ConversationSession(
            session_id=str(uuid4()),
            external_user_id="+919876543220",
            user_type=UserType.seller,
            session_state=SessionState.completed,
            workflow_type=WorkflowType.general_inquiry,
            outcome=ConversationOutcome.completed,
            rfq_ids=["rfq_001", "rfq_003"],  # RFQs seller engaged with
            seller_responses=[
                {"rfq_id": "rfq_001", "bid_amount": 15000, "type": "initial_bid"},
                {"rfq_id": "rfq_003", "bid_amount": 8000, "type": "counter_offer"}
            ],
            # Counter offers tracking
            counter_offers_made=[
                {"rfq_id": "rfq_001", "original_bid": 16000, "counter_bid": 15000},
                {"rfq_id": "rfq_003", "original_bid": 9000, "counter_bid": 8000}
            ],
            counter_offers_accepted=[
                {"rfq_id": "rfq_003", "accepted_amount": 8000}
            ],
            bids_accepted=[
                {"rfq_id": "rfq_003", "amount": 8000}
            ],
            interaction_metrics={"rfqs_downloaded": 3, "responses_sent": 2},
            extracted_entities={"category": "Construction", "description": "Building Materials"},
            workflow_state={"step": "completed"},
            conversation_history=[{"message": "I can supply construction materials"}],
            retention_date=target_date + timedelta(days=365),
            created_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=10),
            last_activity_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=10, minutes=20),
            completed_at=datetime.combine(target_date, datetime.min.time()) + timedelta(hours=10, minutes=20)
        )
        seller_sessions.append(session3)
        
        # Add all sessions to database
        all_sessions = buyer_sessions + seller_sessions
        for session in all_sessions:
            db.add(session)
        
        db.commit()
        print(f"Created {len(all_sessions)} sample sessions ({len(buyer_sessions)} buyers, {len(seller_sessions)} sellers)")
        return all_sessions

def run_all_services(target_date: date):
    """Test all services with the sample data."""
    print(f"\nRunning all services for {target_date}")
    
    # 1. Test Daily Summary Service (user-level)
    print("\n1. Testing DailySummaryService...")
    daily_summary_service = DailySummaryService()
    
    # Generate daily summaries for each user
    users = ["+919876543210", "+919876543211", "+919876543220"]
    for user_id in users:
        import asyncio
        result = asyncio.run(daily_summary_service.generate_daily_summary(user_id, target_date))
        if result:
            print(f"   Created daily summary for {user_id}")
        else:
            print(f"   Failed to create daily summary for {user_id}")
    
    # 2. Test Daily Aggregation Service (business metrics)
    print("\n2. Testing DailyAggregationService...")
    aggregation_service = DailyAggregationService()
    
    import asyncio
    result = asyncio.run(aggregation_service.run_daily_aggregation(target_date))
    
    if result:
        print("   Daily aggregation completed successfully")
    else:
        print("   Daily aggregation failed")
    
    return result

def check_results(target_date: date):
    """Check and display the aggregated results."""
    print(f"\nChecking aggregation results for {target_date}")
    
    with get_db_session() as db:
        # Check daily summaries (user-level)
        print("\nDaily Summaries (User-Level):")
        daily_summaries = db.query(DailySummary)\
            .filter(DailySummary.date == target_date)\
            .all()
        
        for summary in daily_summaries:
            print(f"   User: {summary.external_user_id}")
            print(f"   Type: {summary.user_type.value if summary.user_type else 'unknown'}")
            print(f"   Sessions: {summary.sessions_count}")
            print(f"   RFQs: {summary.rfqs_created_count}")
            print(f"   Seller Interactions: {summary.seller_interaction_count}")
            print(f"   Categories: {summary.primary_product_categories}")
            print(f"   ---")
        
        # Check aggregated metrics (business-level)
        print("\nDaily Aggregated Metrics (Business-Level):")
        aggregated_metrics = db.query(DailyAggregatedMetrics)\
            .filter(DailyAggregatedMetrics.date == target_date)\
            .all()
        
        for metric in aggregated_metrics:
            print(f"   Type: {metric.metric_type}")
            print(f"   Data: {metric.metric_data}")
            print(f"   Complete: {metric.is_complete}")
            print(f"   ---")

def export_test_results(target_date: date):
    """Export the test results to Excel and Markdown."""
    print(f"\nExporting test results for {target_date}")
    
    # Export to Excel
    try:
        from tests.export_aggregated_metrics import export_daily_metrics_to_excel
        
        output_file = f"test_aggregation_results_{target_date}.xlsx"
        result = export_daily_metrics_to_excel(target_date, target_date, output_file)
        
        if result:
            print(f"   Excel results exported to: {result}")
        else:
            print("   Excel export failed")
            
    except Exception as e:
        print(f"   Excel export error: {e}")
    
    # Export to Markdown
    try:
        from export_to_markdown import export_to_markdown
        
        md_output_file = f"B2B_WhatsApp_Test_Report_{target_date}.md"
        md_result = export_to_markdown(target_date, md_output_file)
        
        if md_result:
            print(f"   Markdown results exported to: {md_result}")
        else:
            print("   Markdown export failed")
            
    except Exception as e:
        print(f"   Markdown export error: {e}")

def main():
    """Run the complete aggregation system test."""
    print("Starting Aggregation System Test")
    print("=" * 50)
    
    # Use yesterday's date for testing
    test_date = date.today() - timedelta(days=1)
    print(f"Test Date: {test_date}")
    
    try:
        # Step 1: Create sample data
        sessions = create_sample_session_data(test_date)
        
        # Step 2: Run all services
        success = run_all_services(test_date)
        
        if success:
            # Step 3: Check results
            check_results(test_date)
            
            # Step 4: Export results
            export_test_results(test_date)
            
            print("\nAggregation System Test PASSED!")
            print("=" * 50)
            return 0
        else:
            print("\nAggregation System Test FAILED!")
            return 1
            
    except Exception as e:
        print(f"\nTest failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    exit(main())