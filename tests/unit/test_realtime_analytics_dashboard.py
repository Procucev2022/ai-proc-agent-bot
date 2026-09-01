"""Unit tests for the Real-Time Analytics Dashboard services, APIs, and event streaming."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from fastapi.testclient import TestClient

from app.main import app
from app.services.realtime_analytics_service import (
    RealtimeAnalyticsService,
    get_realtime_analytics_service,
    REDIS_KEY_ACTIVE_USERS,
    REDIS_CHANNEL_LIVE_EVENTS,
    REDIS_KEY_RECENT_FEED
)
from app.services.dashboard_aggregation_service import DashboardAggregationService
from app.models import (
    ConversationSession,
    RFQ,
    RFQStatus,
    UserType,
    SessionState,
    ConversationOutcome,
    WorkflowType,
    RFQNotificationFact,
    Seller,
    ProductCategory,
    SessionEvent,
    SellerRanking,
)
from app.database import get_db_session


@pytest.fixture(autouse=True)
def override_db_dependency():
    """Ensure TestClient uses a mocked DB session."""
    mock_session = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: mock_session
    yield mock_session
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for async operations."""
    client = MagicMock()
    client.zadd = AsyncMock()
    client.zremrangebyscore = AsyncMock()
    client.expire = AsyncMock()
    client.zrange = AsyncMock(return_value=[
        json.dumps({"phone": "919876543210", "user_type": "buyer"}),
        json.dumps({"phone": "919876543211", "user_type": "seller"}),
        json.dumps({"phone": "919876543212", "user_type": "unknown"}),
        json.dumps({"phone": "919876543210", "user_type": "buyer"}),  # duplicate phone
        "invalid-json-entry"
    ])
    client.publish = AsyncMock()
    client.lpush = AsyncMock()
    client.ltrim = AsyncMock()
    client.lrange = AsyncMock(return_value=[
        json.dumps({
            "event_type": "whatsapp_visitor",
            "user_id": "919876543210",
            "user_masked": "9198****10",
            "timestamp": "2026-08-27T10:00:00Z",
            "data": {}
        }),
        "invalid-json"
    ])
    
    pubsub_mock = MagicMock()
    pubsub_mock.subscribe = AsyncMock()
    pubsub_mock.unsubscribe = AsyncMock()
    pubsub_mock.close = AsyncMock()
    pubsub_mock.get_message = AsyncMock(side_effect=[
        {"type": "message", "data": json.dumps({"event_type": "rfq_created", "user_id": "919876543210"}).encode("utf-8")},
        {"type": "message", "data": "invalid-json"},
        {"type": "other"},
        RuntimeError("transient pubsub err"),
        asyncio.CancelledError()
    ])
    client.pubsub = MagicMock(return_value=pubsub_mock)

    return client


@pytest.mark.asyncio
async def test_dashboard_aggregation_service_exhaustive_filters():
    """Test DashboardAggregationService with all date presets and filter dimensions."""
    from app.services.dashboard_aggregation_service import DashboardAggregationService
    from app.models import ConversationSession, RFQ, ProductCategory, RFQNotificationFact, SessionState, WorkflowType, ConversationOutcome, RFQStatus, UserType, Seller, SellerRanking
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.database import Base
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    db = SessionTest()

    now = datetime.utcnow()
    past = now - timedelta(days=2)

    # Seed data
    p = ProductCategory(category_name="Chemicals")
    # FIX: Add required 'location' field to Seller model
    seller_row = Seller(
        seller_id="seller_123",
        seller_name="Acme Chemicals",
        phone_number="919999999992",
        categories=["Chemicals"],
        location={"city": "Mumbai", "state": "Maharashtra", "lat": 19.0760, "lng": 72.8777},
        ranking=SellerRanking.Gold,
        subscription_credits=10,
    )
    db.add_all([p, seller_row])
    db.commit()

    # Prior session for returning user calculation
    sess_prior = ConversationSession(
        session_id="s_prior",
        external_user_id="919999999991",
        user_type=UserType.buyer,
        session_state=SessionState.active,
        workflow_type=WorkflowType.registration,
        workflow_state={"status": "completed", "registration_stage": "completed"},
        conversation_history={"messages": [{"role": "user", "content": "Hi"}]},
        outcome=ConversationOutcome.completed,
        retention_date=past.date(),
        created_at=past,
        last_activity_at=past,
    )

    # Comparison session
    sess_comp = ConversationSession(
        session_id="s_comp",
        external_user_id="919999999991",
        user_type=UserType.buyer,
        session_state=SessionState.active,
        workflow_type=WorkflowType.rfq_creation,
        rfq_ids=["RFQ100"],
        conversation_history={"messages": []},
        outcome=ConversationOutcome.completed,
        created_at=now - timedelta(days=1),
        last_activity_at=now - timedelta(days=1),
    )

    r = RFQ(
        rfq_id="RFQ100",
        external_user_id="919999999991",
        status=RFQStatus.collecting,
        api_payload={"product_name": "Chemicals", "location": "Delhi"},
        created_at=now,
    )
    r2 = RFQ(
        rfq_id="RFQ101",
        external_user_id="919999999991",
        status=RFQStatus.submitted,
        api_payload={"product_name": "Steel", "location": "Mumbai"},
        created_at=past,
    )
    sess = ConversationSession(
        session_id="s_chem",
        external_user_id="919999999991",
        user_type=UserType.buyer,
        session_state=SessionState.active,
        workflow_type=WorkflowType.rfq_creation,
        rfq_id="RFQ100",
        rfq_ids=["RFQ100"],
        workflow_state={"status": "completed", "registration_stage": "completed"},
        conversation_history={"messages": [{"role": "user", "content": "Need chemicals", "timestamp": "2026-08-27T10:00:00Z"}]},
        outcome=ConversationOutcome.completed,
        retention_date=now.date(),
        created_at=now,
        last_activity_at=now,
    )
    sess_seller = ConversationSession(
        session_id="s_seller",
        external_user_id="919999999992",
        user_type=UserType.seller,
        session_state=SessionState.active,
        workflow_type=WorkflowType.seller_rfq_interest,
        workflow_state={"seller_subscribed": True},
        conversation_history={"messages": []},
        outcome=ConversationOutcome.completed,
        retention_date=now.date(),
        created_at=now,
        last_activity_at=now,
    )
    sess_unknown = ConversationSession(
        session_id="s_unk",
        external_user_id="919999999993",
        user_type=UserType.unknown,
        session_state=SessionState.active,
        workflow_type=None,
        workflow_state={},
        conversation_history={"messages": []},
        outcome=None,
        retention_date=now.date(),
        created_at=now,
        last_activity_at=now,
    )

    facts = [
        RFQNotificationFact(
            date=now.date(),
            session_id="s_seller",
            rfq_id="RFQ100",
            seller_id="seller_123",
            category="Chemicals",
            rfq_notified_at=now,
            seller_response_at=now,
            created_at=now,
        ),
        RFQNotificationFact(
            date=now.date(),
            session_id="s_seller",
            rfq_id="RFQ100",
            seller_id="seller_124",
            category="Chemicals",
            rfq_notified_at=now,
            seller_response_at=None,
            created_at=now,
        ),
        RFQNotificationFact(
            date=now.date(),
            session_id="s_seller",
            rfq_id="RFQ100",
            seller_id="seller_125",
            category="Chemicals",
            rfq_notified_at=now,
            seller_response_at=None,
            created_at=now,
        ),
    ]
    db.add_all([sess_prior, sess_comp, r, r2, sess, sess_seller, sess_unknown] + facts)
    db.commit()

    svc = DashboardAggregationService(db_session=db)

    # 1. Test all date presets
    for preset in ["today", "yesterday", "7d", "30d", "90d"]:
        stats = await svc.get_dashboard_stats(date_preset=preset)
        assert stats["status"] == "success"

    # 2. Custom date range (valid and invalid)
    stats_custom = await svc.get_dashboard_stats(date_preset="custom", start_date="2026-08-01", end_date="2026-08-31")
    assert stats_custom["status"] == "success"

    stats_custom_inv = await svc.get_dashboard_stats(date_preset="custom", start_date="invalid", end_date="invalid")
    assert stats_custom_inv["status"] == "success"

    # 3. Role and dimension filters
    stats_buyer = await svc.get_dashboard_stats(role="buyer", category="Chemicals", location="Delhi", rfq_status="collecting")
    assert stats_buyer["status"] == "success"

    stats_seller = await svc.get_dashboard_stats(role="seller")
    assert stats_seller["status"] == "success"

    stats_all = await svc.get_dashboard_stats(role="all")
    assert stats_all["status"] == "success"

    db.close()
