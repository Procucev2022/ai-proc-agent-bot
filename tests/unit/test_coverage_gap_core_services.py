"""Deterministic residual coverage tests for core services and data/report flows.

Every external boundary in this module is replaced with a fake or mock.  The
cases intentionally exercise low-frequency constructor, empty, fallback, and
error branches that are not useful to cover with live integrations.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.auto_categorization_service as auto_mod
import app.services.conversation_analytics_service as analytics_mod
import app.services.daily_aggregation_service as aggregation_mod
import app.services.daily_summary_service as summary_mod
import app.services.enhanced_excel_report_service as report_mod
import app.services.excel_validation_service as validation_mod
import app.services.exit_service as exit_mod
import app.services.learning_categorization_service as learning_mod
import app.services.message_queue_service as queue_mod
import app.services.openai_service as openai_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.seller_categorization_service as seller_cat_mod
import app.services.seller_service as seller_mod
import app.services.user_cache_service as cache_mod
import app.services.webhook_health_monitor_service as health_mod
import app.services.whatsapp_service as whatsapp_mod
from app.models import RFQStatus, SessionState, UserType
from app.services.whatsapp_service import MessageResponse


class Query:
    """Small SQLAlchemy-query-shaped fake used by synchronous service code."""

    def __init__(self, *, all_values=None, first=None, scalar=0, count=None, delete=0):
        self.all_value = list(all_values or [])
        self.first_value = first
        self.scalar_value = scalar
        self.count_value = scalar if count is None else count
        self.delete_value = delete

    def filter(self, *_args, **_kwargs):
        return self

    def with_entities(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def distinct(self, *_args, **_kwargs):
        return self

    def between(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.first_value

    def all(self):
        return self.all_value

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.count_value

    def delete(self):
        return self.delete_value


class ModelDB:
    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default or Query()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.refresh = MagicMock()
        self.flush = MagicMock()
        self.bind = "unit-bind"

    def query(self, model=None):
        value = self.routes.get(model, self.default)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(model)
        return value

    def add(self, value):
        self.added.append(value)


class SequenceDB(ModelDB):
    def __init__(self, queries):
        super().__init__()
        self.queries = list(queries)

    def query(self, _model=None):
        return self.queries.pop(0)


class DBContext:
    def __init__(self, db, error=None):
        self.db = db
        self.error = error

    def __enter__(self):
        if self.error:
            raise self.error
        return self.db

    def __exit__(self, *_args):
        return False


class AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeWriter:
    def __init__(self, workbook=None):
        self.book = workbook or SimpleNamespace(sheetnames=[])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def bare(cls, **attrs):
    service = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


# Learning categorization ---------------------------------------------------


def test_learning_lookup_frequency_and_client_category_alternatives(monkeypatch):
    service = bare(learning_mod.LearningCategorizationService, openai_service=MagicMock())
    item = SimpleNamespace(
        id="item-1", learning_category_id="cat-1", client_category_name="Other"
    )
    category = SimpleNamespace(
        id="cat-1", level_1_category="Tools", level_2_category="Hand", level_3_category="Drill",
        confidence_score="0.75", usage_frequency=2,
    )
    db = ModelDB({
        learning_mod.LearningCategoryItem: Query(first=item),
        learning_mod.LearningCategory: Query(first=category),
    })
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)

    found = service.check_existing_learning_category("steel drill")
    assert found["learning_category_id"] == "cat-1"
    assert found["confidence_score"] == 0.75
    assert db.close.called

    db.routes[learning_mod.LearningCategory] = Query(first=None)
    assert service.check_existing_learning_category("missing-category") is None
    db.routes[learning_mod.LearningCategoryItem] = Query(first=None)
    assert service.check_existing_learning_category("missing-item") is None

    db.routes[learning_mod.LearningCategoryItem] = Query(first=item)
    db.routes[learning_mod.LearningCategory] = Query(first=category)
    category.usage_frequency = 2
    assert service.update_usage_frequency("cat-1") is True
    assert category.usage_frequency == 3

    db.routes[learning_mod.LearningCategory] = Query(first=None)
    assert service.update_usage_frequency("unknown") is False
    db.routes[learning_mod.LearningCategory] = Query(first=category)
    db.commit.side_effect = RuntimeError("commit failed")
    assert service.update_usage_frequency("cat-1") is False
    db.commit.side_effect = None
    assert db.rollback.called

    assert service.update_client_category("item-1", "") is False
    assert service.update_client_category("item-1", "Other") is False
    db.routes[learning_mod.LearningCategoryItem] = Query(first=None)
    assert service.update_client_category("missing", "Hardware") is False

    existing = SimpleNamespace(id="item-1", client_category_name="Hardware")
    db.routes[learning_mod.LearningCategoryItem] = Query(first=existing)
    assert service.update_client_category("item-1", "Tools") is False
    assert existing.client_category_name == "Hardware"

    existing.client_category_name = "Other"
    assert service.update_client_category("item-1", "Tools") is True
    assert existing.client_category_name == "Tools"

    existing.client_category_name = ""
    db.commit.side_effect = RuntimeError("write failed")
    assert service.update_client_category("item-1", "Fasteners") is False
    assert db.rollback.called


def test_learning_deduplication_rules_and_fuzzy_fallback():
    service = bare(learning_mod.LearningCategorizationService, openai_service=MagicMock())
    empty_db = ModelDB(default=Query(all_values=[]))
    assert service._deduplicate_category_hierarchy(empty_db, "A", "B", "C") == {
        "level_1": "A", "level_2": "B", "level_3": "C"
    }

    rule_one = SimpleNamespace(
        level_1_category="Pumps", level_2_category="Hydraulics", level_3_category="Water", usage_frequency=2
    )
    db = ModelDB(default=Query(all_values=[rule_one]))
    assert service._deduplicate_category_hierarchy(db, "Hydraulics", "Components", "X") == {
        "level_1": "Pumps", "level_2": "Hydraulics", "level_3": "Components"
    }

    rule_two_rows = [
        SimpleNamespace(level_1_category="Tools", level_2_category="Hand", level_3_category="A", usage_frequency=3),
        SimpleNamespace(level_1_category="Tools", level_2_category="Power", level_3_category="B", usage_frequency=2),
    ]
    db = ModelDB(default=Query(all_values=rule_two_rows))
    result = service._deduplicate_category_hierarchy(db, "Hardware", "Tools", "Drills")
    assert result["level_1"] == "Tools" and result["level_2"] == "Hardware"

    rule_three = SimpleNamespace(
        level_1_category="Hardware", level_2_category="Fasteners", level_3_category="Bolts", usage_frequency=4
    )
    db = ModelDB(default=Query(all_values=[rule_three]))
    result = service._deduplicate_category_hierarchy(db, "Plumbing", "Fasteners", "Nuts")
    assert result["level_1"] == "Hardware"

    fuzzy = SimpleNamespace(
        level_1_category="Hardware", level_2_category="Valves & Fittings", level_3_category="Parts", usage_frequency=1
    )
    db = ModelDB(default=Query(all_values=[fuzzy]))
    result = service._deduplicate_category_hierarchy(db, "Hardware", "Valves", "Parts")
    assert result["level_2"] == "Valves & Fittings"

    broken = ModelDB(default=Query(all_values=[]))
    broken.default.all = lambda: (_ for _ in ()).throw(RuntimeError("query"))
    assert service._deduplicate_category_hierarchy(broken, "A", "B", "C") == {
        "level_1": "A", "level_2": "B", "level_3": "C"
    }


# Conversation analytics ---------------------------------------------------


def analytics_service():
    return bare(
        analytics_mod.ConversationAnalyticsService,
        batch_size=2,
        settings=SimpleNamespace(),
        openai_service=SimpleNamespace(
            client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())),
            _load_prompt=MagicMock(return_value="system"),
            default_model="unit-model",
            tools_dir=Path("."),
        ),
    )


def analytics_frames(target, *, full=True):
    if not full:
        return (pd.DataFrame(), pd.DataFrame(), pd.DataFrame()), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    buyer = pd.DataFrame([{
        "date": target, "phone_number": "+111", "buyer_email": "buyer@example.com", "session_id": "B1",
        "confidence_score": 0.9, "bfs_products_searched_list": ["bolt"], "successful_rfqs_ai": 1,
    }])
    seller = pd.DataFrame([{
        "date": target, "phone_number": "+222", "seller_email": "seller@example.com", "session_id": "S1",
        "confidence_score": 0.8, "rfq_response_ai": 0,
    }])
    unknown = pd.DataFrame([{"date": target, "session_id": "U1", "phone_number": "+333"}])
    interest = pd.DataFrame([{
        "response_date": target, "session_id": "S1", "phone_number": "+222", "rfq_id": "R1", "seller_id": "SELL1"
    }])
    remote_buyer = pd.DataFrame([{
        "phone": "+111", "username": "buyer@example.com", "date": target, "org_uuid": "org", "user_uuid": "u"
    }])
    remote_seller = pd.DataFrame([{
        "phone": "+222", "username": "seller@example.com", "rfq_date": target, "rfq_id": "R1"
    }])
    bids = pd.DataFrame([{
        "date": target, "phone_number": "+222", "username": "seller@example.com", "bids_accepted": 1
    }])
    categories = pd.DataFrame([{"rfq_id": "R1", "category": "Hardware"}])
    return (buyer, seller, unknown), interest, remote_buyer, remote_seller, bids, categories


@pytest.mark.asyncio
async def test_analytics_nonempty_join_paths_and_remote_alternatives(monkeypatch):
    service = analytics_service()
    target = date(2025, 1, 1)
    raw_session = SimpleNamespace(
        created_at=datetime(2025, 1, 1), external_user_id="+111", session_id="raw",
        conversation_history={"messages": [{"role": "user", "content": "hello"}]},
    )
    db = ModelDB(default=Query(all_values=[raw_session]))
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: DBContext(db))
    service._process_sessions_in_batches = AsyncMock(return_value={"date": str(target), "total_sessions": 3, "sessions": []})
    service._create_dataframes_from_sessions = MagicMock()
    service._create_seller_rfq_interest_event_df = MagicMock()
    service._create_bfs_search_dataframe = MagicMock(return_value=pd.DataFrame([{"session_id": "B1"}]))
    service._dump_bfs_search_df_to_db = MagicMock()
    service._dump_joined_seller_interest_to_fact_table = MagicMock()
    service._dump_joined_buyer_df_to_db = MagicMock()
    service._dump_joined_seller_df_to_db = MagicMock()
    service._dump_unknown_df_to_db = MagicMock()
    service.calculate_and_store_daily_aggregates = MagicMock()
    service.calculate_and_store_category_aggregates = MagicMock()
    service.calculate_and_store_seller_daily_aggregates = MagicMock()
    service.calculate_and_store_unknown_daily_aggregates = MagicMock()

    frames, interest, remote_buyer, remote_seller, bids, categories = analytics_frames(target)
    service._create_dataframes_from_sessions.return_value = frames
    service._create_seller_rfq_interest_event_df.return_value = interest
    service._query_remote_users = MagicMock(return_value=remote_buyer)
    service._query_remote_seller_rfqs = MagicMock(return_value=remote_seller)
    service._query_remote_counter_seller_bids = MagicMock(return_value=bids)
    service._query_remote_rfq_categories = MagicMock(return_value=categories)

    result = await service.analyze_daily_conversations(target)
    assert result["success"] is True
    assert result["sessions_df"].shape[0] == 1
    assert result["bfs_search_df"].shape[0] == 1
    service._dump_joined_buyer_df_to_db.assert_called_once()
    service._dump_joined_seller_df_to_db.assert_called_once()
    service._dump_unknown_df_to_db.assert_called_once()
    service._dump_joined_seller_interest_to_fact_table.assert_called_once()
    service._dump_bfs_search_df_to_db.assert_called_once()

    # Buyer/seller local-only branches.
    service._create_dataframes_from_sessions.return_value = (frames[0], frames[1], pd.DataFrame())
    service._create_seller_rfq_interest_event_df.return_value = pd.DataFrame()
    service._query_remote_users.return_value = pd.DataFrame()
    service._query_remote_seller_rfqs.return_value = pd.DataFrame()
    service._query_remote_counter_seller_bids.return_value = pd.DataFrame()
    service._query_remote_rfq_categories.return_value = pd.DataFrame()
    result = await service.analyze_daily_conversations(target)
    assert result["success"] is True
    assert service.calculate_and_store_daily_aggregates.call_count >= 2

    # Remote-only buyer/seller branches.
    service._create_dataframes_from_sessions.return_value = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    service._query_remote_users.return_value = remote_buyer
    service._query_remote_seller_rfqs.return_value = remote_seller
    result = await service.analyze_daily_conversations(target)
    assert result["success"] is True
    assert service._dump_joined_buyer_df_to_db.call_count >= 2
    assert service._dump_joined_seller_df_to_db.call_count >= 2


# Daily aggregation and summary --------------------------------------------


def _session(**overrides):
    values = dict(
        rfq_ids=["R1", None], rfq_id=None, product_items=[{"category": "Tools"}],
        seller_responses=["response"], created_at=datetime(2025, 1, 1),
        completed_at=datetime(2025, 1, 1, 1), last_activity_at=None,
        session_state=SessionState.completed, user_type=UserType.buyer,
        parent_session_id=None, extracted_entities={"category": "Hardware", "description": "Bolt"},
        external_user_id="user",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_daily_services_constructor_defaults_and_shape_fallbacks(monkeypatch):
    settings = SimpleNamespace(enable_daily_summarization=True)
    monkeypatch.setattr(summary_mod, "get_settings", lambda: settings)
    summary_service = summary_mod.DailySummaryService()
    assert summary_service.settings is settings

    summary_db = SequenceDB([
        Query(all_values=[
            _session(),
            _session(
                rfq_ids=[], rfq_id="R2", product_items=[], seller_responses={"x": 1},
                session_state=None, user_type=UserType.seller, parent_session_id="parent",
                completed_at=None, last_activity_at=datetime(2025, 1, 1, 0, 30),
                extracted_entities=[{"category": "Pumps"}, "malformed"],
            ),
        ]),
        Query(all_values=[SimpleNamespace(extracted_entities=[{"description": "Valve"}, "bad"])]),
        Query(first=None),
    ])
    monkeypatch.setattr(summary_mod, "get_db_session", lambda: DBContext(summary_db))
    created = asyncio.run(summary_service.generate_daily_summary("user"))
    assert created.sessions_count == 2
    assert created.rfqs_created_count == 2
    assert created.session_continuation_count == 1
    assert set(created.primary_product_categories) >= {"Bolt", "Hardware", "Pumps", "Valve"}
    assert summary_db.added and summary_db.commit.called

    # Default-date lookup with empty optional fields exercises None/default output branches.
    summary = SimpleNamespace(
        date=date(2025, 1, 2), user_type=None, sessions_count=0, rfqs_created_count=0,
        session_states_breakdown=None, seller_interaction_count=0, avg_session_duration=None,
        primary_product_categories=None, product_items_count=0, unique_categories_count=0,
        session_continuation_count=0, total_rfq_value_estimate=0,
    )
    lookup_db = SequenceDB([Query(first=summary)])
    monkeypatch.setattr(summary_mod, "get_db_session", lambda: DBContext(lookup_db))
    value = asyncio.run(summary_service.get_user_daily_summary("user"))
    assert value["user_type"] is None and value["session_states_breakdown"] == {}
    assert value["primary_product_categories"] == []
    assert value["total_rfq_value_estimate"] is None

    aggregation_settings = SimpleNamespace()
    monkeypatch.setattr(aggregation_mod, "get_settings", lambda: aggregation_settings)
    aggregation_service = aggregation_mod.DailyAggregationService()
    assert aggregation_service.settings is aggregation_settings
    empty_db = ModelDB(default=Query(all_values=[]))
    monkeypatch.setattr(aggregation_mod, "get_db_session", lambda: DBContext(empty_db))
    assert aggregation_service.run_daily_aggregation() is True
    failing_db = ModelDB(default=Query(all_values=[]))
    failing_db.default.all = lambda: (_ for _ in ()).throw(RuntimeError("database"))
    monkeypatch.setattr(aggregation_mod, "get_db_session", lambda: DBContext(failing_db))
    assert aggregation_service.run_daily_aggregation(date(2025, 1, 1)) is False


# Excel validation and reporting -------------------------------------------


@pytest.mark.asyncio
async def test_excel_validation_remaining_pipeline_returns(monkeypatch):
    service = validation_mod.ExcelValidationService()
    service._download_file_with_retry = AsyncMock(return_value=b"content")
    service._validate_file_integrity = MagicMock(return_value={"valid": True})
    service._validate_excel_content = MagicMock(return_value={"valid": True, "format": "xlsx"})
    service._validate_excel_readability = AsyncMock(return_value={"valid": True})
    service._validate_excel_structure_comprehensive = AsyncMock(return_value={"valid": True})
    service._validate_data_quality = AsyncMock(return_value={"valid": True})

    too_large = b"x" * (service.MAX_FILE_SIZE + 1)
    service._download_file_with_retry.return_value = too_large
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "file_too_large"
    service._download_file_with_retry.return_value = b"content"

    service._validate_excel_content.return_value = {"valid": False, "error_type": "wrong_format"}
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "wrong_format"
    service._validate_excel_content.return_value = {"valid": True, "format": "xlsx"}
    service._validate_excel_readability.return_value = {"valid": False, "error_type": "unreadable"}
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "unreadable"
    service._validate_excel_readability.return_value = {"valid": True}
    service._validate_excel_structure_comprehensive.return_value = {"valid": False, "error_type": "structure"}
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "structure"
    service._validate_excel_structure_comprehensive.return_value = {"valid": True}
    service._validate_data_quality.return_value = {"valid": False, "error_type": "quality"}
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "quality"


class BadWorksheet:
    columns = []

    def __getitem__(self, _key):
        raise RuntimeError("formatting")


class BadWorkbook:
    sheetnames = ["Sheet1"]

    def __getitem__(self, _key):
        return BadWorksheet()


def test_report_generation_email_switch_and_formatting_failure(monkeypatch, tmp_path):
    service = bare(report_mod.EnhancedExcelReportService, settings=SimpleNamespace())
    writer = FakeWriter()
    for name in (
        "_generate_buyer_details_sheet", "_generate_seller_details_sheet",
        "_generate_category_details_sheet", "_generate_buyer_summary_sheet",
        "_generate_seller_summary_sheet", "_generate_category_summary_sheet",
        "_generate_aggregate_sheet_90d", "_format_excel_sheets",
    ):
        setattr(service, name, MagicMock())
    monkeypatch.setattr(report_mod.pd, "ExcelWriter", lambda *_args, **_kwargs: writer)
    sent = []
    service._send_report_email = AsyncMock()

    def run_and_close(coro):
        sent.append(coro)
        coro.close()

    monkeypatch.setattr(report_mod.asyncio, "run", run_and_close)
    output = tmp_path / "report.xlsx"
    assert service.generate_report(date(2025, 1, 1), str(output), send_email=True) == str(output)
    assert sent and service._send_report_email.call_count == 1
    assert all(method.called for name, method in vars(service).items() if name.startswith("_generate_"))

    bad_writer = FakeWriter(BadWorkbook())
    service._format_excel_sheets = report_mod.EnhancedExcelReportService._format_excel_sheets.__get__(service)
    service._format_excel_sheets(bad_writer)  # formatting failures are logged, not raised


# Queueing ------------------------------------------------------------------


def queue_service():
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    redis = MagicMock()
    redis.lock.return_value = AsyncLock()
    redis.exists = AsyncMock(return_value=False)
    redis.zrange = AsyncMock(return_value=[])
    redis.zrem = AsyncMock()
    redis.delete = AsyncMock()
    redis.rpush = AsyncMock()
    redis.lpop = AsyncMock(return_value=None)
    redis.lpush = AsyncMock()
    redis.setex = AsyncMock()
    service.redis = redis
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    return service


@pytest.mark.asyncio
async def test_queue_batch_lock_empty_dedup_and_redis_error_paths(monkeypatch):
    service = queue_service()
    service._try_start_processing = AsyncMock()
    service.redis.exists.return_value = True
    await service._create_batch("1")
    service.redis.zrange.assert_not_awaited()

    service.redis.exists.return_value = False
    service.redis.zrange.return_value = []
    await service._create_batch("1")
    service._try_start_processing.assert_not_awaited()

    valid = lambda content: json.dumps(queue_mod.Message("m", "1", content, "text", 1, {}).to_dict())
    service.redis.zrange.return_value = [valid("Hello"), "not-json", valid("hello"), valid("  ")]
    await service._create_batch("1")
    payload = json.loads(service.redis.rpush.await_args.args[1])
    assert payload["concatenated_content"] == "Hello\nhello\n"
    assert payload["message_count"] == 3
    service._try_start_processing.assert_awaited_once_with("1")

    service = queue_service()
    service._try_start_processing = AsyncMock()
    service.redis.exists.side_effect = RuntimeError("redis unavailable")
    await service._create_batch("1")
    service._try_start_processing.assert_not_awaited()

    service = queue_service()
    service._try_start_processing = AsyncMock()
    service.redis.zrange.return_value = [valid("hello")]
    service.redis.rpush.side_effect = RuntimeError("write")
    await service._create_batch("1")
    service._try_start_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_processing_claim_early_returns_and_task_creation(monkeypatch):
    service = queue_service()
    service.redis.exists.return_value = True
    await service._try_start_processing("1")
    service.redis.lpop.assert_not_awaited()

    service.redis.exists.return_value = False
    await service._try_start_processing("1")
    service.redis.lpop.assert_awaited_once()

    batch = queue_mod.Batch("batch", "1", "body", "text", 1, 1)
    service.redis.lpop.return_value = json.dumps(batch.to_dict())
    created = []
    monkeypatch.setattr(queue_mod.asyncio, "create_task", lambda coro: created.append(coro))
    service._process_batch = AsyncMock()
    await service._try_start_processing("1")
    assert service.redis.setex.await_count == 2
    assert len(created) == 1
    created[0].close()


# RFQ background/intimation and seller categorization ----------------------


@pytest.mark.asyncio
async def test_rfq_background_priority_fetch_pending_and_constructor_paths():
    service = object.__new__(background_mod.RFQBackgroundService)
    now = datetime.utcnow()
    urgent = SimpleNamespace(
        rfq_id="urgent", api_payload={"rfq_title": "RFQ", "deadline": (date.today() + timedelta(days=3)).isoformat(), "categories": ["Medical Equipment"]},
        created_at=now - timedelta(days=24), status=RFQStatus.ready, external_user_id="u",
    )
    medium = SimpleNamespace(
        rfq_id="medium", api_payload={"deadline": (date.today() + timedelta(days=10)).isoformat(), "categories": []},
        created_at=now, status=RFQStatus.ready, external_user_id="m",
    )
    old = SimpleNamespace(
        rfq_id="old", api_payload={"categories": ["Other"]}, created_at=now - timedelta(days=1),
        status=RFQStatus.ready, external_user_id="o",
    )
    invalid = SimpleNamespace(
        rfq_id="bad", api_payload={"deadline": "not-a-date", "categories": ["Emergency Supplies"]},
        created_at=now, status=RFQStatus.ready, external_user_id="b",
    )
    assert service._calculate_rfq_priority(urgent) >= 180
    assert service._calculate_rfq_priority(medium) == 125
    assert service._calculate_rfq_priority(old) == 101
    assert service._calculate_rfq_priority(invalid) == 130

    db = SequenceDB([Query(first=urgent), Query(all_values=[urgent, medium]), Query(scalar=1), Query(scalar=0)])
    service.db_session = db
    found = await service._fetch_rfq_data("urgent")
    assert found["rfq_title"] == "RFQ" and found["status"] == RFQStatus.ready.value
    pending = await service.get_pending_rfqs()
    assert [row["rfq_id"] for row in pending] == ["medium"]

    broken = object.__new__(background_mod.RFQBackgroundService)
    broken.db_session = ModelDB(default=RuntimeError("query"))
    assert await broken._fetch_rfq_data("bad") is None


@pytest.mark.asyncio
async def test_rfq_intimation_persistence_helpers_and_timeout_task(monkeypatch):
    seller = SimpleNamespace(seller_id="seller", seller_name="S", phone_number="1", email="s@example.com")
    query = Query(first=seller)
    db = ModelDB(default=query)
    service = bare(intimation_mod.RFQIntimationService, db_session=db, conversation_timeout_minutes=0)
    found = await service._get_seller_details("seller")
    assert found is seller
    await service._record_notification("seller", "rfq", "message")
    await service._record_interaction("seller", "rfq", intimation_mod.InteractionType.notification_sent, {"source": "unit"})
    assert len(db.added) == 2 and db.commit.call_count == 2

    notification = SimpleNamespace()
    db.routes[intimation_mod.RFQSellerNotification] = Query(first=notification)
    await service._update_notification_response("seller", "rfq", intimation_mod.ResponseType.rfq_request)
    assert notification.response_type == intimation_mod.ResponseType.rfq_request

    created = []
    monkeypatch.setattr(intimation_mod.asyncio, "create_task", lambda coro: created.append(coro))
    service.handle_conversation_timeout = AsyncMock()
    await service._start_conversation_timeout("seller", "rfq")
    assert created
    created[0].close()


@pytest.mark.asyncio
async def test_seller_categorization_similarity_job_and_mapping_error_paths():
    item = SimpleNamespace(
        item_description="bolt", learning_category=SimpleNamespace(
            level_1_category="Hardware", level_2_category="Fasteners", level_3_category="Bolts"
        )
    )
    db = ModelDB(default=Query(all_values=[item]))
    service = bare(seller_cat_mod.SellerCategorizationService, db_session=db)
    assert (await service._get_similar_category_items("bolt"))[0]["category"] == "Hardware > Fasteners > Bolts"
    db.default = Query(all_values=[])
    assert await service._get_similar_category_items("missing") == []
    db.default.all = lambda: (_ for _ in ()).throw(RuntimeError("query"))
    assert await service._get_similar_category_items("bad") == []

    job = SimpleNamespace()
    db.routes[seller_cat_mod.SellerCategorizationJob] = Query(first=job)
    await service._update_categorization_job(
        "j", seller_cat_mod.JobStatus.completed, output_mappings=[{"x": 1}], processing_time_ms=12, error_message="warning"
    )
    assert job.output_mappings == [{"x": 1}] and job.processing_time_ms == 12
    db.routes[seller_cat_mod.SellerCategorizationJob] = Query(first=None)
    await service._update_categorization_job("missing", seller_cat_mod.JobStatus.failed)

    mapping = {
        "original_category": "Tools", "level_1_category": "Hardware", "level_2_category": "Fasteners",
        "level_3_category": "Bolts", "confidence_score": 0.8, "ai_reasoning": "unit",
    }
    category = SimpleNamespace(id="cat")
    db.routes[seller_cat_mod.SellerLearningMapping] = Query(delete=1)
    db.routes[learning_mod.LearningCategory] = Query(first=category)
    await service._store_seller_mappings("seller", [mapping])
    assert len(db.added) == 1 and db.commit.called

    db.commit.side_effect = RuntimeError("mapping commit")
    with pytest.raises(RuntimeError):
        await service._store_seller_mappings("seller", [mapping])
    assert db.rollback.called


# Seller workflow, exit, cache, and WhatsApp --------------------------------


@pytest.mark.asyncio
async def test_seller_dispatch_and_selection_alternatives():
    rfq_status = SimpleNamespace(handle_rfq_status_inquiry=AsyncMock(return_value={"route": "status"}))
    service = bare(seller_mod.SellerService, rfq_status_service=rfq_status)
    user = SimpleNamespace(org_id="org")
    session = SimpleNamespace(workflow_state={"seller_workflow_state": "awaiting_rfq_selection"}, workflow_type=None)
    service._handle_rfq_selection_response = AsyncMock(return_value={"route": "select"})
    assert await service.handle_seller_workflow(user, session, "1") == {"route": "select"}
    session.workflow_type = SimpleNamespace(value="seller_rfq_view")
    assert await service.handle_seller_workflow(user, session, "status", {"intent": "rfq_status_check"}) == {"route": "status"}

    service = bare(seller_mod.SellerService)
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 0})
    service._fetch_seller_rfqs = AsyncMock(return_value={"success": True, "rfqs": []})
    service._handle_no_credits_response = AsyncMock(return_value={"route": "credits"})
    assert await service._handle_rfq_selection_response(user, session, "1") == {"route": "credits"}

    service._check_seller_credits.return_value = {"credits_available": 2}
    service._fetch_seller_rfqs.return_value = {"success": False}
    service._handle_rfq_fetch_error = AsyncMock(return_value={"route": "fetch-error"})
    assert await service._handle_rfq_selection_response(user, session, "1") == {"route": "fetch-error"}

    service._fetch_seller_rfqs.return_value = {"success": True, "rfqs": [{"rfq_id": "R1"}]}
    service._extract_rfq_ids_from_message = AsyncMock(return_value=[])
    service._handle_general_seller_response = AsyncMock(return_value={"route": "general"})
    assert await service._handle_rfq_selection_response(user, session, "question") == {"route": "general"}

    service._extract_rfq_ids_from_message.return_value = ["bad"]
    service._handle_invalid_rfq_selection = AsyncMock(return_value={"route": "invalid"})
    assert await service._handle_rfq_selection_response(user, session, "bad") == {"route": "invalid"}

    service._extract_rfq_ids_from_message.return_value = ["R1"]
    service._process_rfq_email_requests = AsyncMock(return_value={"route": "processed"})
    assert await service._handle_rfq_selection_response(user, session, "R1") == {"route": "processed"}

    service._extract_rfq_ids_from_message.side_effect = RuntimeError("parse")
    service._handle_workflow_error = AsyncMock(return_value={"route": "error"})
    assert await service._handle_rfq_selection_response(user, session, "bad") == {"route": "error"}


@pytest.mark.asyncio
async def test_exit_confirmation_and_cleanup_failure_paths(monkeypatch):
    wa = SimpleNamespace(
        send_message=AsyncMock(return_value=MessageResponse(True)),
        send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)),
    )
    auth = SimpleNamespace(clear_user_token=AsyncMock(return_value=True))
    manager = SimpleNamespace(save_session=AsyncMock())
    service = bare(exit_mod.ExitService, whatsapp_service=wa, authentication_service=auth, session_manager=manager,
                   settings=SimpleNamespace(procucev_link="https://example.test"), db_manager=ModelDB())
    session = SimpleNamespace(workflow_state={}, conversation_history={"messages": [{"role": "user", "content": "x"}]},
                              workflow_type=None, session_id="sid", external_user_id="+1", conversation_history_extra=None,
                              extracted_entities={}, retention_date=None)
    pending = await service.handle_exit_intent("+1", session)
    assert pending["status"] == "exit_confirmation_pending"
    assert session.workflow_state["exit_pending"] is True
    assert await service.handle_exit_intent("+1", session, message={"type": "button_reply", "button_reply": {"id": "confirm_yes", "title": "yes"}})

    declined_session = SimpleNamespace(workflow_state={"exit_pending": True, "last_bot_message_before_exit": "old"},
                                       conversation_history={}, workflow_type=None)
    monkeypatch.setattr(exit_mod, "restore_last_bot_message", AsyncMock())
    assert (await service.handle_exit_confirmation("+1", declined_session, False))["status"] == "exit_aborted"
    service._clear_session_data = AsyncMock(return_value=False)
    wa.send_message.side_effect = RuntimeError("goodbye")
    result = await service.handle_exit_confirmation("+1", SimpleNamespace(workflow_state={}, conversation_history={}), True)
    assert result["session_cleared"] is False and result["goodbye_sent"] is False
    assert service._get_last_bot_message(SimpleNamespace(conversation_history=object())) is None


@pytest.mark.asyncio
async def test_cache_whatsapp_pending_flag_and_message_switches(monkeypatch):
    redis = AsyncMock()
    cache = bare(cache_mod.UserCacheService, redis_service=redis)
    redis.exists.return_value = False
    assert await cache.is_data_cached("+1") is False
    redis.exists.side_effect = RuntimeError("redis")
    assert await cache.is_data_cached("+1") is False
    redis.exists.side_effect = None
    redis.get.return_value = None
    redis.set.return_value = False
    assert await cache.store_meaningful_message("1", "hello", {"intent": "buy"}) is False
    redis.get.side_effect = RuntimeError("read")
    assert await cache.store_meaningful_message("1", "hello", {}) is False

    service = bare(whatsapp_mod.WhatsAppService, mock_mode=True, retry_service=None)
    service.retry_service = SimpleNamespace(
        retry_with_backoff=AsyncMock(return_value={"success": True, "attempts": 1, "result": MessageResponse(True, "m")})
    )
    service._clear_pending_reply_flag = AsyncMock()
    assert (await service.send_message("+1", "ack", clear_pending_reply=False)).success
    service._clear_pending_reply_flag.assert_not_awaited()
    service.retry_service.retry_with_backoff.return_value = {"success": False, "attempts": 2, "error": "down"}
    assert not (await service.send_message("+1", "fails")).success

    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: redis)
    redis.delete.return_value = 0
    await service._clear_pending_reply_flag("+1")
    redis.delete.side_effect = RuntimeError("delete")
    await service._clear_pending_reply_flag("+1")


# OpenAI and health-monitor lifecycle --------------------------------------


@pytest.mark.asyncio
async def test_openai_contextual_response_and_lazy_client_paths(tmp_path, monkeypatch):
    tool = tmp_path / "contextual_response_generation.json"
    tool.write_text(json.dumps({"type": "function", "name": "generate_contextual_response"}), encoding="utf-8")
    interaction_logger = MagicMock()
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()))
    service = bare(
        openai_mod.OpenAIService,
        tools_dir=tmp_path,
        default_model="model",
        _client=client,
        _client_closed=False,
        interaction_logger=interaction_logger,
        _build_messages_with_history=MagicMock(return_value=[]),
        _load_prompt=MagicMock(return_value="system"),
        settings=SimpleNamespace(support_email="support@example.com"),
    )
    function_call = SimpleNamespace(
        type="function_call", arguments=json.dumps({"acknowledgment": "Thanks", "progress_update": "Working", "next_question": "What item?"})
    )
    client.responses.create.return_value = SimpleNamespace(output=[function_call])
    response = await service.generate_contextual_response({"user_message": "bolt", "extracted_entities": {"x": 1}}, ["What item?"])
    assert response == "Thanks\n\nWorking\n\nWhat item?"
    interaction_logger.log_response_generation.assert_called_once()

    client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="text", arguments="{}")])
    assert await service.generate_contextual_response({}, ["Fallback question"]) == "Thank you for the information! Fallback question"
    client.responses.create.return_value = SimpleNamespace(output=[])
    assert await service.generate_contextual_response({}) == "Could you provide more details to help me assist you?"
    client.responses.create.side_effect = json.JSONDecodeError("bad", "{}", 0)
    assert await service.generate_contextual_response({}, ["Again"]) == "Thank you for the information! Again"

    class FakeClient:
        def __init__(self, **_kwargs):
            self.close = AsyncMock()

    made = []
    monkeypatch.setattr(openai_mod, "AsyncOpenAI", lambda **kwargs: made.append(FakeClient(**kwargs)) or made[-1])
    lazy = bare(openai_mod.OpenAIService, _client=None, _client_closed=False)
    first = lazy.client
    assert first is made[0]
    lazy._client_closed = True
    assert lazy.client is made[1]
    await lazy.close()
    assert lazy._client_closed is True


@pytest.mark.asyncio
async def test_webhook_health_constructor_and_standby_cleanup(monkeypatch):
    settings = SimpleNamespace(
        webhook_health_check_interval_seconds=0, webhook_api_response_threshold_seconds=1,
        webhook_api_timeout_seconds=2, webhook_failure_grace_period_seconds=0,
        webhook_recovery_confirmations=2, webhook_warning_consecutive_threshold=2,
        webhook_alert_recipients=[" a@example.com "], webhook_health_monitoring_enabled=True,
    )
    redis = MagicMock()
    email = MagicMock()
    monkeypatch.setattr(health_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(health_mod, "get_redis_service", lambda: redis)
    monkeypatch.setattr(health_mod, "EmailService", lambda: email)
    monkeypatch.setattr(health_mod.os, "getpid", lambda: 42)
    service = health_mod.WebhookHealthMonitorService()
    assert service.worker_id == "worker_42" and service.alert_recipients == ["a@example.com"]

    service._try_acquire_leader_lock = AsyncMock(return_value=False)
    service._interruptible_sleep = AsyncMock(side_effect=lambda _seconds: setattr(service, "_running", False))
    service._release_leader_lock = AsyncMock()
    service._close_session = AsyncMock()
    await service.start_monitoring()
    service._release_leader_lock.assert_awaited_once()
    assert service.is_leader is False

    service._running = False
    service.is_leader = True
    service._try_acquire_leader_lock = AsyncMock(return_value=False)
    service._interruptible_sleep = AsyncMock(side_effect=lambda _seconds: setattr(service, "_running", False))
    await service.start_monitoring()
    assert service.is_leader is False


# Auto-categorization singleton --------------------------------------------


def test_auto_categorization_factory_empty_collection_and_project_fallback(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_vector_search", True)
    fake =SimpleNamespace(collection=SimpleNamespace(count=MagicMock(return_value=0)), populate_embeddings_from_db=MagicMock())
    monkeypatch.setattr(auto_mod, "_auto_categorization_service_instance", None)
    monkeypatch.setattr(auto_mod, "AutoCategorizationService", lambda: fake)
    assert auto_mod.get_auto_categorization_service() is fake
    fake.populate_embeddings_from_db.assert_called_once()
    assert auto_mod.get_auto_categorization_service() is fake
    assert auto_mod.get_project_root().joinpath("app").exists()
