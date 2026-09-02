"""Branch-level coverage for the dashboard aggregation service and its API guard.

These tests target the defensive paths that the happy-path dashboard tests never
reach: per-section DB failures inside ``_compute_all_metrics``, falsy rows in the
row-unpacking loops, the Redis merge logic in ``get_today_conversations`` and
``get_conversation_messages``, the interactive-message extractors, and the
operator-key dependency on the dashboard router.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models import ConversationOutcome, RFQStatus, WorkflowType
from app.services.dashboard_aggregation_service import DashboardAggregationService


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _query_mock(db: MagicMock) -> MagicMock:
    """Wire a MagicMock db so every query chain terminates on the same mock."""
    query = MagicMock()
    db.query.return_value = query
    query.filter.return_value = query
    query.group_by.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    return query


def _service(db: MagicMock) -> DashboardAggregationService:
    service = DashboardAggregationService(db_session=db)
    service.realtime_service = SimpleNamespace(
        get_active_users_count=AsyncMock(return_value={"total": 0}),
        get_recent_feed=AsyncMock(return_value=[]),
    )
    return service


def _redis_client(members=None, session_blobs=None) -> MagicMock:
    """Build an async Redis mock exposing zrange / scan_iter / get."""
    client = MagicMock()
    client.zrange = AsyncMock(return_value=list(members or []))

    async def _scan_iter(match=None):  # noqa: ARG001 - signature mirrors redis-py
        for key in (session_blobs or {}):
            yield key

    client.scan_iter = _scan_iter
    client.get = AsyncMock(side_effect=lambda key: (session_blobs or {}).get(key))
    return client


# --------------------------------------------------------------------------- #
# _compute_all_metrics: optional sections failing independently
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_compute_all_metrics_survives_every_optional_section_failure():
    """Seller, hourly, category, fallback, buyer and seller leaderboards all fail."""
    db = MagicMock()
    query = _query_mock(db)
    query.scalar.return_value = 0
    query.count.return_value = 0
    boom = RuntimeError("section down")
    query.all.side_effect = [
        [("919000000001",)],  # users_in_period
        [],                   # returning_buyers role_user_ids
        [],                   # returning_sellers role_user_ids
        [("919000000001",)],  # all_today_users
        [("919000000001",)],  # buyer_users_set
        [("919000000002",)],  # seller_users_set
        [],                   # rfq_phones_in_period
        [],                   # session rfq id rows
        [],                   # sessions_by_phone
        boom,                 # registered_sellers_map -> except branch
        [],                   # session_rfq_rows
        [],                   # db_rfqs
        [],                   # comp_session_rfq_rows
        [],                   # comp_db_rfqs
        boom,                 # sessions_in_window -> hourly except branch
        boom,                 # cat_rows -> category except branch
        boom,                 # ProductCategory fallback -> except branch
        boom,                 # top buyers -> except branch
        boom,                 # top sellers -> except branch
    ]

    stats = await _service(db).get_dashboard_stats(date_preset="30d")

    assert stats["status"] == "success"
    assert stats["whatsapp"]["hourly_traffic"] == [0] * 24
    assert stats["rfq_lifecycle"]["category_breakdown"] == []
    assert stats["buyer_seller"]["top_buyers"] == []
    assert stats["buyer_seller"]["top_sellers"] == []
    # Seller lookup failed, so the seller is classified as not registered.
    assert stats["user_classification"]["seller"]["not_registered"] == 1


@pytest.mark.asyncio
async def test_compute_all_metrics_skips_falsy_rows_in_every_loop():
    """Rows whose significant column is falsy must be skipped, not counted."""
    db = MagicMock()
    query = _query_mock(db)
    query.scalar.return_value = 0
    query.count.return_value = 0
    query.all.side_effect = [
        [("919000000001",), (None,)],  # users_in_period (falsy filtered out)
        [],                             # returning_buyers role_user_ids
        [],                             # returning_sellers role_user_ids
        [("919000000001",), (None,)],  # all_today_users
        [],                            # buyer_users_set
        [("919000000002",)],           # seller_users_set
        [(None,)],                     # rfq_phones_in_period, falsy row
        [([], None, None)],            # session rfq rows: no rfq and no user
        [],                            # sessions_by_phone
        [
            SimpleNamespace(phone_number=None, subscription_credits=None),
            SimpleNamespace(phone_number="+919000000002", subscription_credits=0),
        ],                             # registered_sellers_map, falsy phone skipped
        [
            ([None], None, None, None),          # rfq_ids list of falsy ids
            (None, None, None, WorkflowType.rfq_creation),  # workflow-only RFQ
        ],                             # session_rfq_rows
        [(None, RFQStatus.submitted)],  # db_rfqs, falsy id skipped
        [([None], None), (None, "RFQ-COMP")],  # comp_session_rfq_rows
        [(None,)],                     # comp_db_rfqs, falsy id skipped
        [(None,), (datetime(2026, 8, 27, 9, 0),)],  # sessions_in_window
        [(None, 5, 1), ("Steel", 0, 0)],  # cat_rows: falsy name then zero notified
        [(None, None, None)],          # top buyers row with no phone
        [
            SimpleNamespace(
                seller_id="s-1",
                seller_name="Anon",
                phone_number=None,
                categories=None,
                ranking=None,
                subscription_credits=None,
            )
        ],                             # top sellers row with missing fields
    ]

    stats = await _service(db).get_dashboard_stats(date_preset="90d")

    assert stats["whatsapp"]["hourly_traffic"][9] == 1
    # "Steel" has zero notifications, so its response rate degrades to 0.0.
    assert stats["rfq_lifecycle"]["category_breakdown"] == [
        {"category": "Steel", "rfqs_notified": 0, "responses": 0, "response_rate": 0.0}
    ]
    assert stats["buyer_seller"]["top_buyers"][0]["phone_masked"] == "Unknown"
    assert stats["buyer_seller"]["top_sellers"][0]["ranking"] == "Gold"
    assert stats["buyer_seller"]["top_sellers"][0]["credits"] == 0
    assert stats["user_classification"]["seller"]["registered_sws"] == 1
    # The workflow-only session still counts one active RFQ.
    assert stats["rfq_lifecycle"]["active_rfqs"] == 1


@pytest.mark.asyncio
async def test_compute_all_metrics_uses_session_context_when_no_db():
    """With no injected session the service opens its own DB context."""
    db = MagicMock()
    query = _query_mock(db)
    query.scalar.return_value = 0
    query.count.return_value = 0
    query.all.return_value = []

    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = None

    service = _service(None)
    with patch(
        "app.services.dashboard_aggregation_service.get_db_session_context",
        return_value=ctx,
    ):
        stats = await service.get_dashboard_stats(date_preset="today")

    assert stats["status"] == "success"


# --------------------------------------------------------------------------- #
# export_user_classification_csv / get_user_classification_details_json
# --------------------------------------------------------------------------- #

def _classification_all_side_effect(seller_rows):
    """Shared `.all()` sequence for the CSV and details-JSON generators."""
    return [
        [("919000000001",), ("919000000002",), ("919000000003",), (None,)],  # all users
        [("919000000002",)],  # buyers
        [("919000000003",)],  # sellers
        [(None,)],            # rfq phones, falsy row skipped
        # single bulk session fetch grouped by phone; no created_at, so
        # last_active stays "N/A" and the null row is skipped
        [
            ("919000000002", WorkflowType.registration, {"registration_stage": "completed"},
             None, None, None, None, "sess-buyer-1"),
            ("919000000003", WorkflowType.general_inquiry, {}, None, None, None, None, "sess-seller-1"),
            (None, None, None, None, None, None, None, None),
        ],
        seller_rows,          # Seller.phone_number / subscription_credits
    ]


def test_export_csv_handles_null_rows_and_seller_lookup_failure():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None  # unknown user has no aggregate row
    query.all.side_effect = _classification_all_side_effect(
        [
            SimpleNamespace(phone_number=None, subscription_credits=None),
            SimpleNamespace(phone_number="+919000000003", subscription_credits=0),
        ]
    )

    csv_out = DashboardAggregationService(db_session=db).export_user_classification_csv(
        date_preset="today", filter_type="all"
    )

    assert "Phone Number" in csv_out
    # Unknown user rendered with the "no aggregate row" defaults.
    assert "919000000001,Unknown,Not Registered" in csv_out
    assert ",0,N/A" in csv_out
    assert "Seller without Subscription (0 credits)" in csv_out


def test_export_csv_unknown_filter_type_keeps_every_row():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    query.all.side_effect = _classification_all_side_effect([])

    csv_out = DashboardAggregationService(db_session=db).export_user_classification_csv(
        date_preset="today", filter_type="not-a-known-filter"
    )

    assert csv_out.count("\r\n") == 4  # header + 3 users


def test_export_csv_seller_query_failure_is_swallowed():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    side_effect = _classification_all_side_effect(RuntimeError("seller table gone"))
    query.all.side_effect = side_effect

    csv_out = DashboardAggregationService(db_session=db).export_user_classification_csv(
        date_preset="today", filter_type="seller_not_registered"
    )

    assert "Seller Intent Expressed" in csv_out


def test_export_csv_uses_session_context_when_no_db():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    query.all.return_value = []

    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = None

    with patch(
        "app.services.dashboard_aggregation_service.get_db_session_context",
        return_value=ctx,
    ):
        csv_out = DashboardAggregationService().export_user_classification_csv()

    assert csv_out.startswith("Phone Number")


def test_details_json_handles_null_rows_and_seller_lookup_failure():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    query.all.side_effect = _classification_all_side_effect(
        RuntimeError("seller table gone")
    )

    result = DashboardAggregationService(db_session=db).get_user_classification_details_json(
        date_preset="today", filter_type="all"
    )

    assert result["status"] == "success"
    assert result["total_overall_users"] == 3
    unknown_row = next(r for r in result["users"] if r["category"] == "Unknown")
    assert unknown_row["sessions_count"] == 0
    assert unknown_row["last_active"] == "N/A"
    assert unknown_row["session_id"] is None
    seller_row = next(r for r in result["users"] if r["category"] == "Seller")
    assert seller_row["key"] == "seller_not_registered"


def test_details_json_unknown_filter_type_keeps_every_row():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    query.all.side_effect = _classification_all_side_effect(
        [
            SimpleNamespace(phone_number=None, subscription_credits=3),
            SimpleNamespace(phone_number="919000000003", subscription_credits=7),
        ]
    )

    result = DashboardAggregationService(db_session=db).get_user_classification_details_json(
        date_preset="today", filter_type="not-a-known-filter"
    )

    assert result["total_users"] == 3
    seller_row = next(r for r in result["users"] if r["category"] == "Seller")
    assert seller_row["key"] == "seller_subscribed"


def test_details_json_uses_session_context_when_no_db():
    db = MagicMock()
    query = _query_mock(db)
    query.first.return_value = None
    query.all.return_value = []

    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = None

    with patch(
        "app.services.dashboard_aggregation_service.get_db_session_context",
        return_value=ctx,
    ):
        result = DashboardAggregationService().get_user_classification_details_json()

    assert result["users"] == []


# --------------------------------------------------------------------------- #
# get_daily_visitors row shapes
# --------------------------------------------------------------------------- #

def test_get_daily_visitors_skips_rows_missing_day_or_user():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [
        (None, "919000000001"),
        ("2026-08-27", None),
        "not-a-row",
    ]

    result = DashboardAggregationService(db_session=db).get_daily_visitors(
        date_preset="custom", start_date_str="2026-08-27", end_date_str="2026-08-27"
    )

    assert result == [{"date": "2026-08-27", "visitors": 0}]


# --------------------------------------------------------------------------- #
# get_today_conversations
# --------------------------------------------------------------------------- #

def _db_session_row(**overrides):
    row = dict(
        session_id="db-1",
        external_user_id="919000000001",
        user_type=SimpleNamespace(value="buyer"),
        outcome=SimpleNamespace(value="completed"),
        started_at=datetime(2026, 8, 27, 9, 0),
        created_at=datetime(2026, 8, 27, 9, 0),
        last_activity_at=datetime(2026, 8, 27, 9, 30),
        rfq_id=None,
        rfq_ids=["RFQ-1"],
        conversation_history={"messages": [{"role": "user", "content": "hello"}]},
    )
    row.update(overrides)
    return SimpleNamespace(**row)


@pytest.mark.asyncio
async def test_today_conversations_merges_redis_presence_and_sessions():
    """Redis presence, a duplicate session and a Redis-only session all merge."""
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [
        _db_session_row(),
        # Same normalized phone (+91 prefix) so the second branch merges it.
        _db_session_row(
            session_id="db-2",
            external_user_id="+919000000001",
            user_type=SimpleNamespace(value="seller"),
            conversation_history={"messages": [{"role": "assistant", "content": {"body": "hi"}}]},
            rfq_id="RFQ-2",
        ),
        # Blank phone number is skipped entirely.
        _db_session_row(session_id="db-3", external_user_id="   "),
        # Non-dict history degrades to an empty message list, so it is dropped.
        _db_session_row(
            session_id="db-4",
            external_user_id="919000000009",
            conversation_history="not-a-dict",
            user_type="unknown",
            outcome=None,
        ),
    ]

    session_blobs = {
        "session:db-2": json.dumps(
            {
                "session_id": "db-2",
                "external_user_id": "919000000001",
                "user_type": "seller",
                "outcome": "active",
                "last_activity_at": "2026-08-27T10:00:00",
                "started_at": "2026-08-27T09:00:00",
                "rfq_id": "RFQ-3",
                "conversation_history": {"messages": [{"role": "user", "content": "x" * 120}]},
            }
        ),
        "session:new-1": json.dumps(
            {
                "session_id": "new-1",
                "external_user_id": "919000000002",
                "user_type": "unknown",
                "conversation_history": {"messages": [{"role": "user", "content": {"header": "H"}}]},
            }
        ),
        "session:blank": json.dumps({"session_id": "blank", "external_user_id": ""}),
        "session:broken": "not-json",
    }
    members = [
        b'{"phone": "+919000000001"}',
        json.dumps({"phone": ""}),
        "not-json-member",
    ]

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client(members, session_blobs)),
    ):
        result = await service.get_today_conversations()

    by_phone = {entry["normalized_phone"]: entry for entry in result}
    assert set(by_phone) == {"9000000001", "9000000002"}

    merged = by_phone["9000000001"]
    assert merged["session_count"] == 2
    assert merged["user_type"] == "seller"
    assert merged["rfq_id"] == "RFQ-3"
    assert merged["is_online"] is True
    assert merged["last_activity_at"] == "2026-08-27T10:00:00"
    assert merged["latest_message_preview"].endswith("...")
    assert len(merged["latest_message_preview"]) == 80

    redis_only = by_phone["9000000002"]
    assert redis_only["session_count"] == 1
    assert redis_only["is_online"] is True
    assert redis_only["latest_message_preview"] == "H"


@pytest.mark.asyncio
async def test_today_conversations_redis_only_users_and_new_session_branch():
    """A Redis session for a known user that MySQL has not persisted yet."""
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [_db_session_row()]

    session_blobs = {
        "session:unsaved": json.dumps(
            {
                "session_id": "unsaved",
                "external_user_id": "919000000001",
                "user_type": "unknown",
                "conversation_history": {"messages": [{"role": "user", "content": "later"}]},
            }
        )
    }

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client([], session_blobs)),
    ):
        result = await service.get_today_conversations()

    assert len(result) == 1
    entry = result[0]
    assert entry["session_count"] == 2  # DB session plus the unsaved Redis one
    assert entry["user_type"] == "buyer"  # "unknown" from Redis must not override
    assert entry["latest_message_preview"] == "later"


@pytest.mark.asyncio
async def test_today_conversations_merge_keeps_existing_values_for_blank_fields():
    """A merged session with blank metadata must not overwrite the first one."""
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [
        _db_session_row(),
        _db_session_row(
            session_id="db-blank",
            last_activity_at=None,
            user_type=None,
            outcome=None,
            rfq_id=None,
            rfq_ids=[],
            conversation_history={"messages": [{"role": "user", "content": "second"}]},
        ),
    ]

    session_blobs = {
        # Already persisted in MySQL and carrying no new messages.
        "session:db-1": json.dumps(
            {
                "session_id": "db-1",
                "external_user_id": "919000000001",
                "conversation_history": {"messages": []},
            }
        ),
        # scan_iter yields the key but GET returns nothing.
        "session:empty": None,
    }

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client([], session_blobs)),
    ):
        result = await service.get_today_conversations()

    assert len(result) == 1
    entry = result[0]
    assert entry["session_count"] == 2
    assert entry["user_type"] == "buyer"
    assert entry["outcome"] == "completed"
    assert entry["rfq_id"] == "RFQ-1"
    assert entry["last_activity_at"] == "2026-08-27T09:30:00"
    # Redis holds the authoritative snapshot for "db-1" (replacing, not appending,
    # to its already-counted MySQL messages); "db-blank" keeps its own DB messages.
    assert entry["total_messages"] == 1


@pytest.mark.asyncio
async def test_today_conversations_handles_redis_and_db_failures():
    db = MagicMock()
    db.query.side_effect = RuntimeError("db offline")

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(side_effect=RuntimeError("redis offline")),
    ):
        assert await service.get_today_conversations() == []


@pytest.mark.asyncio
async def test_today_conversations_without_redis_client_uses_db_context():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [_db_session_row()]

    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = None

    service = DashboardAggregationService(db_session=None)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=None),
    ), patch(
        "app.services.dashboard_aggregation_service.get_db_session_context",
        return_value=ctx,
    ):
        result = await service.get_today_conversations()

    assert result[0]["normalized_phone"] == "9000000001"
    assert result[0]["is_online"] is False


# --------------------------------------------------------------------------- #
# get_conversation_messages
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_conversation_messages_requires_phone_or_session():
    service = DashboardAggregationService(db_session=MagicMock())
    result = await service.get_conversation_messages()
    assert result["status"] == "error"
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_conversation_messages_returns_empty_when_nothing_found():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = []

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client()),
    ):
        result = await service.get_conversation_messages(session_id="missing")

    assert result == {"status": "success", "messages": [], "session_info": None}


@pytest.mark.asyncio
async def test_conversation_messages_merges_redis_and_formats_content():
    """Redis overrides a stale DB session and every content shape is formatted."""
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [
        _db_session_row(
            rfq_id=None,
            rfq_ids=["RFQ-IDS"],
            conversation_history={"messages": [{"role": "user", "content": "stale"}]},
        ),
        _db_session_row(
            session_id="db-2",
            rfq_id="RFQ-DIRECT",
            user_type="unknown",
            outcome=None,
            started_at=None,
            last_activity_at=None,
            conversation_history="not-a-dict",
        ),
    ]

    interactive = {
        "header": {"text": "Header text"},
        "body": {"body": "Body text"},
        "footer": "Footer",
        "action": {
            "buttons": [
                {"reply": {"id": "b1", "title": "Yes"}},
                {"reply": {"id": "b2"}},
                {"id": "b3", "title": "Plain"},
                {"id": "", "title": ""},
                "not-a-dict",
            ]
        },
    }
    session_blobs = {
        "session:db-1": json.dumps(
            {
                "session_id": "db-1",
                "external_user_id": "919000000001",
                "conversation_history": {
                    "messages": [
                        {"role": "user", "content": "fresh", "timestamp": "t1"},
                        {"sender": "assistant", "content": interactive, "type": "interactive"},
                    ]
                },
            }
        ),
        "session:extra": json.dumps(
            {
                "session_id": "extra",
                "external_user_id": "919000000001",
                "user_type": "seller",
                "outcome": "abandoned",
                "last_activity_at": "2026-08-27T11:00:00",
                "started_at": "2026-08-27T08:00:00",
                "rfq_id": "RFQ-EXTRA",
                "conversation_history": {
                    "messages": [
                        {"role": "assistant", "content": {"title": "Titled"}},
                        {"role": "assistant", "content": {"message": "Nested"}},
                        {"role": "assistant", "content": {"unknown": "shape"}},
                        {"role": "assistant", "content": {"buttons": "not-a-list"}},
                        {"role": "assistant", "content": None},
                        {"role": "assistant", "content": 42},
                        # header holds a dict that only resolves via its own header key
                        {"role": "assistant", "content": {"header": {"header": "Deep"}}},
                        # body holds a non-string scalar, hitting the str() fallback
                        {"role": "assistant", "content": {"body": 7}},
                    ]
                },
            }
        ),
        "session:other-user": json.dumps(
            {"session_id": "other", "external_user_id": "919000009999"}
        ),
        "session:broken": "not-json",
    }

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client([], session_blobs)),
    ):
        result = await service.get_conversation_messages(phone="+919000000001")

    assert result["status"] == "success"
    info = result["session_info"]
    assert info["total_sessions"] == 3
    assert info["user_type"] == "seller"
    assert info["outcome"] == "abandoned"
    assert info["rfq_id"] == "RFQ-EXTRA"
    assert info["last_activity_at"] == "2026-08-27T11:00:00"

    contents = [m["content"] for m in result["messages"]]
    assert "fresh" in contents
    assert "stale" not in contents
    assert "Body text" in contents
    assert "Titled" in contents
    assert "Nested" in contents
    assert "42" in contents
    assert "Deep" in contents
    assert "7" in contents
    assert "" in contents  # unknown dict shape and None content

    interactive_msg = next(m for m in result["messages"] if m["type"] == "interactive")
    assert interactive_msg["header"] == "Header text"
    assert interactive_msg["footer"] == "Footer"
    assert interactive_msg["buttons"] == [
        {"id": "b1", "title": "Yes"},
        {"id": "b2", "title": "b2"},
        {"id": "b3", "title": "Plain"},
    ]


@pytest.mark.asyncio
async def test_conversation_messages_keeps_db_messages_when_redis_is_shorter():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [
        _db_session_row(
            conversation_history={
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "user", "content": "two"},
                ]
            }
        )
    ]
    session_blobs = {
        "session:db-1": json.dumps(
            {
                "session_id": "db-1",
                "external_user_id": "919000000001",
                "conversation_history": "not-a-dict",
            }
        )
    }

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client([], session_blobs)),
    ):
        result = await service.get_conversation_messages(phone="919000000001")

    assert [m["content"] for m in result["messages"]] == ["one", "two"]


@pytest.mark.asyncio
async def test_conversation_messages_session_id_only_and_db_failure():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [_db_session_row(session_id="only")]

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=None),
    ):
        result = await service.get_conversation_messages(session_id="only")
    assert result["session_info"]["phone"] == "919000000001"

    failing = MagicMock()
    failing.query.side_effect = RuntimeError("db offline")
    failing_service = DashboardAggregationService(db_session=failing)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(side_effect=RuntimeError("redis offline")),
    ):
        assert (await failing_service.get_conversation_messages(phone="1")) == {
            "status": "success",
            "messages": [],
            "session_info": None,
        }


@pytest.mark.asyncio
async def test_conversation_messages_uses_db_context_when_no_session():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = [_db_session_row()]

    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = None

    service = DashboardAggregationService(db_session=None)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=None),
    ), patch(
        "app.services.dashboard_aggregation_service.get_db_session_context",
        return_value=ctx,
    ):
        result = await service.get_conversation_messages(phone="919000000001")

    assert result["session_info"]["total_sessions"] == 1


@pytest.mark.asyncio
async def test_conversation_messages_redis_only_session_reports_phone():
    db = MagicMock()
    query = _query_mock(db)
    query.all.return_value = []

    session_blobs = {
        "session:live": json.dumps(
            {
                "session_id": "live",
                "external_user_id": "919000000001",
                "started_at": "2026-08-27T07:00:00",
                "conversation_history": {"messages": [{"role": "user", "content": "live"}]},
            }
        ),
        # scan_iter yields the key but GET returns nothing.
        "session:empty": None,
    }

    service = DashboardAggregationService(db_session=db)
    with patch(
        "app.redis_db.AsyncRedisConnectionManager.get_client",
        AsyncMock(return_value=_redis_client([], session_blobs)),
    ):
        result = await service.get_conversation_messages(session_id="live")

    assert result["session_info"]["phone"] == "919000000001"
    assert result["session_info"]["user_type"] == "unknown"
    assert result["session_info"]["started_at"] == "2026-08-27T07:00:00"
    assert result["messages"][0]["content"] == "live"


# --------------------------------------------------------------------------- #
# Dashboard operator key dependency
# --------------------------------------------------------------------------- #

def test_require_dashboard_operator_rejects_wrong_and_unset_keys():
    from app.api import dashboard as dashboard_api

    with patch.object(
        dashboard_api,
        "get_settings",
        return_value=SimpleNamespace(dashboard_api_key="expected-key"),
    ):
        with pytest.raises(HTTPException) as wrong:
            dashboard_api.require_dashboard_operator(x_dashboard_key="wrong-key")
        assert wrong.value.status_code == 401

        with pytest.raises(HTTPException):
            dashboard_api.require_dashboard_operator(x_dashboard_key=None)

        # Correct key passes without raising.
        assert dashboard_api.require_dashboard_operator(x_dashboard_key="expected-key") is None

    with patch.object(
        dashboard_api,
        "get_settings",
        return_value=SimpleNamespace(dashboard_api_key=""),
    ):
        # Unset key allows access without authentication.
        assert dashboard_api.require_dashboard_operator(x_dashboard_key="anything") is None
        assert dashboard_api.require_dashboard_operator(x_dashboard_key=None) is None


def test_require_dashboard_operator_accepts_session_cookie():
    """Browser clients authenticate with the HttpOnly session cookie."""
    from app.api import dashboard as dashboard_api

    token = dashboard_api.build_dashboard_session_token("expected-key")
    with patch.object(
        dashboard_api,
        "get_settings",
        return_value=SimpleNamespace(dashboard_api_key="expected-key"),
    ):
        assert dashboard_api.require_dashboard_operator(
            x_dashboard_key=None, dashboard_session=token
        ) is None

        with pytest.raises(HTTPException) as stale:
            dashboard_api.require_dashboard_operator(
                x_dashboard_key=None, dashboard_session="stale-token"
            )
        assert stale.value.status_code == 401

        # Expired token: correctly signed, but issued outside the allowed window.
        expired_issued_at = int(time.time()) - dashboard_api.DASHBOARD_SESSION_MAX_AGE - 10
        expired_token = dashboard_api.build_dashboard_session_token("expected-key", expired_issued_at)
        with pytest.raises(HTTPException) as expired:
            dashboard_api.require_dashboard_operator(
                x_dashboard_key=None, dashboard_session=expired_token
            )
        assert expired.value.status_code == 401

        # Tampered token: well-formed and unexpired, but the signature does not match.
        issued_at = int(time.time())
        tampered_token = f"{issued_at}.deadbeef"
        with pytest.raises(HTTPException) as tampered:
            dashboard_api.require_dashboard_operator(
                x_dashboard_key=None, dashboard_session=tampered_token
            )
        assert tampered.value.status_code == 401


def test_details_json_groups_bulk_session_rows_per_user():
    """Every user is classified from one bulk session fetch, not per-user queries."""
    db = MagicMock()
    query = _query_mock(db)
    created = datetime(2026, 8, 27, 9, 30, 0)
    query.all.side_effect = [
        [("919000000001",), ("919000000002",)],  # all users
        [("919000000002",)],                     # buyers
        [],                                      # sellers
        [],                                      # rfq phones
        [
            ("919000000001", WorkflowType.general_inquiry, {}, None, None, None, None, "sess-unknown-old"),
            ("919000000001", WorkflowType.general_inquiry, {}, None, None, None, created, "sess-unknown-new"),
            ("919000000002", WorkflowType.rfq_creation, {}, None, "R1", None, created, "sess-buyer"),
        ],
        [],                                      # sellers table
    ]

    result = DashboardAggregationService(db_session=db).get_user_classification_details_json(
        date_preset="today", filter_type="all"
    )

    # One bulk session query replaced the per-user aggregates.
    assert db.query.call_count == 6
    unknown_row = next(r for r in result["users"] if r["category"] == "Unknown")
    assert unknown_row["sessions_count"] == 2
    assert unknown_row["session_id"] == "sess-unknown-new"
    assert unknown_row["last_active"] == "2026-08-27 09:30:00"
    buyer_row = next(r for r in result["users"] if r["category"] == "Buyer")
    assert buyer_row["key"] == "buyer_registered_rfq_created"
    assert buyer_row["session_id"] == "sess-buyer"


def test_details_json_unknown_user_session_id_falls_back_without_timestamps():
    """Sessions without a created_at still expose a session id for the viewer."""
    db = MagicMock()
    query = _query_mock(db)
    query.all.side_effect = [
        [("919000000001",)],  # all users
        [],                   # buyers
        [],                   # sellers
        [],                   # rfq phones
        [("919000000001", WorkflowType.general_inquiry, {}, None, None, None, None, "sess-no-time")],
        [],                   # sellers table
    ]

    result = DashboardAggregationService(db_session=db).get_user_classification_details_json(
        date_preset="today", filter_type="unknown"
    )

    unknown_row = result["users"][0]
    assert unknown_row["session_id"] == "sess-no-time"
    assert unknown_row["last_active"] == "N/A"


def test_export_csv_groups_bulk_session_rows_per_user():
    """The CSV export classifies users from the same single bulk fetch."""
    db = MagicMock()
    query = _query_mock(db)
    created = datetime(2026, 8, 27, 9, 30, 0)
    query.all.side_effect = [
        [("919000000001",), ("919000000003",)],  # all users
        [],                                      # buyers
        [("919000000003",)],                     # sellers
        [],                                      # rfq phones
        [
            ("919000000001", WorkflowType.general_inquiry, {}, None, None, None, created, "sess-unknown"),
            ("919000000003", WorkflowType.registration, {"registration_stage": "completed"},
             None, None, None, created, "sess-seller"),
        ],
        [],                                      # sellers table
    ]

    csv_out = DashboardAggregationService(db_session=db).export_user_classification_csv(
        date_preset="today", filter_type="all"
    )

    assert db.query.call_count == 6
    assert "919000000001,Unknown,Not Registered,No Role Selected / Greeting Only,1,2026-08-27 09:30:00" in csv_out
    assert "919000000003,Seller,Registered,Seller without Subscription (0 credits),1,2026-08-27 09:30:00" in csv_out


def test_get_dashboard_db_closes_the_session():
    from app.api import dashboard as dashboard_api

    fake_db = MagicMock()
    with patch.object(dashboard_api, "get_db_session", return_value=fake_db):
        generator = dashboard_api.get_dashboard_db()
        assert next(generator) is fake_db
        with pytest.raises(StopIteration):
            next(generator)
    fake_db.close.assert_called_once()


def test_compute_all_metrics_delta_pct_helper_covers_zero_previous():
    """delta_pct is nested inside _compute_all_metrics; exercise it via metrics."""
    db = MagicMock()
    query = _query_mock(db)
    query.count.return_value = 0
    query.all.return_value = []
    # total_sessions/unique_users positive, comparison period zero.
    query.scalar.side_effect = [4, 0, 2, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0]

    service = _service(db)
    stats = service._compute_all_metrics(
        db,
        "today",
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc),
        "all",
        None,
        None,
        "not-a-status",
        {"total": 0},
        [],
    )

    assert stats["executive"]["visitors_delta_pct"] == 100.0
    assert stats["executive"]["conversations_delta_pct"] == 100.0
    assert stats["executive"]["rfqs_delta_pct"] == 0.0
    assert stats["whatsapp"]["buy_vs_sell_ratio"] == "1.0:1"
    assert stats["marketplace_health"]["alerts"][0]["type"] == "healthy"


def test_compute_all_metrics_rounds_out_ratio_when_no_sellers():
    db = MagicMock()
    query = _query_mock(db)
    query.count.return_value = 0
    query.all.return_value = []
    query.scalar.side_effect = [0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0]

    service = _service(db)
    stats = service._compute_all_metrics(
        db,
        "today",
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc),
        "buyer",
        "Steel",
        "Mumbai",
        RFQStatus.submitted.value,
        {"total": 0},
        [],
    )

    assert stats["whatsapp"]["buy_vs_sell_ratio"] == "3:0"
    assert stats["executive"]["buyer_to_rfq_conversion"] == 0.0
    assert stats["filters"]["location"] == "Mumbai"


def test_compute_all_metrics_counts_returning_users_and_outcomes():
    """Prior-window users make the returning count non-zero."""
    db = MagicMock()
    query = _query_mock(db)
    query.count.return_value = 1
    query.scalar.side_effect = [5, 2, 3, 1, 2, 1, 0, 1, 2, 4, 2, 1, 1]
    query.all.side_effect = [
        [("919000000001",)],
        [],
        [],
        [("919000000001",)],
        [("919000000001",)],
        [],
        [],
        [],
        [
            (
                "919000000001",
                WorkflowType.general_inquiry,
                None,
                ConversationOutcome.abandoned,
                None,
                None,
            )
        ],
        [],
        [
            (["RFQ-A", "RFQ-B"], None, ConversationOutcome.completed, None),
            (None, "RFQ-C", ConversationOutcome.abandoned, None),
        ],
        [("RFQ-A", RFQStatus.submitted), ("RFQ-D", RFQStatus.ready)],
        [(["RFQ-OLD"], None), (None, "RFQ-OLDER")],
        [("RFQ-OLD",)],
        [],
        [("Steel", 4, 0)],
        [("919000000001", 3, datetime(2026, 8, 27, 12, 0))],
        [],
    ]

    service = _service(db)
    stats = service._compute_all_metrics(
        db,
        "today",
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc),
        "all",
        None,
        None,
        None,
        {"total": 1, "buyers": 1, "sellers": 0},
        [],
    )

    assert stats["executive"]["returning_users"] == 1
    assert stats["executive"]["new_users"] == 2
    assert stats["rfq_lifecycle"]["submitted_rfqs"] == 2
    assert stats["rfq_lifecycle"]["active_rfqs"] == 2
    assert stats["buyer_seller"]["top_buyers"][0]["phone_masked"] == "9190****01"
    assert stats["buyer_seller"]["top_buyers"][0]["last_active"] == "12:00:00"
    # Steel: 4 notified, 0 responses -> low response rate opportunity alert.
    alert_types = {alert["type"] for alert in stats["marketplace_health"]["alerts"]}
    assert "opportunity" in alert_types
    assert stats["user_classification"]["buyer"]["not_registered"] == 1
