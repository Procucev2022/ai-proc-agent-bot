"""Live chat viewer filter: list and open a date range and/or one phone number.

The live list and the conversation view take an optional From/To (IST days) and
phone. Without them both endpoints behave exactly as before: today's users, and
each user's whole history. Runs the real service against an in-memory SQLite
database, with Redis mocked for live sessions.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import ConversationSession, UserType
from app.services import dashboard_aggregation_service as agg
from app.services.dashboard_aggregation_service import DashboardAggregationService

REDIS = "app.redis_db.AsyncRedisConnectionManager.get_client"
BUYER = "919876543210"
SELLER = "918660216900"


def _naive(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)


def _session(session_id, phone, user_type, started, messages, last_active=None):
    return ConversationSession(
        session_id=session_id,
        external_user_id=phone,
        user_type=user_type,
        workflow_state={},
        conversation_history={"messages": messages},
        retention_date=date(2027, 1, 1),
        started_at=_naive(started),
        created_at=_naive(started),
        last_activity_at=_naive(last_active or started),
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        # Buyer on 10 Nov (IST).
        _session("buyer_1010", BUYER, UserType.buyer, "2026-11-10T05:00:00Z", [
            {"role": "user", "content": "Hello", "timestamp": "2026-11-10T05:00:00Z"},
            {"role": "assistant", "content": "Welcome buyer", "timestamp": "2026-11-10T05:00:05Z"},
        ]),
        # Seller session crossing midnight IST: 12 Nov 23:30 IST, then 13 Nov 00:30 IST.
        _session("seller_1213", "+" + SELLER, UserType.seller, "2026-11-12T18:00:00Z", [
            {"role": "user", "content": "Cable", "timestamp": "2026-11-12T18:00:00Z"},
            {"role": "assistant", "content": "Got it, cable", "timestamp": "2026-11-12T19:00:00Z"},
        ], last_active="2026-11-12T19:00:00Z"),
        # Seller again on 13 Nov as the bare number; one message has no time.
        _session("seller_1313", SELLER[2:], UserType.seller, "2026-11-13T06:54:00Z", [
            {"role": "user", "content": "laptop", "timestamp": "2026-11-13T06:55:00Z"},
            {"role": "assistant", "content": {"text": "Thanks"}},
        ]),
    ])
    session.commit()
    yield session
    session.close()


def _redis(sessions):
    client = MagicMock()
    keys = [f"session:{i}" for i in range(len(sessions))]

    async def scan_iter(match):
        for key in keys:
            yield key

    async def get(key):
        return sessions[keys.index(key)]

    client.scan_iter = scan_iter
    client.get = get
    client.zrange = AsyncMock(return_value=[])
    return client


async def _list(db, redis_client=None, **kwargs):
    with patch(REDIS, AsyncMock(return_value=redis_client)):
        return await DashboardAggregationService(db_session=db).get_today_conversations(**kwargs)


async def _messages(db, redis_client=None, **kwargs):
    with patch(REDIS, AsyncMock(return_value=redis_client)):
        return await DashboardAggregationService(db_session=db).get_conversation_messages(**kwargs)


def _by_phone(users):
    return {u["normalized_phone"]: u for u in users}


# --------------------------------------------------------------- live list

@pytest.mark.asyncio
async def test_range_lists_only_users_with_messages_in_it_counting_only_those(db):
    users = _by_phone(await _list(db, start_date_str="2026-11-13", end_date_str="2026-11-13"))

    # 13 Nov IST: the seller's 00:30 message and the 13 Nov session (its untimed
    # message is placed at the session's start, 13 Nov).
    assert set(users) == {SELLER[2:]}
    seller = users[SELLER[2:]]
    assert seller["total_messages"] == 3
    assert seller["session_count"] == 2
    assert seller["latest_message_preview"] == "Interactive message"


@pytest.mark.asyncio
async def test_range_covering_every_day_lists_everyone(db):
    users = _by_phone(await _list(db, start_date_str="2026-11-01", end_date_str="2026-11-30"))

    assert set(users) == {BUYER[2:], SELLER[2:]}
    assert users[BUYER[2:]]["total_messages"] == 2
    assert users[SELLER[2:]]["total_messages"] == 4


@pytest.mark.asyncio
async def test_phone_filter_matches_any_spelling_and_excludes_others(db):
    users = await _list(db, start_date_str="2026-11-01", end_date_str="2026-11-30", phone="+91 86602 16900")

    assert [u["normalized_phone"] for u in users] == [SELLER[2:]]
    assert users[0]["total_messages"] == 4


@pytest.mark.asyncio
async def test_range_with_no_messages_is_empty(db):
    assert await _list(db, start_date_str="2026-10-01", end_date_str="2026-10-31") == []


@pytest.mark.asyncio
async def test_live_redis_sessions_respect_range_and_phone(db):
    live_other = json.dumps({
        "session_id": "live_other", "external_user_id": "917000000001", "user_type": "buyer",
        "started_at": "2026-11-13T07:00:00Z", "last_activity_at": "2026-11-13T07:00:00Z",
        "conversation_history": {"messages": [
            {"role": "user", "content": "Hi live", "timestamp": "2026-11-13T07:00:00Z"}]},
    })
    live_seller_old = json.dumps({
        "session_id": "live_seller", "external_user_id": SELLER, "user_type": "seller",
        "conversation_history": {"messages": [
            {"role": "user", "content": "October", "timestamp": "2026-10-01T07:00:00Z"}]},
    })
    redis_client = _redis([live_other, live_seller_old])

    users = _by_phone(await _list(db, redis_client, start_date_str="2026-11-13", end_date_str="2026-11-13"))
    assert set(users) == {SELLER[2:], "7000000001"}
    assert users[SELLER[2:]]["total_messages"] == 3  # the October live message is out of range

    only_other = await _list(db, _redis([live_other, live_seller_old]),
                             start_date_str="2026-11-13", end_date_str="2026-11-13", phone="7000000001")
    assert [u["normalized_phone"] for u in only_other] == ["7000000001"]


@pytest.mark.asyncio
async def test_no_arguments_is_still_todays_live_list(db):
    fixed_now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    with patch.object(agg, "datetime", FrozenDatetime):
        users = _by_phone(await _list(db))

    # Today (UTC day 13 Nov): only the 13 Nov session; its whole history is shown.
    assert set(users) == {SELLER[2:]}
    assert users[SELLER[2:]]["total_messages"] == 2


NOV_1_2 = (datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize(("message", "day_range", "session_started", "kept"), [
    ({"timestamp": "2026-11-13T00:00:00Z"}, None, None, True),
    ("not-a-dict", NOV_1_2, None, False),
    ({"content": "no time at all"}, NOV_1_2, None, True),
    ({"content": "untimed, session in range"}, NOV_1_2, "2026-11-01T08:00:00Z", True),
    ({"content": "untimed, session out of range"}, NOV_1_2, datetime(2026, 11, 5), False),
    ({"timestamp": "2026-11-01T12:00:00Z"}, NOV_1_2, "2026-11-20T00:00:00Z", True),
    ({"timestamp": "2026-11-03T00:00:00Z"}, NOV_1_2, None, False),
])
def test_message_in_range(message, day_range, session_started, kept):
    assert agg._message_in_range(message, day_range, session_started) is kept


def test_messages_in_range_tolerates_a_malformed_history():
    assert agg._messages_in_range({"not": "a list"}, NOV_1_2) == []
    assert agg._messages_in_range(None, None) == []


# ------------------------------------------------------- conversation view

@pytest.mark.asyncio
async def test_conversation_range_keeps_only_messages_sent_in_it(db):
    result = await _messages(db, phone=SELLER, start_date_str="2026-11-12", end_date_str="2026-11-12")

    assert [m["content"] for m in result["messages"]] == ["Cable"]
    assert result["session_info"]["total_sessions"] == 2


@pytest.mark.asyncio
async def test_conversation_without_range_is_still_the_whole_history(db):
    result = await _messages(db, phone=SELLER)

    assert [m["content"] for m in result["messages"]] == ["Cable", "Got it, cable", "laptop", "Thanks"]


# -------------------------------------------------------------------- API

@pytest.fixture
def client():
    from app.main import app
    from app.api.dashboard import get_dashboard_db
    from app.database import get_db_session

    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[get_dashboard_db] = lambda: MagicMock()
    with patch("app.api.dashboard.get_settings", return_value=SimpleNamespace(dashboard_api_key=None)):
        yield TestClient(app)
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_dashboard_db, None)


LIST = "app.api.dashboard.DashboardAggregationService.get_today_conversations"
MESSAGES = "app.api.dashboard.DashboardAggregationService.get_conversation_messages"


def test_api_list_without_filters_calls_the_service_exactly_as_before(client):
    service = AsyncMock(return_value=[{"phone": BUYER}])
    with patch(LIST, service):
        res = client.get("/api/dashboard/today-conversations")
    assert res.status_code == 200 and res.json() == {"status": "success", "data": [{"phone": BUYER}]}
    assert service.await_args.args == () and service.await_args.kwargs == {}


def test_api_list_passes_range_and_phone(client):
    service = AsyncMock(return_value=[])
    with patch(LIST, service):
        res = client.get("/api/dashboard/today-conversations?start_date=2026-11-10&end_date=2026-11-13&phone=8660216900")
    assert res.status_code == 200
    assert service.await_args.kwargs == {
        "start_date_str": "2026-11-10", "end_date_str": "2026-11-13", "phone": "8660216900"}


def test_api_messages_without_range_calls_the_service_exactly_as_before(client):
    service = AsyncMock(return_value={"status": "success", "messages": []})
    with patch(MESSAGES, service):
        res = client.get(f"/api/dashboard/conversation-messages?phone={BUYER}")
    assert res.status_code == 200
    assert service.await_args.kwargs == {"phone": BUYER, "session_id": None}


def test_api_messages_passes_range(client):
    service = AsyncMock(return_value={"status": "success", "messages": []})
    with patch(MESSAGES, service):
        client.get(f"/api/dashboard/conversation-messages?phone={BUYER}&start_date=2026-11-10&end_date=2026-11-10")
    assert service.await_args.kwargs == {
        "phone": BUYER, "session_id": None, "start_date_str": "2026-11-10", "end_date_str": "2026-11-10"}


@pytest.mark.parametrize("path", ["today-conversations?", f"conversation-messages?phone={BUYER}&"])
@pytest.mark.parametrize(("query", "message"), [
    ("start_date=2026-11-10", "Choose a valid From and To date (YYYY-MM-DD)"),
    ("start_date=13-11-2026&end_date=2026-11-13", "Choose a valid From and To date (YYYY-MM-DD)"),
    ("start_date=2026-11-14&end_date=2026-11-13", "From date must be on or before To date"),
])
def test_api_rejects_bad_ranges_without_querying(client, path, query, message):
    list_service, messages_service = AsyncMock(), AsyncMock()
    with patch(LIST, list_service), patch(MESSAGES, messages_service):
        res = client.get(f"/api/dashboard/{path}{query}")
    assert res.status_code == 400 and res.json()["message"] == message
    list_service.assert_not_awaited()
    messages_service.assert_not_awaited()


def test_dashboard_page_has_the_apply_filter_controls():
    html = (Path(__file__).resolve().parents[2] / "templates" / "dashboard.html").read_text(encoding="utf-8")
    for marker in ('id="btn-apply-chat-filter"', 'onclick="applyChatFilter()"', 'data-range="7d"',
                   'id="chat-export-from"', 'id="chat-export-phone"', 'id="btn-export-chat-history"',
                   'setInterval(refreshLiveConversations, 8000)'):
        assert marker in html
