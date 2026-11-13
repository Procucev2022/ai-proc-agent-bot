"""Chat history CSV download: any past date, all users or one user.

Runs the real export against an in-memory SQLite database seeded across several
days, users and phone spellings, with Redis mocked for live sessions.
"""
import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
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
        # Buyer on 10 Nov (IST), stored with the 91 prefix.
        _session("buyer_1010", BUYER, UserType.buyer, "2026-11-10T05:00:00Z", [
            {"role": "user", "content": "Hello", "timestamp": "2026-11-10T05:00:00Z"},
            {"role": "assistant", "type": "interactive", "timestamp": "2026-11-10T05:00:05Z", "content": {
                "header": "Welcome", "body": "Choose an option", "footer": "Procucev",
                "action": {"buttons": [{"reply": {"id": "buy", "title": "Buyer"}}, {"reply": {"id": "sell"}}]},
            }},
        ]),
        # Seller session crossing midnight IST: 12 Nov 23:30 IST and 13 Nov 00:30 IST.
        _session("seller_1213", "+" + SELLER, UserType.seller, "2026-11-12T18:00:00Z", [
            {"sender": "user", "content": "Cable", "timestamp": "2026-11-12T18:00:00Z"},
            {"role": "assistant", "content": "What's your products or services offered?",
             "timestamp": "2026-11-12T19:00:00Z"},
        ], last_active="2026-11-12T19:00:00Z"),
        # Seller again on 13 Nov, stored as the bare 10-digit number; epoch and missing times.
        _session("seller_1313", SELLER[2:], UserType.seller, "2026-11-13T06:54:00Z", [
            {"role": "user", "content": "laptop", "timestamp": 1794552900.0},  # 2026-11-13T06:55:00Z
            {"role": "assistant", "content": {"text": "Thanks"}},  # no timestamp: uses session start
            "not-a-message",
        ]),
    ])
    session.commit()
    yield session
    session.close()


def _rows(csv_text):
    assert csv_text.startswith("﻿")
    return list(csv.reader(io.StringIO(csv_text[1:])))


async def _export(db, redis_client=None, **kwargs):
    with patch(REDIS, AsyncMock(return_value=redis_client)):
        return _rows(await DashboardAggregationService(db_session=db).export_chat_history_csv(**kwargs))


@pytest.mark.asyncio
async def test_all_dates_all_users_grouped_per_user_in_time_order(db):
    rows = await _export(db)
    assert rows[0] == agg.CHAT_EXPORT_HEADERS
    body = rows[1:]
    # Seller (normalised 8660216900) sorts before buyer (9876543210); each block chronological.
    assert [r[6] for r in body] == [
        "Cable", "What's your products or services offered?", "Thanks", "laptop",
        "Hello", "Welcome\nChoose an option\nProcucev",
    ]
    hello = body[4]
    assert hello[:6] == [BUYER, "buyer", "buyer_1010", "2026-11-10", "10:30:00", "User"]
    interactive = body[5]
    assert interactive[5] == "Bot" and interactive[7] == "Buyer | sell" and interactive[8] == "interactive"


@pytest.mark.asyncio
async def test_one_user_matches_every_stored_phone_spelling(db):
    for spelling in (SELLER, "+" + SELLER, SELLER[2:], "+91 86602-16900"):
        rows = await _export(db, phone=spelling)
        assert {r[2] for r in rows[1:]} == {"seller_1213", "seller_1313"}, spelling
    assert await _export(db, phone="919999999999") == [agg.CHAT_EXPORT_HEADERS]


@pytest.mark.asyncio
async def test_custom_range_uses_ist_calendar_days(db):
    rows = await _export(db, date_preset="custom", start_date_str="2026-11-13", end_date_str="2026-11-13")
    # 18:00Z is 12 Nov 23:30 IST (out); 19:00Z is 13 Nov 00:30 IST (in).
    assert [(r[3], r[4], r[6]) for r in rows[1:]] == [
        ("2026-11-13", "00:30:00", "What's your products or services offered?"),
        ("2026-11-13", "12:24:00", "Thanks"),
        ("2026-11-13", "12:25:00", "laptop"),
    ]
    old = await _export(db, date_preset="custom", start_date_str="2026-11-10", end_date_str="2026-11-10", phone=BUYER)
    assert [r[6] for r in old[1:]] == ["Hello", "Welcome\nChoose an option\nProcucev"]
    assert await _export(db, date_preset="custom", start_date_str="2026-11-01", end_date_str="2026-11-02") == [
        agg.CHAT_EXPORT_HEADERS
    ]


@pytest.mark.asyncio
async def test_dashboard_presets_and_invalid_custom_fall_back_like_the_dashboard(db):
    service = DashboardAggregationService(db_session=db)
    start = datetime(2026, 11, 13, tzinfo=timezone.utc)
    with patch.object(service, "_resolve_date_range", return_value=(start, start + timedelta(days=1), None, None)) as resolve, \
            patch(REDIS, AsyncMock(return_value=None)):
        assert len(_rows(await service.export_chat_history_csv(date_preset="7d"))) == 3
        await service.export_chat_history_csv(date_preset="custom", start_date_str="bad", end_date_str=None)
    assert [c.args[0] for c in resolve.call_args_list] == ["7d", "custom"]


def _redis(sessions):
    client = MagicMock()
    keys = [f"session:{i}" for i in range(len(sessions))]

    async def scan_iter(match):
        for key in keys:
            yield key

    async def get(key):
        value = sessions[keys.index(key)]
        if isinstance(value, Exception):
            raise value
        return value

    client.scan_iter = scan_iter
    client.get = get
    return client


@pytest.mark.asyncio
async def test_live_redis_sessions_merge_and_respect_the_phone_filter(db):
    live = {
        "session_id": "seller_1313", "external_user_id": "+91" + SELLER[2:], "user_type": "seller",
        "started_at": "2026-11-13T06:54:00Z",
        "conversation_history": json.dumps({"messages": [
            {"role": "user", "content": "laptop", "timestamp": "2026-11-13T06:55:00Z"},
            {"role": "assistant", "content": "Thanks", "timestamp": "2026-11-13T06:55:02Z"},
            {"role": "user", "content": "Yes", "timestamp": "2026-11-13T06:56:00Z"},
        ]}),
    }
    stale = {"session_id": "buyer_1010", "external_user_id": BUYER, "conversation_history": {"messages": []}}
    new_live = {"session_id": "new", "external_user_id": "917000000000", "last_activity_at": 1794552900,
                "conversation_history": [{"role": "user", "content": "Hi"}]}
    broken = [None, b"not json", json.dumps({"session_id": "x"}), RuntimeError("redis read")]
    client = _redis([json.dumps(live), json.dumps(stale), json.dumps(new_live), *broken])

    rows = await _export(db, redis_client=client, phone=SELLER)
    assert [r[6] for r in rows[1:]][-3:] == ["laptop", "Thanks", "Yes"]  # live copy replaced the stored one

    everyone = await _export(db, redis_client=client)
    texts = [r[6] for r in everyone[1:]]
    assert "Hello" in texts  # stored session kept: the empty Redis copy is less complete
    assert ["917000000000", "unknown", "new"] == [r for r in everyone[1:] if r[6] == "Hi"][0][:3]


@pytest.mark.asyncio
async def test_redis_unavailable_still_exports_stored_chats(db):
    with patch(REDIS, AsyncMock(side_effect=RuntimeError("redis down"))):
        rows = _rows(await DashboardAggregationService(db_session=db).export_chat_history_csv())
    assert len(rows) == 7


@pytest.mark.asyncio
async def test_database_error_is_raised_to_the_api():
    broken_db = MagicMock()
    broken_db.query.side_effect = RuntimeError("db down")
    with patch(REDIS, AsyncMock(return_value=None)), pytest.raises(RuntimeError, match="db down"):
        await DashboardAggregationService(db_session=broken_db).export_chat_history_csv()


@pytest.mark.asyncio
async def test_uses_its_own_session_when_none_is_injected(db):
    class _Context:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    with patch.object(agg, "get_db_session_context", return_value=_Context()):
        rows = await _export(None)
    assert len(rows) == 7


def test_helpers_cover_every_stored_shape():
    assert agg._parse_message_time(None) is None and agg._parse_message_time(True) is None
    assert agg._parse_message_time("  ") is None and agg._parse_message_time([1]) is None
    assert agg._parse_message_time("not a date") is None
    assert agg._parse_message_time("1794552900") == datetime(2026, 11, 13, 6, 55, tzinfo=timezone.utc)
    assert agg._parse_message_time(1e20) is None and agg._parse_message_time("1e20") is None
    assert agg._parse_message_time(datetime(2026, 1, 1)).tzinfo == timezone.utc
    assert agg._parse_message_time("2026-01-01T05:30:00+05:30") == datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert agg._history_messages("{bad") == [] and agg._history_messages(None) == []
    assert agg._history_messages('[{"a": 1}]') == [{"a": 1}]
    assert agg._history_messages({"messages": "x"}) == []

    assert agg._message_text_and_buttons(None) == ("", "")
    assert agg._message_text_and_buttons(5) == ("5", "")
    assert agg._message_text_and_buttons({"message": "hi", "buttons": "x"}) == ("hi", "")
    assert agg._message_text_and_buttons({"body": {"text": "b"}, "buttons": [{"id": "", "title": ""}, "x"]}) == ("b", "")
    assert agg._message_text_and_buttons({"buttons": [{"id": "1", "title": "One"}]}) == ("", "One")

    assert agg._ist_day_range("2026-11-13", "bad") is None
    # Duplicates collapse: the 91-prefixed spelling equals the original phone here.
    assert agg._export_phone_variants(SELLER, SELLER[2:]) == [SELLER, SELLER[2:], "+" + SELLER[2:], "+" + SELLER]
    snap = agg._export_session_snapshot("s", None, SimpleNamespace(value=""), None, None)
    assert snap["user_type"] == "unknown" and snap["phone"] == "" and snap["messages"] == []


# ------------------------------------------------------------------- API endpoint

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


EXPORT = "app.api.dashboard.DashboardAggregationService.export_chat_history_csv"


def test_api_downloads_csv_with_a_descriptive_filename(client):
    export = AsyncMock(return_value="﻿Phone Number\n")
    with patch(EXPORT, export):
        res = client.get("/api/dashboard/export-chat-history-csv?date_preset=custom"
                         "&start_date=2026-11-10&end_date=2026-11-13&phone=%2B91%2086602%2016900")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"] == (
        'attachment; filename="chat_history_918660216900_2026-11-10_to_2026-11-13.csv"'
    )
    assert export.await_args.kwargs == {
        "date_preset": "custom", "start_date_str": "2026-11-10", "end_date_str": "2026-11-13",
        "phone": "+91 86602 16900",
    }

    with patch(EXPORT, AsyncMock(return_value="x")):
        res = client.get("/api/dashboard/export-chat-history-csv")
    assert res.headers["content-disposition"] == 'attachment; filename="chat_history_all_users_all.csv"'


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("date_preset=forever", "Unknown date range"),
        ("date_preset=custom&start_date=2026-11-10", "Choose a valid From and To date (YYYY-MM-DD)"),
        ("date_preset=custom&start_date=13-11-2026&end_date=2026-11-13", "Choose a valid From and To date (YYYY-MM-DD)"),
        ("date_preset=custom&start_date=2026-11-14&end_date=2026-11-13", "From date must be on or before To date"),
    ],
)
def test_api_rejects_bad_ranges_without_querying(client, query, message):
    export = AsyncMock()
    with patch(EXPORT, export):
        res = client.get(f"/api/dashboard/export-chat-history-csv?{query}")
    assert res.status_code == 400 and res.json()["message"] == message
    export.assert_not_awaited()


def test_api_reports_server_errors(client):
    with patch(EXPORT, AsyncMock(side_effect=RuntimeError("db down"))):
        res = client.get("/api/dashboard/export-chat-history-csv?phone=9876543210")
    assert res.status_code == 500 and res.json()["message"] == "Failed to export chat history"


def test_existing_classification_csv_download_is_unchanged(client):
    with patch("app.api.dashboard.DashboardAggregationService.export_user_classification_csv", return_value="a,b\n"):
        res = client.get("/api/dashboard/export-user-classification-csv?date_preset=7d&filter_type=buyer")
    assert res.status_code == 200
    assert res.headers["content-disposition"] == "attachment; filename=user_classification_buyer_7d.csv"
