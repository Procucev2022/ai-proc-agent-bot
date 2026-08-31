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
