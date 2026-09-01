"""
Unit tests for the Real-Time Analytics Dashboard services, APIs, and event streaming.
"""

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
async def test_realtime_analytics_service_heartbeat_and_counts(mock_redis_client):
    """Test heartbeat recording and active user counts."""
    service = RealtimeAnalyticsService()

    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=mock_redis_client)):
        # Test record heartbeat
        await service.record_heartbeat("919876543210", "buyer", "session_123")
        assert mock_redis_client.zadd.called
        assert mock_redis_client.zremrangebyscore.called

        # Test get active users count
        counts = await service.get_active_users_count()
        assert counts["total"] == 3
        assert counts["buyers"] == 1
        assert counts["sellers"] == 1
        assert counts["unknown"] == 1


@pytest.mark.asyncio
async def test_realtime_analytics_service_disabled_and_exceptions():
    """Test fallback paths when Redis is disabled or raises exceptions."""
    service = RealtimeAnalyticsService()
    service.settings = SimpleNamespace(redis_session_storage_enabled=False)

    # Heartbeat and counts when disabled
    await service.record_heartbeat("919876543210", "buyer")
    counts = await service.get_active_users_count()
    assert counts["total"] == 0

    # Heartbeat and counts with None client
    service.settings = SimpleNamespace(redis_session_storage_enabled=True)
    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=None)):
        await service.record_heartbeat("919876543210", "buyer")
        counts = await service.get_active_users_count()
        assert counts["total"] == 0

    # Heartbeat with Redis exception
    failing_client = MagicMock()
    failing_client.zadd = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=failing_client)):
        await service.record_heartbeat("919876543210", "buyer")
        counts = await service.get_active_users_count()
        assert counts["total"] == 0


@pytest.mark.asyncio
async def test_realtime_analytics_service_publish_and_feed(mock_redis_client):
    """Test publishing events and retrieving recent feed."""
    service = RealtimeAnalyticsService()

    fake_db = MagicMock()
    fake_db_context = MagicMock()
    fake_db_context.__enter__.return_value = fake_db
    fake_db_context.__exit__.return_value = None

    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=mock_redis_client)), \
         patch("app.services.realtime_analytics_service.get_db_session_context", return_value=fake_db_context):

        event = await service.publish_event(
            event_type="rfq_created",
            user_id="user_12345",
            data={"rfq_id": "RFQ12345", "categories": ["Steel"], "user_type": "buyer"},
            session_id="session_test",
            persist_db=True
        )

        assert event["event_type"] == "rfq_created"
        assert event["user_masked"] == "user_12345"
        assert mock_redis_client.publish.called
        assert mock_redis_client.lpush.called
        assert fake_db.add.called
        assert fake_db.commit.called

        feed = await service.get_recent_feed(limit=10)
        assert len(feed) == 1
        assert feed[0]["event_type"] == "whatsapp_visitor"


@pytest.mark.asyncio
async def test_realtime_analytics_service_publish_exceptions():
    """Test publish_event handling Redis and DB exceptions gracefully."""
    service = RealtimeAnalyticsService()
    service.settings = SimpleNamespace(redis_session_storage_enabled=True)

    failing_redis = MagicMock()
    failing_redis.publish = AsyncMock(side_effect=RuntimeError("redis pub err"))
    failing_redis.lpush = AsyncMock(side_effect=RuntimeError("redis lpush err"))

    failing_db_context = MagicMock()
    failing_db_context.__enter__.side_effect = RuntimeError("db error")

    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=failing_redis)), \
         patch("app.services.realtime_analytics_service.get_db_session_context", return_value=failing_db_context):

        # Should not raise exception
        event = await service.publish_event(
            event_type="test_event",
            user_id="",
            data=None,
            session_id="sess_err",
            persist_db=True
        )
        assert event["event_type"] == "test_event"


@pytest.mark.asyncio
async def test_realtime_analytics_service_db_feed_fallback():
    """Test retrieving feed from DB when Redis returns empty list or fails."""
    service = RealtimeAnalyticsService()

    fake_row = MagicMock()
    fake_row.event_type = "rfq_created"
    fake_row.user_id = "919876543210"
    fake_row.session_id = "sess_1"
    fake_row.event_timestamp = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    fake_row.event_data = {"key": "val"}

    fake_db = MagicMock()
    fake_db.query.return_value.order_by.return_value.limit.return_value.all.return_value = [fake_row]
    fake_db_context = MagicMock()
    fake_db_context.__enter__.return_value = fake_db
    fake_db_context.__exit__.return_value = None

    failing_redis = MagicMock()
    failing_redis.lrange = AsyncMock(side_effect=RuntimeError("lrange error"))

    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=failing_redis)), \
         patch("app.services.realtime_analytics_service.get_db_session_context", return_value=fake_db_context):

        feed = await service.get_recent_feed(limit=5)
        assert len(feed) == 1
        assert feed[0]["event_type"] == "rfq_created"
        assert feed[0]["user_masked"] == "9198****10"


@pytest.mark.asyncio
async def test_realtime_analytics_service_db_feed_failure():
    """Test get_recent_feed when both Redis and DB fail."""
    service = RealtimeAnalyticsService()
    service.settings = SimpleNamespace(redis_session_storage_enabled=False)

    failing_db_context = MagicMock()
    failing_db_context.__enter__.side_effect = RuntimeError("db fail")

    with patch("app.services.realtime_analytics_service.get_db_session_context", return_value=failing_db_context):
        feed = await service.get_recent_feed(limit=5)
        assert feed == []


@pytest.mark.asyncio
async def test_realtime_analytics_service_subscribe(mock_redis_client):
    """Test subscribing to Redis Pub/Sub live stream."""
    service = RealtimeAnalyticsService()

    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=mock_redis_client)):
        messages = []
        async for msg in service.subscribe_events():
            messages.append(msg)
            if len(messages) >= 1:
                break

        assert len(messages) == 1
        assert messages[0]["event_type"] == "rfq_created"

    # Test subscribe fallback when Redis is disabled
    service.settings = SimpleNamespace(redis_session_storage_enabled=False)
    async for msg in service.subscribe_events():
        assert msg["event_type"] == "heartbeat"
        break

    # Test subscribe fallback when client is None
    service.settings = SimpleNamespace(redis_session_storage_enabled=True)
    with patch("app.services.realtime_analytics_service.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=None)):
        async for msg in service.subscribe_events():
            assert msg["event_type"] == "heartbeat"
            break


def test_get_realtime_analytics_service_singleton():
    """Test global singleton getter."""
    s1 = get_realtime_analytics_service()
    s2 = get_realtime_analytics_service()
    assert s1 is s2


def test_dashboard_date_range_resolution():
    """Test date preset resolution for all supported ranges."""
    service = DashboardAggregationService()

    for preset in ["today", "yesterday", "7d", "30d", "90d"]:
        start, end, comp_start, comp_end = service._resolve_date_range(preset)
        assert start <= end
        assert comp_start <= comp_end

    # Test custom range valid
    start, end, comp_start, comp_end = service._resolve_date_range(
        "custom", "2026-08-01", "2026-08-15"
    )
    assert start.strftime("%Y-%m-%d") == "2026-08-01"
    assert end.strftime("%Y-%m-%d") == "2026-08-15"

    # Test custom range invalid format fallback
    start, end, comp_start, comp_end = service._resolve_date_range(
        "custom", "invalid", "dates"
    )
    assert start <= end


@pytest.mark.asyncio
async def test_dashboard_aggregation_service_full_stats():
    """Test aggregation computation logic with mocked DB models."""
    mock_db = MagicMock()

    mock_query = MagicMock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.group_by.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.scalar.side_effect = [
        10,  # total_sessions
        8,   # comp_sessions
        6,   # unique_users
        5,   # comp_unique_users
        4,   # new_buyers
        2,   # new_sellers
        2,   # unknown_sessions_count
        3,   # active_conversations
        1,   # drop_off_count
        15,  # total_notifications
        6,   # responses_received
        3,   # active_responding_sellers
        4,   # rfqs_responded
    ]
    mock_query.all.side_effect = [
        [("919876543210",), ("919876543211",)],  # users_in_period
        [("919876543210",), ("919876543211",)],  # all_today_users
        [("919876543210",)],  # buyer_users_set
        [("919876543211",)],  # seller_users_set
        [("919876543210",)],  # rfq_phones_in_period
        [([], None, "919876543210")],  # session rfq_ids
        [(WorkflowType.general_inquiry, {}, None, None, None)],  # buyer_sessions for user
        [SimpleNamespace(phone_number="919876543211", subscription_credits=100)],  # registered_sellers_map
        [(WorkflowType.general_inquiry, {}, None)],  # seller_sessions for user
        [
            (["RFQ12345", "RFQ12346"], None, ConversationOutcome.completed, None),
            (None, "RFQ12347", ConversationOutcome.abandoned, None),
            (None, None, None, WorkflowType.rfq_creation),
        ],  # session_rfq_rows
        [("RFQ12345", RFQStatus.submitted), ("RFQ99999", RFQStatus.ready)],  # db_rfqs
        [(["RFQ10000"], None), (None, "RFQ10001")],  # comp_session_rfq_rows
        [("RFQ10000",), ("RFQ10002",)],  # comp_db_rfqs
        [(datetime(2026, 8, 27, 10, 0),), (datetime(2026, 8, 27, 14, 0),)],  # sessions_in_window
        [("Industrial Steel", 10, 2)],  # category breakdown with low response rate
        [("919876543210", 4, datetime(2026, 8, 27, 12, 0))],  # top buyers
        [
            SimpleNamespace(
                seller_id="s1",
                seller_name="Steel Corp",
                phone_number="919876543211",
                categories=["Steel"],
                ranking=SimpleNamespace(value="Gold"),
                subscription_credits=100
            )
        ],  # top sellers
    ]
    mock_query.count.return_value = 2  # returning users count

    service = DashboardAggregationService(db_session=mock_db)

    with patch.object(service.realtime_service, "get_active_users_count", AsyncMock(return_value={"total": 5, "buyers": 3, "sellers": 2})), \
         patch.object(service.realtime_service, "get_recent_feed", AsyncMock(return_value=[])):

        stats = await service.get_dashboard_stats(
            date_preset="today",
            role="buyer",
            category="Steel",
            location="Mumbai",
            rfq_status="submitted"
        )

        assert stats["status"] == "success"
        assert "executive" in stats
        assert "whatsapp" in stats
        assert "buyer_seller" in stats
        assert "rfq_lifecycle" in stats
        assert "seller_response" in stats
        assert "marketplace_health" in stats
        assert "user_classification" in stats
        assert stats["user_classification"]["buyer"]["total"] == 1
        assert stats["user_classification"]["seller"]["total"] == 1
        assert stats["user_classification"]["unknown"]["sessions"] == 2
        assert "funnel" in stats
        assert len(stats["funnel"]) == 7
        assert len(stats["marketplace_health"]["alerts"]) >= 1


@pytest.mark.asyncio
async def test_dashboard_aggregation_service_fallback_categories():
    """Test fallback to ProductCategory when notification fact is empty."""
    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.group_by.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.scalar.return_value = 0
    mock_query.count.return_value = 0
    mock_query.all.side_effect = [
        [],  # users_in_period
        [],  # all_today_users
        [],  # buyer_users_set
        [],  # seller_users_set
        [],  # rfq_phones_in_period
        [],  # session rfq_ids
        [],  # registered_sellers_map
        [],  # session_rfq_rows
        [],  # db_rfqs
        [],  # comp_session_rfq_rows
        [],  # comp_db_rfqs
        [],  # sessions_in_window
        [],  # category breakdown empty
        [("Raw Materials",), ("Chemicals",)],  # ProductCategory fallback
        [],  # top buyers
        [],  # top sellers
    ]

    service = DashboardAggregationService(db_session=mock_db)
    with patch.object(service.realtime_service, "get_active_users_count", AsyncMock(return_value={"total": 0})), \
         patch.object(service.realtime_service, "get_recent_feed", AsyncMock(return_value=[])):

        stats = await service.get_dashboard_stats(date_preset="yesterday", role="seller")
        assert stats["status"] == "success"
        assert len(stats["rfq_lifecycle"]["category_breakdown"]) == 2


def test_dashboard_api_endpoints():
    """Test FastAPI dashboard REST and HTML endpoints."""
    client = TestClient(app)

    # 1. Test HTML Dashboard Page
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "AI Procurement Agent" in response.text

    # 1b. Test HTML Classification Details Page
    response = client.get("/dashboard/classification-details")
    assert response.status_code == 200

    # 2. Test Root Endpoint Contains Dashboard Link
    root_res = client.get("/")
    assert root_res.status_code == 200
    assert "/dashboard" in root_res.json().get("dashboard", "")

    # 3. Test Active Users API
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.get_active_users_count = AsyncMock(return_value={"total": 2, "buyers": 1, "sellers": 1, "unknown": 0})
        mock_rt.return_value = mock_instance
        res = client.get("/api/dashboard/active-users")
        assert res.status_code == 200
        assert res.json()["active_users"]["total"] == 2

    # 4. Test Active Users API Exception
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.get_active_users_count = AsyncMock(side_effect=RuntimeError("error"))
        mock_rt.return_value = mock_instance
        res = client.get("/api/dashboard/active-users")
        assert res.status_code == 500

    # 5. Test Feed API
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.get_recent_feed = AsyncMock(return_value=[{"event_type": "whatsapp_visitor"}])
        mock_rt.return_value = mock_instance
        res = client.get("/api/dashboard/feed?limit=5")
        assert res.status_code == 200
        assert len(res.json()["feed"]) == 1

    # 6. Test Feed API Exception
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.get_recent_feed = AsyncMock(side_effect=RuntimeError("error"))
        mock_rt.return_value = mock_instance
        res = client.get("/api/dashboard/feed?limit=5")
        assert res.status_code == 500

    # 7. Test Export API
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(return_value={"export": "ok"})):
        res = client.get("/api/dashboard/export?date_preset=today")
        assert res.status_code == 200
        assert res.json()["export"] == "ok"

    # 8. Test Export API Exception
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(side_effect=RuntimeError("export fail"))):
        res = client.get("/api/dashboard/export?date_preset=today")
        assert res.status_code == 500

    # 9. Test Stats API Endpoint
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(return_value={"status": "success", "executive": {}})):
        res = client.get("/api/dashboard/stats?date_preset=today&role=all")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

    # 10. Test Stats API Exception
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(side_effect=RuntimeError("stats fail"))):
        res = client.get("/api/dashboard/stats?date_preset=today")
        assert res.status_code == 500

    # 11. Test SSE live-stream endpoint
    async def sample_generator():
        yield {"event_type": "test", "data": {}}

    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.subscribe_events = sample_generator
        mock_rt.return_value = mock_instance
        with client.stream("GET", "/api/dashboard/live-stream") as stream_res:
            assert stream_res.status_code == 200
            for line in stream_res.iter_lines():
                if line:
                    assert line.startswith("data:")
                    break

    # 12. Test SSE live-stream cancellation and error handling
    async def cancelled_generator():
        yield {"event_type": "cancelled_test"}
        raise asyncio.CancelledError()

    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.subscribe_events = cancelled_generator
        mock_rt.return_value = mock_instance
        with client.stream("GET", "/api/dashboard/live-stream") as stream_res:
            assert stream_res.status_code == 200
            for _ in stream_res.iter_lines():
                pass

    async def error_generator():
        yield {"event_type": "error_test"}
        raise RuntimeError("stream boom")

    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_instance = MagicMock()
        mock_instance.subscribe_events = error_generator
        mock_rt.return_value = mock_instance
        with client.stream("GET", "/api/dashboard/live-stream") as stream_res:
            assert stream_res.status_code == 200
            for _ in stream_res.iter_lines():
                pass



# ─── Daily Visitors Tests ──────────────────────────────────────────────────────

def _make_session(external_user_id: str, created_at: datetime) -> MagicMock:
    """Helper: create a lightweight fake session row."""
    row = MagicMock()
    row.day = created_at.date()
    row.external_user_id = external_user_id
    return row


def test_get_daily_visitors_basic():
    """get_daily_visitors returns one entry per day in the 7d window."""
    now = datetime.now(timezone.utc)
    fake_rows = [
        _make_session("user_A", now - timedelta(days=1)),
        _make_session("user_A", now - timedelta(days=1)),  # duplicate same user same day
        _make_session("user_B", now - timedelta(days=1)),
        _make_session("user_C", now),
    ]

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.all.return_value = fake_rows

    svc = DashboardAggregationService(db_session=fake_db)
    result = svc.get_daily_visitors(date_preset="7d")

    assert isinstance(result, list)
    assert len(result) == 7  # 7-day window always has 7 entries
    for entry in result:
        assert "date" in entry
        assert "visitors" in entry
        assert isinstance(entry["visitors"], int)

    # Find yesterday's entry — should have 2 unique users (user_A and user_B)
    today_str = now.date().isoformat()
    yesterday_str = (now - timedelta(days=1)).date().isoformat()
    yesterday_entry = next((e for e in result if e["date"] == yesterday_str), None)
    today_entry = next((e for e in result if e["date"] == today_str), None)

    assert yesterday_entry is not None
    assert yesterday_entry["visitors"] == 2  # user_A and user_B
    assert today_entry is not None
    assert today_entry["visitors"] == 1  # user_C


def test_get_daily_visitors_today_preset():
    """get_daily_visitors for 'today' returns exactly 1 entry."""
    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.all.return_value = []

    svc = DashboardAggregationService(db_session=fake_db)
    result = svc.get_daily_visitors(date_preset="today")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["visitors"] == 0


def test_get_daily_visitors_custom_range():
    """get_daily_visitors with a custom 3-day range returns exactly 3 entries."""
    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.all.return_value = []

    svc = DashboardAggregationService(db_session=fake_db)
    result = svc.get_daily_visitors(
        date_preset="custom",
        start_date_str="2026-08-20",
        end_date_str="2026-08-22",
    )

    assert len(result) == 3
    assert result[0]["date"] == "2026-08-20"
    assert result[1]["date"] == "2026-08-21"
    assert result[2]["date"] == "2026-08-22"


def test_get_daily_visitors_db_exception_fallback():
    """get_daily_visitors returns empty list if DB raises an exception."""
    fake_db = MagicMock()
    fake_db.query.side_effect = RuntimeError("db down")

    svc = DashboardAggregationService(db_session=fake_db)
    result = svc.get_daily_visitors(date_preset="7d")

    assert isinstance(result, list)
    assert result == []


def test_get_daily_visitors_uses_context_when_no_db():
    """get_daily_visitors falls back to get_db_session_context when db is None."""
    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.all.return_value = []

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=fake_db)
    ctx.__exit__ = MagicMock(return_value=None)

    svc = DashboardAggregationService(db_session=None)
    with patch("app.services.dashboard_aggregation_service.get_db_session_context", return_value=ctx):
        result = svc.get_daily_visitors(date_preset="7d")

    assert isinstance(result, list)
    assert len(result) == 7


def test_daily_visitors_api_endpoint_success():
    """GET /api/dashboard/daily-visitors returns 200 with expected structure."""
    fake_data = [{"date": "2026-08-27", "visitors": 5}, {"date": "2026-08-26", "visitors": 3}]
    with patch("app.api.dashboard.DashboardAggregationService.get_daily_visitors", return_value=fake_data):
        client = TestClient(app)
        res = client.get("/api/dashboard/daily-visitors?date_preset=7d")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        assert body["data"] == fake_data


def test_daily_visitors_api_endpoint_custom_range():
    """GET /api/dashboard/daily-visitors with custom range passes params through."""
    fake_data = [{"date": "2026-08-20", "visitors": 1}]
    with patch("app.api.dashboard.DashboardAggregationService.get_daily_visitors", return_value=fake_data):
        client = TestClient(app)
        res = client.get("/api/dashboard/daily-visitors?date_preset=custom&start_date=2026-08-20&end_date=2026-08-20")
        assert res.status_code == 200
        assert res.json()["data"][0]["date"] == "2026-08-20"


def test_daily_visitors_api_endpoint_error():
    """GET /api/dashboard/daily-visitors returns 500 on service exception."""
    with patch("app.api.dashboard.DashboardAggregationService.get_daily_visitors", side_effect=RuntimeError("fail")):
        client = TestClient(app)
        res = client.get("/api/dashboard/daily-visitors?date_preset=7d")
        assert res.status_code == 500
        assert res.json()["status"] == "error"


def test_user_classification_details_json_and_filters():
    """Test get_user_classification_details_json with various classification filters."""
    fake_db = MagicMock()

    s1 = SimpleNamespace(
        external_user_id="+919876543210",
        user_type=UserType.buyer,
        workflow_type=WorkflowType.registration,
        workflow_state={"registration_stage": "completed", "email": "b1@test.com"},
        outcome=ConversationOutcome.completed,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        retention_date=datetime.now(timezone.utc),
        session_id="s1"
    )

    s2 = SimpleNamespace(
        external_user_id="919876543211",
        user_type=UserType.seller,
        workflow_type=WorkflowType.seller_rfq_interest,
        workflow_state={"seller_subscribed": True, "email": "s1@test.com"},
        outcome=ConversationOutcome.completed,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        retention_date=datetime.now(timezone.utc),
        session_id="s2"
    )

    fake_db.query.return_value.filter.return_value.all.return_value = [s1, s2]
    fake_db.query.return_value.filter.return_value.first.return_value = None

    svc = DashboardAggregationService(db_session=fake_db)
    
    # Test all filter types
    filters = [
        "all", "unknown", "buyer", "buyer_registered", "buyer_not_registered",
        "buyer_rfq_created", "buyer_rfq_not_created", "seller", "seller_registered",
        "seller_not_registered", "seller_subscribed", "seller_without_subscription"
    ]
    for f in filters:
        res = svc.get_user_classification_details_json(date_preset="today", filter_type=f)
        assert res["status"] == "success"
        assert "users" in res





def test_dashboard_api_endpoints_coverage():
    """Test coverage for API endpoints in app/api/dashboard.py."""
    client = TestClient(app)

    # 1. GET /api/dashboard/user-classification-details
    fake_details = {"status": "success", "users": []}
    with patch("app.api.dashboard.DashboardAggregationService.get_user_classification_details_json", return_value=fake_details):
        res = client.get("/api/dashboard/user-classification-details?date_preset=today&filter_type=all")
        assert res.status_code == 200

    # 2. GET /api/dashboard/user-classification-details error fallback
    with patch("app.api.dashboard.DashboardAggregationService.get_user_classification_details_json", side_effect=RuntimeError("err")):
        res = client.get("/api/dashboard/user-classification-details?date_preset=today&filter_type=all")
        assert res.status_code == 500

    # 3. GET /api/dashboard/export-user-classification-csv
    with patch("app.api.dashboard.DashboardAggregationService.export_user_classification_csv", return_value="phone,category\n919876543210,buyer"):
        res = client.get("/api/dashboard/export-user-classification-csv?date_preset=today&filter_type=all")
        assert res.status_code == 200
        assert res.headers["content-type"] == "text/csv; charset=utf-8"

    # 4. GET /api/dashboard/export-user-classification-csv error fallback
    with patch("app.api.dashboard.DashboardAggregationService.export_user_classification_csv", side_effect=RuntimeError("csv err")):
        res = client.get("/api/dashboard/export-user-classification-csv?date_preset=today&filter_type=all")
        assert res.status_code == 500


def test_today_conversations_api_endpoint():
    """Test GET /api/dashboard/today-conversations success and error paths."""
    client = TestClient(app)

    # Success path
    fake_convos = [{"phone": "919876543210", "user_type": "buyer"}]
    with patch("app.api.dashboard.DashboardAggregationService.get_today_conversations", AsyncMock(return_value=fake_convos)):
        res = client.get("/api/dashboard/today-conversations")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "success"
        assert body["data"] == fake_convos

    # Error path
    with patch("app.api.dashboard.DashboardAggregationService.get_today_conversations", AsyncMock(side_effect=RuntimeError("db fail"))):
        res = client.get("/api/dashboard/today-conversations")
        assert res.status_code == 500
        assert res.json()["status"] == "error"


def test_conversation_messages_api_endpoint():
    """Test GET /api/dashboard/conversation-messages success and error paths."""
    client = TestClient(app)

    fake_result = {"status": "success", "messages": [{"role": "user", "content": "Hello"}]}

    # Success with phone param
    with patch("app.api.dashboard.DashboardAggregationService.get_conversation_messages", AsyncMock(return_value=fake_result)):
        res = client.get("/api/dashboard/conversation-messages?phone=919876543210")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

    # Success with session_id param
    with patch("app.api.dashboard.DashboardAggregationService.get_conversation_messages", AsyncMock(return_value=fake_result)):
        res = client.get("/api/dashboard/conversation-messages?session_id=sess_abc")
        assert res.status_code == 200

    # Error path
    with patch("app.api.dashboard.DashboardAggregationService.get_conversation_messages", AsyncMock(side_effect=RuntimeError("msg fail"))):
        res = client.get("/api/dashboard/conversation-messages?phone=919876543210")
        assert res.status_code == 500
        assert res.json()["status"] == "error"


def test_dashboard_aggregation_daily_visitors_logic():
    """Test get_daily_visitors direct implementation."""
    fake_db = MagicMock()
    d1 = datetime(2026, 8, 27, 10, 0)
    d2 = datetime(2026, 8, 26, 12, 0)
    fake_db.query.return_value.filter.return_value.all.return_value = [
        (d1, "919876543210"),
        (d1, "919876543210"),  # Duplicate user same day
        (d2, "919876543211"),
    ]
    svc = DashboardAggregationService(db_session=fake_db)
    result = svc.get_daily_visitors(date_preset="7d")
    assert len(result) >= 1
    assert any(r["visitors"] > 0 for r in result)


@pytest.mark.asyncio
async def test_dashboard_aggregation_today_conversations_and_messages_logic():
    """Test get_today_conversations and get_conversation_messages logic."""
    fake_db = MagicMock()
    s1 = SimpleNamespace(
        session_id="s1",
        external_user_id="919876543210",
        user_type=UserType.buyer,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        workflow_type=WorkflowType.registration,
        workflow_state={"status": "active"},
        outcome=ConversationOutcome.completed,
        rfq_id="r1",
        rfq_ids=["r1"],
        conversation_history={"messages": [{"sender": "user", "content": "Hello", "timestamp": "2026-08-27T10:00:00Z"}]}
    )
    s2 = SimpleNamespace(
        session_id="s2",
        external_user_id="919876543211",
        user_type=UserType.seller,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        workflow_type=WorkflowType.seller_rfq_interest,
        workflow_state={},
        outcome=ConversationOutcome.completed,
        rfq_id=None,
        rfq_ids=[],
        conversation_history='[{"role": "assistant", "content": "Hi", "timestamp": "2026-08-27T10:01:00Z"}]'
    )
    s3 = SimpleNamespace(
        session_id="s3",
        external_user_id="919876543212",
        user_type=UserType.unknown,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        workflow_type=None,
        workflow_state={},
        outcome=None,
        rfq_id=None,
        rfq_ids=[],
        conversation_history="invalid-json"
    )

    fake_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [s1, s2, s3]
    fake_db.query.return_value.filter.return_value.all.return_value = [s1, s2, s3]
    fake_db.query.return_value.filter.return_value.first.return_value = s1

    svc = DashboardAggregationService(db_session=fake_db)
    
    # 1. get_today_conversations
    today_convs = await svc.get_today_conversations()
    assert len(today_convs) >= 1

    # 2. get_conversation_messages by phone
    msgs_phone = await svc.get_conversation_messages(phone="919876543210")
    assert msgs_phone["status"] == "success"
    assert len(msgs_phone["messages"]) >= 1

    # 3. get_conversation_messages by session_id
    msgs_sess = await svc.get_conversation_messages(session_id="s1")
    assert msgs_sess["status"] == "success"

    # 4. get_conversation_messages neither provided
    msgs_empty = await svc.get_conversation_messages()
    assert msgs_empty["status"] == "error"


def test_export_user_classification_csv_all_filter_types():
    """Test export_user_classification_csv for every filter type branch."""
    fake_db = MagicMock()
    s_buyer = SimpleNamespace(
        external_user_id="919876543210",
        user_type=UserType.buyer,
        workflow_type=WorkflowType.registration,
        workflow_state={"registration_stage": "completed"},
        outcome=ConversationOutcome.completed,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        retention_date=datetime.now(timezone.utc),
        session_id="s1"
    )
    s_seller = SimpleNamespace(
        external_user_id="919876543211",
        user_type=UserType.seller,
        workflow_type=WorkflowType.seller_rfq_interest,
        workflow_state={"seller_subscribed": True},
        outcome=ConversationOutcome.completed,
        created_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        retention_date=datetime.now(timezone.utc),
        session_id="s2"
    )
    fake_db.query.return_value.filter.return_value.all.return_value = [s_buyer, s_seller]
    fake_db.query.return_value.filter.return_value.first.return_value = None

    svc = DashboardAggregationService(db_session=fake_db)
    filter_types = [
        "all", "unknown", "buyer", "buyer_registered", "buyer_not_registered",
        "buyer_rfq_created", "buyer_rfq_not_created", "seller", "seller_registered",
        "seller_not_registered", "seller_subscribed", "seller_without_subscription"
    ]
    for ft in filter_types:
        csv_out = svc.export_user_classification_csv(date_preset="today", filter_type=ft)
        assert "Phone Number" in csv_out


@pytest.mark.asyncio
async def test_redis_db_services_and_operations():
    """Test BaseRedisService, SessionRedisService, and AuthRedisService operations."""
    from app.redis_db import (
        BaseRedisService,
        SessionRedisService,
        AuthRedisService,
        get_redis_service,
        get_auth_redis_service,
        get_session_redis_service,
    )

    base = BaseRedisService()
    auth = AuthRedisService()
    sess = SessionRedisService()
    assert get_redis_service() is not None
    assert get_auth_redis_service() is not None
    assert get_session_redis_service() is not None

    mock_client = MagicMock()
    mock_client.set = AsyncMock(return_value=True)
    mock_client.get = AsyncMock(return_value=json.dumps({"session_id": "s1", "conversation_history": {"messages": []}}))
    mock_client.delete = AsyncMock(return_value=1)
    mock_client.exists = AsyncMock(return_value=1)
    mock_client.ttl = AsyncMock(return_value=300)
    mock_client.expire = AsyncMock(return_value=True)
    mock_client.expireat = AsyncMock(return_value=True)

    with patch("app.redis_db.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=mock_client)):
        base.client = mock_client
        sess.client = mock_client
        auth.client = mock_client

        # Base operations
        assert await base.set("k1", {"foo": "bar"}, ex=60) is True
        assert await base.get("k1", as_json=True) == {"session_id": "s1", "conversation_history": {"messages": []}}
        assert await base.exists("k1") is True
        assert await base.ttl("k1") == 300
        assert await base.expire("k1", 100) is True
        assert await base.expireat("k1", 1234567890) is True
        assert await base.delete("k1") is True

        # Session operations
        assert await sess.set_user_active_session_id("919876543210", "s1") is True
        assert await sess.get_user_active_session_id("919876543210") is not None
        assert await sess.clear_user_active_session_id("919876543210") is True
        assert await sess.append_message_to_history("s1", "user", "Hello there") is True


def test_api_dashboard_remaining_error_endpoints():
    """Test API error handling in get_dashboard_stats, get_recent_feed, and export_dashboard_data."""
    client = TestClient(app)

    # 1. GET /api/dashboard/stats error
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(side_effect=RuntimeError("stats fail"))):
        res = client.get("/api/dashboard/stats")
        assert res.status_code == 500

    # 2. GET /api/dashboard/feed error
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_rt.return_value.get_recent_feed = AsyncMock(side_effect=RuntimeError("feed fail"))
        res = client.get("/api/dashboard/feed")
        assert res.status_code == 500

    # 3. GET /api/dashboard/active-users error
    with patch("app.api.dashboard.get_realtime_analytics_service") as mock_rt:
        mock_rt.return_value.get_active_users_count = AsyncMock(side_effect=RuntimeError("active fail"))
        res = client.get("/api/dashboard/active-users")
        assert res.status_code == 500

    # 4. GET /api/dashboard/export error
    with patch("app.api.dashboard.DashboardAggregationService.get_dashboard_stats", AsyncMock(side_effect=RuntimeError("export fail"))):
        res = client.get("/api/dashboard/export")
        assert res.status_code == 500


@pytest.mark.asyncio
async def test_dashboard_sse_stream_all_branches():
    """Test SSE live_events_stream branches in app/api/dashboard.py."""
    from app.api.dashboard import live_events_stream
    from starlette.requests import Request

    # 1. Stream with active events and disconnection
    async def fake_events():
        yield {"event_type": "visitor", "data": 1}
        yield {"event_type": "heartbeat", "data": 2}

    mock_rt = MagicMock()
    mock_rt.subscribe_events = fake_events

    fake_scope = {"type": "http", "method": "GET", "path": "/api/dashboard/live-stream", "headers": []}
    fake_request = Request(fake_scope)
    fake_request.is_disconnected = AsyncMock(side_effect=[False, False, True])

    with patch("app.api.dashboard.get_realtime_analytics_service", return_value=mock_rt):
        resp = await live_events_stream(fake_request)
        assert resp.status_code == 200
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        assert len(chunks) >= 2

    # 2. Stream with Exception inside generator
    async def error_events():
        raise RuntimeError("stream failure")
        yield {}

    mock_rt.subscribe_events = error_events
    with patch("app.api.dashboard.get_realtime_analytics_service", return_value=mock_rt):
        resp = await live_events_stream(fake_request)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        assert len(chunks) >= 1


@pytest.mark.asyncio
async def test_realtime_analytics_service_comprehensive():
    """Test RealtimeAnalyticsService publish, heartbeat, and subscription edge cases."""
    from app.services.realtime_analytics_service import RealtimeAnalyticsService

    svc = RealtimeAnalyticsService()
    
    # 1. Test publish_event Redis failure fallback
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    mock_redis.lpush = AsyncMock(side_effect=RuntimeError("redis down"))
    mock_redis.ltrim = AsyncMock(side_effect=RuntimeError("redis down"))
    mock_redis.zremrangebyscore = AsyncMock(side_effect=RuntimeError("redis down"))
    mock_redis.zcount = AsyncMock(side_effect=RuntimeError("redis down"))
    mock_redis.zrange = AsyncMock(return_value=[b'{"user_type": "buyer"}', b'invalid-json', b'{"user_type": "seller"}'])
    mock_redis.lrange = AsyncMock(return_value=[json.dumps({"event_type": "test"})])

    with patch("app.redis_db.AsyncRedisConnectionManager.get_client", AsyncMock(return_value=mock_redis)):
        await svc.publish_event("test_event", {"foo": "bar"})
        await svc.record_heartbeat("919876543210", user_type="buyer")

        # Counts with invalid JSON in Redis
        counts = await svc.get_active_users_count()
        assert counts["total"] >= 0

        # Feed with Redis
        feed = await svc.get_recent_feed(limit=5)
        assert len(feed) >= 1


@pytest.mark.asyncio
async def test_dashboard_aggregation_full_metrics_coverage():
    """Execute complete _generate_metrics calculation path with real SQLite DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import ConversationSession, RFQ, Seller, ProductCategory, RFQNotificationFact, UserType, SessionState, ConversationOutcome, RFQStatus

    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    now = datetime.utcnow()

    # Seed realistic rows
    s1 = ConversationSession(
        session_id="s1",
        external_user_id="919876543210",
        user_type=UserType.buyer,
        session_state=SessionState.active,
        outcome=ConversationOutcome.completed,
        workflow_type=WorkflowType.registration,
        workflow_state={"registration_stage": "completed", "user_id": "u1"},
        rfq_id="r1",
        created_at=now,
        last_activity_at=now,
        retention_date=now.date(),
        conversation_history={"messages": [{"role": "user", "content": "Hi"}]}
    )
    s2 = ConversationSession(
        session_id="s2",
        external_user_id="919876543211",
        user_type=UserType.seller,
        session_state=SessionState.active,
        outcome=ConversationOutcome.abandoned,
        workflow_type=WorkflowType.seller_rfq_interest,
        workflow_state={},
        created_at=now,
        last_activity_at=now,
        retention_date=now.date(),
        conversation_history={"messages": []}
    )
    r1 = RFQ(
        rfq_id="r1",
        external_user_id="919876543210",
        status=RFQStatus.submitted,
        created_at=now,
        api_payload={"product_name": "Steel"}
    )
    sel1 = Seller(
        seller_id="sel1",
        phone_number="919876543211",
        seller_name="Acme Sellers",
        categories=["Fasteners"],
        location="Mumbai",
        subscription_credits=10
    )
    db.add_all([s1, s2, r1, sel1])
    db.commit()

    svc = DashboardAggregationService(db_session=db)
    with patch.object(svc.realtime_service, "get_active_users_count", AsyncMock(return_value={"total": 2, "buyers": 1, "sellers": 1, "unknown": 0})), \
         patch.object(svc.realtime_service, "get_recent_feed", AsyncMock(return_value=[{"event_type": "visitor"}])):

        stats = await svc.get_dashboard_stats(date_preset="today", role="buyer", rfq_status="submitted")
        assert stats["status"] == "success"
        assert "executive_kpis" in stats
        assert "marketplace_health" in stats
        assert "buyer_funnel" in stats
        assert "seller_funnel" in stats

        # Test presets: yesterday, 7d, 30d, 90d, custom
        stats_y = await svc.get_dashboard_stats(date_preset="yesterday", role="seller")
        assert stats_y["status"] == "success"

        stats_7d = await svc.get_dashboard_stats(date_preset="7d", role="all")
        assert stats_7d["status"] == "success"

        stats_30d = await svc.get_dashboard_stats(date_preset="30d", category="Fasteners")
        assert stats_30d["status"] == "success"

        stats_90d = await svc.get_dashboard_stats(date_preset="90d", location="Mumbai")
        assert stats_90d["status"] == "success"

        stats_custom = await svc.get_dashboard_stats(date_preset="custom", start_date="2026-01-01", end_date="2026-12-31")
        assert stats_custom["status"] == "success"

        # Test get_user_classification_details_json
        for ftype in ["all", "unknown", "buyer", "buyer_registered", "buyer_not_registered", "buyer_rfq_created", "buyer_rfq_not_created", "seller", "seller_registered", "seller_not_registered", "seller_subscribed", "seller_without_subscription"]:
            details = svc.get_user_classification_details_json("today", filter_type=ftype)
            assert "users" in details
            assert "total_matching" in details

    db.close()


def test_api_dashboard_all_remaining_routes():
    """Test all dashboard API endpoints for 200 and 500 error cases."""
    client = TestClient(app)

    # 1. /api/dashboard/user-classification-details
    with patch("app.api.dashboard.DashboardAggregationService.get_user_classification_details_json", return_value={"users": [], "total_matching": 0}):
        res = client.get("/api/dashboard/user-classification-details?date_preset=today&filter_type=buyer")
        assert res.status_code == 200

    with patch("app.api.dashboard.DashboardAggregationService.get_user_classification_details_json", side_effect=RuntimeError("err")):
        res = client.get("/api/dashboard/user-classification-details")
        assert res.status_code == 500

    # 2. /api/dashboard/export-user-classification-csv
    with patch("app.api.dashboard.DashboardAggregationService.export_user_classification_csv", return_value="phone,type\n919876543210,buyer\n"):
        res = client.get("/api/dashboard/export-user-classification-csv?date_preset=today&filter_type=buyer")
        assert res.status_code == 200
        assert "attachment" in res.headers.get("Content-Disposition", "")

    with patch("app.api.dashboard.DashboardAggregationService.export_user_classification_csv", side_effect=RuntimeError("err")):
        res = client.get("/api/dashboard/export-user-classification-csv")
        assert res.status_code == 500

    # 3. /api/dashboard/daily-visitors
    with patch("app.api.dashboard.DashboardAggregationService.get_daily_visitors", return_value=[{"date": "2026-08-31", "visitors": 5}]):
        res = client.get("/api/dashboard/daily-visitors?date_preset=7d")
        assert res.status_code == 200

    with patch("app.api.dashboard.DashboardAggregationService.get_daily_visitors", side_effect=RuntimeError("err")):
        res = client.get("/api/dashboard/daily-visitors")
        assert res.status_code == 500

    # 4. /api/dashboard/today-conversations
    with patch("app.api.dashboard.DashboardAggregationService.get_today_conversations", AsyncMock(return_value=[{"phone": "919876543210"}])):
        res = client.get("/api/dashboard/today-conversations")
        assert res.status_code == 200

    with patch("app.api.dashboard.DashboardAggregationService.get_today_conversations", AsyncMock(side_effect=RuntimeError("err"))):
        res = client.get("/api/dashboard/today-conversations")
        assert res.status_code == 500

    # 5. /api/dashboard/conversation-messages
    with patch("app.api.dashboard.DashboardAggregationService.get_conversation_messages", AsyncMock(return_value={"messages": []})):
        res = client.get("/api/dashboard/conversation-messages?phone=919876543210")
        assert res.status_code == 200

    with patch("app.api.dashboard.DashboardAggregationService.get_conversation_messages", AsyncMock(side_effect=RuntimeError("err"))):
        res = client.get("/api/dashboard/conversation-messages?phone=919876543210")
        assert res.status_code == 500


@pytest.mark.asyncio
async def test_realtime_analytics_service_redis_disabled():
    """Test RealtimeAnalyticsService when redis is disabled."""
    from app.services.realtime_analytics_service import RealtimeAnalyticsService, get_realtime_analytics_service

    svc = RealtimeAnalyticsService()
    svc.settings.redis_session_storage_enabled = False

    # 1. record_heartbeat with redis disabled
    await svc.record_heartbeat("919876543210", "buyer")

    # 2. get_active_users_count with redis disabled
    counts = await svc.get_active_users_count()
    assert counts["total"] == 0

    # 3. get_recent_feed with DB fallback
    feed = await svc.get_recent_feed(limit=5)
    assert isinstance(feed, list)

    # 4. singleton function
    singleton = get_realtime_analytics_service()
    assert singleton is not None



