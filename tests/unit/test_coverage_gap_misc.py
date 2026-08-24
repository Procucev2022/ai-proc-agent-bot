"""Deterministic supplemental coverage for residual API, schema, task, tool, and utility paths."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
import requests
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

import app.config as config
import app.database as database
import app.main as main
import app.models as models
import app.procucev_apis.bfs_apis as bfs_api
import app.procucev_apis.category_apis as category_api
import app.procucev_apis.procucev_api_client as client_module
import app.procucev_apis.seller_apis as seller_api
import app.tasks.bfs_notification_task as bfs_task
import app.tasks.category_name_sync_task as category_task
import app.tasks.log_cleanup_task as cleanup_task
import app.tasks.seller_matching_task as seller_task
import app.tasks.taxonomy_build_task as taxonomy_task
import app.tasks.vector_store_sync_task as vector_task
import app.tools.interaction_logger as interaction_logger
import app.tools.user_selection_tool as selection_tool
import app.utils.bfs_bid_format_parser as bid_parser
import app.utils.bfs_format_parser as bfs_parser
import app.utils.datetime_utils as datetime_utils
import app.utils.logging_utils as logging_utils
import app.utils.pincode_distance as pincode_distance
import app.utils.pincode_lookup as pincode_lookup
import app.utils.sectioned_rfq_format_parser as sectioned_parser
from app.schemas.rfq import (
    ExcelValidationSchema,
    RFQCreateRequestSchema,
    RFQItemSchema,
    RFQValidationSchema,
)
from app.schemas.user import (
    BuyerRegistrationSchema,
    SellerRegistrationSchema,
    User,
    UserRole,
    normalize_phone_number,
)


class Response:
    def __init__(self, status=200, data=None, text="body", content_type="application/json"):
        self.status = status
        self._data = {} if data is None else data
        self._text = text
        self.headers = {"Content-Type": content_type}

    async def text(self):
        return self._text

    async def json(self):
        return self._data


class ResponseContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    async def __aexit__(self, *_args):
        return False


class SessionFake:
    closed = False

    def __init__(self, responses=(), post_responses=None):
        self.responses = list(responses)
        self.post_responses = list(responses if post_responses is None else post_responses)
        self.calls = []
        self.post_calls = []
        self.close = AsyncMock()

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return ResponseContext(self.responses.pop(0))

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return ResponseContext(self.post_responses.pop(0))


class QueryFake:
    def __init__(self, values=(), first=None, delete_count=0):
        self.values = list(values)
        self.first_value = first
        self.delete_count = delete_count

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def filter(self, *_args, **_kwargs):
        return self

    def filter_by(self, **_kwargs):
        return self

    def delete(self, **_kwargs):
        return self.delete_count


class DBFake:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.executed = []
        self.query_value = QueryFake()

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return SimpleNamespace(
            keys=lambda: ["value"],
            fetchall=lambda: self.rows,
            rowcount=self.rowcount,
        )

    def query(self, *_args, **_kwargs):
        return self.query_value

    def add(self, _value):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# Configuration, model, and schema edges ------------------------------------


def test_configuration_accessors_ssl_and_webhook_parsing(monkeypatch):
    settings = config.Settings()
    assert settings._parse_webhook_recipients("") == []
    assert settings._parse_webhook_recipients(" a@example.com, , b@example.com ") == [
        "a@example.com",
        "b@example.com",
    ]

    settings.database_mode = "client"
    settings.client_database_url = "mysql+pymysql://client"
    assert settings.get_database_url().startswith("mysql+")
    assert settings.is_ssl_enabled() is True
    settings.client_database_url = "postgresql://wrong"
    with pytest.raises(ValueError, match="PostgreSQL"):
        settings.get_database_url()
    settings.client_database_url = None
    with pytest.raises(ValueError, match="CLIENT_DATABASE_URL"):
        settings.get_database_url()

    settings.database_mode = "local"
    settings.local_database_url = "mysql+pymysql://local"
    assert settings.get_database_url().endswith("local")
    settings.local_database_url = None
    with pytest.raises(ValueError, match="LOCAL_DATABASE_URL"):
        settings.get_database_url()

    settings.enable_remote_categorization = False
    assert settings.get_remote_database_url() is None
    settings.enable_remote_categorization = True
    settings.remote_database_url = None
    with pytest.raises(ValueError, match="REMOTE_DATABASE_URL"):
        settings.get_remote_database_url()
    settings.remote_database_url = "mysql+pymysql://remote"
    assert settings.get_remote_database_url().endswith("remote")

    settings.openai_api_key = None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        settings.get_openai_config()
    settings.openai_api_key = "key"
    assert settings.get_openai_config()["default_model"] == settings.openai_model_default
    assert settings.get_whatsapp_config()["verify_token"] == settings.whatsapp_verify_token
    assert settings.get_logging_config()["handlers"] == ["console"]
    assert settings.get_rfq_status_config()["max_allowed"] == settings.rfq_max_allowed
    assert settings.get_api_timeout_config()["total_timeout"] == settings.api_total_timeout
    assert settings.get_retry_config()["max_attempts"] == settings.retry_max_attempts
    assert settings.get_celery_config()["broker_url"] == settings.celery_broker_url

    settings.webhook_health_monitoring_enabled = False
    settings.local_database_url = "mysql+pymysql://local"
    settings.validate_config()
    monkeypatch.setattr(config, "_settings", settings)
    assert config.get_settings() is settings


def test_model_validators_cover_none_valid_and_fallback_values():
    session = models.ConversationSession(
        session_id="s",
        external_user_id="u",
        workflow_state={},
        conversation_history=[],
        retention_date=datetime.now().date(),
        workflow_type="rfq_creation",
        outcome="completed",
        user_type="seller",
        session_state="active",
    )
    assert session.workflow_type is models.WorkflowType.rfq_creation
    assert session.outcome is models.ConversationOutcome.completed
    assert session.user_type is models.UserType.seller
    assert session.session_state is models.SessionState.active
    assert models.ConversationSession.validate_workflow_type(None, "x", None) is None
    assert models.ConversationSession.validate_outcome(None, "x", None) is None
    assert models.ConversationSession.validate_user_type(None, "x", "bad") is models.UserType.unknown
    assert models.ConversationSession.validate_session_state(None, "x", "bad") is models.SessionState.active


def test_rfq_and_user_schema_edge_values():
    with pytest.raises(ValidationError, match="valid number"):
        RFQItemSchema(description="bolt", quantity="not-number", unit_of_measures="pcs")
    with pytest.raises(ValidationError, match="greater than 0"):
        RFQItemSchema(description="bolt", quantity=-1, unit_of_measures="pcs")
    assert RFQItemSchema(description="bolt", quantity="2.5", unit_of_measures="unknown").quantity == 2.5

    future = datetime.now() + timedelta(days=2)
    base = {
        "createdBy": "user",
        "deliveryDate": future,
        "user": "user",
        "org": {"id": "org"},
        "rfqItem": [{"description": "bolt", "quantity": 1, "unit_of_measures": "pcs"}],
        "clientdeliverylocationrfq": [{"state": "Unknown", "city": "Pune", "pincode": "411005"}],
    }
    assert RFQCreateRequestSchema(**base).no_pr_flag is True
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "deliveryDate": datetime.now() - timedelta(days=1)})

    empty = RFQValidationSchema()
    assert empty.get_missing_mandatory_fields() == ["delivery_date", "items", "delivery_locations"]
    assert empty.get_optional_questions()
    assert empty.get_combined_questions(include_optional=False)["optional"] == []
    dynamic = RFQValidationSchema(
        delivery_date=future,
        items=[{"description": "valid", "quantity": 1}, {"description": "", "quantity": 0}],
        delivery_locations=[{"state": "", "city": "", "pincode": ""}, {"state": "M", "city": "P", "pincode": "1"}],
    )
    questions = dynamic.get_mandatory_questions()
    assert any("item description" in question.lower() for question in questions)
    assert any("delivery" in question.lower() or "address" in question.lower() for question in questions)

    assert normalize_phone_number("(987) 654-3210") == "+919876543210"
    assert normalize_phone_number("+441234567890") == "+441234567890"
    assert normalize_phone_number("123", default_country_code="1") == "+123"
    buyer = BuyerRegistrationSchema(
        name=" Ada ", companyName=" Acme ", email=" A@EXAMPLE.COM ", zipCode="411005"
    )
    assert buyer.name == "Ada" and buyer.email == "a@example.com"
    seller = SellerRegistrationSchema(
        name="Ada", companyName="Acme", email="a@example.com", address1="Pune",
        zipCode="411005", gstin="27abcde1234f1z5", details="  tools  "
    )
    assert seller.gstin == "27ABCDE1234F1Z5" and seller.details == "tools"
    assert User.from_api_response({"userId": 7, "selfClient": False}).role is UserRole.SELLER
    assert User.from_api_response({"id": "x"}).role is UserRole.UNKNOWN
    with pytest.raises(ValueError, match="User ID"):
        User.from_api_response({"id": "", "userId": ""})
    assert User.from_mixed_data({"name": "Ada", "email": "a@example.com", "id": "u"}).is_registered


# API clients and service wrappers ------------------------------------------


@pytest.mark.asyncio
async def test_api_service_exception_and_default_response_branches(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(bfs_api, "get_procucev_api_client", lambda: client)
    bfs = bfs_api.BFSAPIService()
    client.post.side_effect = RuntimeError("accept-offline")
    assert (await bfs.accept_bid_by_seller("bid"))["error"] == "accept-offline"
    client.post.side_effect = None
    client.post.return_value = {"success": False}
    assert (await bfs.request_bfs_item("i", 1, 2, 1, "o", "u", "p"))["error"] == "BFS item request failed"

    monkeypatch.setattr(category_api, "get_procucev_api_client", lambda: client)
    category = category_api.CategoryAPIService()
    client.get.return_value = {"success": True}
    assert (await category.get_divisions())["divisions"] is None
    client.get.return_value = {"success": False}
    assert (await category.get_categories())["error"] == "Failed to get categories"

    monkeypatch.setattr(seller_api, "get_procucev_api_client", lambda: client)
    monkeypatch.setattr(seller_api, "get_settings", lambda: SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal"))
    seller = seller_api.SellerAPIService()
    client.post.return_value = {
        "success": True,
        "data": {"rfqs": [{"rfqId": "r", "deliveryDate": "bad-date", "rfqItem": [{}, {"category": "Tools"}]}]},
    }
    result = await seller.fetch_active_rfqs("org")
    assert result["rfqs"][0]["delivery_date"] == "bad-date"
    client.post.return_value = {"success": False}
    assert (await seller.check_seller_rfq_status("seller", [None]))["error"] == "Failed to get RFQ status for seller"
    client.post.return_value = {"success": True, "paymentUrl": "root"}
    payment = await seller.generate_payment_link("plan")
    assert payment["payment_url"] == "root"
    client.post.return_value = {"success": False}
    assert (await seller.generate_payment_link("plan"))["payment_url"] == "https://portal"
    client.post.side_effect = RuntimeError("seller-offline")
    assert (await seller.generate_payment_link("plan"))["error"] == "seller-offline"


@pytest.mark.asyncio
async def test_procucev_client_cache_auth_and_http_fallbacks(monkeypatch):
    settings = SimpleNamespace(
        gmt_base_url="https://gmt.example",
        gmt_username="user",
        gmt_password="password",
        gmt_phone="9876543210",
        gmt_client_id="client",
        gmt_client_secret="secret",
        gmt_retry_delay=0,
        gmt_max_retries=3,
    )
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    monkeypatch.setattr(client_module, "manual_log_api_call", Mock())
    client = client_module.ProcucevAPIClient()

    client.send_request = AsyncMock(return_value={"message": "missing token"})
    assert await client.authenticate() is False
    redis.get.side_effect = RuntimeError("redis")
    client.authenticate = AsyncMock(return_value=False)
    assert await client._ensure_authenticated() is False
    client.send_request = client_module.ProcucevAPIClient.send_request.__get__(client)

    client.session = SessionFake([Response(status=200, data=[1])])
    assert (await client.send_request("GET", "/items"))["data"] == [1]
    client.session = SessionFake([Response(status=401, data={})])
    client.max_retries = 1
    client._ensure_authenticated = AsyncMock(return_value=True)
    result = await client.send_request("GET", "/secure", require_auth=True, dynamic_token="fixed")
    assert result["status_code"] == 401
    plain = Response(status=400, text="plain", data={}, content_type="text/plain")
    plain.json = AsyncMock(side_effect=ValueError("not json"))
    client.session = SessionFake([plain])
    assert (await client.send_request("GET", "/bad"))["message"] == "plain"

    handler = AsyncMock(side_effect=RuntimeError("notify"))
    monkeypatch.setattr("app.services.global_error_handler.handle_api_error", handler)
    await client._handle_500_error("/bad", "server")
    await client._handle_timeout_error("/bad", "timeout")
    assert handler.await_count == 2
    await client.close_session()
    client_module._global_client = None
    fake = MagicMock()
    fake.create_session = AsyncMock()
    fake.close_session = AsyncMock()
    monkeypatch.setattr(client_module, "ProcucevAPIClient", lambda: fake)
    assert await client_module.init_procucev_api_client() is fake
    await client_module.close_procucev_api_client()


# Database and application edge paths --------------------------------------


def test_database_context_and_pool_fallbacks(monkeypatch):
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "remote_engine", None)
    manager = database.DatabaseManager(session=MagicMock())
    assert manager.get_connection_pool_status() == {
        "main_db": {"status": "not_initialized"},
        "remote_db": {"status": "not_initialized"},
    }
    assert manager.execute_health_check() is None
    assert manager.backup_learning_data() is None
    manager.close()

    class Session:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closed = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed += 1

    successful = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: successful)
    monkeypatch.setattr(database, "_log_pool_status", Mock())
    with database.get_db_session_context() as current:
        assert current is successful
    assert (successful.commits, successful.rollbacks, successful.closed) == (1, 0, 1)

    failed = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: failed)
    with pytest.raises(RuntimeError):
        with database.get_db_session_context():
            raise RuntimeError("rollback")
    assert (failed.commits, failed.rollbacks, failed.closed) == (0, 1, 1)

    owned_session = MagicMock()
    owned_session.close.side_effect = RuntimeError("close")
    monkeypatch.setattr(database, "get_db_session", lambda: owned_session)
    owned = database.DatabaseManager()
    assert owned.session is owned_session  # force the lazy checkout
    owned.close()
    assert owned.has_session is False


@pytest.mark.asyncio
async def test_main_global_handler_phone_and_unrestricted_ip(monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(main, "handle_server_error", notify)
    request = SimpleNamespace(
        json=AsyncMock(return_value={"phone": "9199"}),
        method="POST",
        url=SimpleNamespace(path="/chat"),
    )
    response = await main.global_exception_handler(request, ValueError("bad"))
    assert response.status_code == 500
    assert notify.await_args.kwargs["user_phone"] == "9199"

    middleware = main.IPRestrictionMiddleware(object(), [])
    request.client = SimpleNamespace(host="127.0.0.1")
    allowed = await middleware.dispatch(request, lambda _request: _response())
    assert allowed.status_code == 200


async def _response():
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True})


# Task branches --------------------------------------------------------------


@pytest.mark.asyncio
async def test_bfs_notification_inactive_split_and_unsent_results(monkeypatch):
    records = [
        {"bfs_user_uuid": "active", "seller_phone": "9199", "listed_price": 4},
        {"bfs_user_uuid": "inactive", "seller_phone": "9188", "buy_price": 7},
    ]
    monkeypatch.setattr(bfs_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_task, "get_pending_bfs_notifications", lambda limit: records)
    monkeypatch.setattr(bfs_task, "get_users_active_in_last_24hrs", lambda phones: {"9199"})
    monkeypatch.setattr(bfs_task, "normalize_phone_for_comparison", lambda phone: phone.lstrip("+"))
    service = SimpleNamespace(
        send_bfs_bid_notifications_batch=AsyncMock(
            return_value={"results": [], "sent": 0, "failed": 0, "skipped": 2}
        )
    )
    monkeypatch.setattr(bfs_task, "get_seller_notification_service", lambda: service)
    marked = Mock()
    monkeypatch.setattr(bfs_task, "mark_notifications_sent_batch", marked)
    result = await bfs_task.process_bfs_notifications()
    assert result["interactive_messages"] == 1
    assert result["template_messages"] == 1
    assert result["skipped"] == 2
    marked.assert_not_called()
    assert service.send_bfs_bid_notifications_batch.await_args.kwargs["notifications"][1]["bid_data"]["buy_price"] == 7


def test_category_sync_path_and_outer_failure(monkeypatch):
    original_path = list(category_task.sys.path)
    project_path = category_task.os.path.abspath(category_task.os.path.join(category_task.os.path.dirname(__file__), "../.."))
    category_task.sys.path[:] = [p for p in original_path if p != project_path]
    category_task._add_project_path()
    assert project_path in category_task.sys.path
    category_task._add_project_path()

    monkeypatch.setattr(category_task, "_add_project_path", Mock(side_effect=RuntimeError("path")))
    result = category_task.sync_category_names.run()
    assert result["status"] == "failed" and result["error"] == "path"
    category_task.sys.path[:] = original_path


def test_cleanup_current_style_and_existing_archive(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 1, 10)

    monkeypatch.setattr(cleanup_task, "datetime", FixedDateTime)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "app.log.2024-01-01"
    log_file.write_text("old", encoding="utf-8")
    manager = cleanup_task.LogCleanupManager(str(log_dir), 7, 30)
    manager.archive_dir.mkdir()
    existing_archive = manager.archive_dir / "logs_2024-01-01.tar.gz"
    existing_archive.write_bytes(b"existing")
    stats = manager.run()
    assert stats["logs_archived"] == 0
    assert log_file.exists()
    assert manager._extract_date_from_filename("bad_2024-20-40.log") is None


@pytest.mark.asyncio
async def test_seller_matching_enhanced_candidates_and_notification_flow(monkeypatch):
    monkeypatch.setattr(seller_task, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller_task, "get_rfq_item_categories", lambda _: ["Tools"])
    monkeypatch.setattr(seller_task, "get_users_active_in_last_24hrs", lambda phones: {"111"})
    monkeypatch.setattr(seller_task, "normalize_phone_for_comparison", lambda phone: phone.lstrip("+"))
    enhanced = SimpleNamespace(
        find_sellers_for_item=AsyncMock(return_value={"success": True, "sellers": [{"seller_id": "s1"}]})
    )
    monkeypatch.setattr(
        "app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService",
        lambda: enhanced,
    )
    seller = {"seller_id": "s1", "phone_number": "+111", "seller_name": "S", "categories": ["Tools"]}
    standard = SimpleNamespace(
        select_sellers_for_rfq=AsyncMock(
            return_value={"subscribed_sellers": [seller], "unsubscribed_sellers": []}
        )
    )
    monkeypatch.setattr(seller_task, "log_selected_sellers_to_remote", Mock(return_value=True))
    notification = SimpleNamespace(
        send_rfq_notifications=AsyncMock(
            return_value={"sent": 1, "failed": 0, "results": [{"seller_id": "s1", "success": True}]}
        )
    )
    monkeypatch.setattr(seller_task, "SellerNotificationService", lambda: notification)
    monkeypatch.setattr(seller_task, "record_rfq_seller_notifications", Mock())
    result = await seller_task.process_single_rfq_matching(
        {"rfq_id": "r", "rfq_uuid": "u", "description": "tools"}, standard, set()
    )
    assert result["sellers_matched"] == 1
    assert result["interactive_messages"] == 1
    assert standard.select_sellers_for_rfq.await_args.kwargs["candidate_seller_ids"] == ["s1"]


async def _taxonomy_loader(monkeypatch, process):
    module = SimpleNamespace(process_category_mappings=process)
    spec = SimpleNamespace(
        loader=SimpleNamespace(exec_module=lambda target: target.__dict__.update(module.__dict__))
    )
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_args: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _spec: SimpleNamespace())


@pytest.mark.asyncio
async def test_taxonomy_parallel_failed_result_and_single_batch_failure(monkeypatch):
    monkeypatch.setattr(
        taxonomy_task,
        "get_settings",
        lambda: SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1),
    )
    monkeypatch.setattr(taxonomy_task, "get_remote_item_categories", lambda: [{"category": "A"}])
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock(), delete=AsyncMock())
    monkeypatch.setattr(taxonomy_task.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy_task.asyncio, "sleep", AsyncMock())
    process = AsyncMock(return_value={"success": False, "error": "rejected"})
    await _taxonomy_loader(monkeypatch, process)
    result = await taxonomy_task.build_taxonomy_async(
        None, batch_size=1, process_all=True, parallel=True, resume=False
    )
    assert result["success"] is False
    assert result["total_errors"] == 3
    assert result["batches_processed"] == 0
    assert process.await_count == 3

    process.return_value = {"success": False, "error": "single failure"}
    result = await taxonomy_task.build_taxonomy_async(
        None, batch_size=1, process_all=False, parallel=False, resume=False
    )
    assert result["success"] is False and "single failure" in result["error"]


@pytest.mark.asyncio
async def test_vector_sync_embedding_exception_and_empty_learning_taxonomy(monkeypatch):
    settings = SimpleNamespace(enable_vector_store_sync=True)
    monkeypatch.setattr(vector_task, "get_settings", lambda: settings)
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", lambda: {"success": True})
    monkeypatch.setattr(vector_task, "_run_vector_embedding_creation", Mock(side_effect=RuntimeError("chroma")))
    result = vector_task.sync_vector_store.run()
    assert result["status"] == "partial_failure"
    assert result["steps_failed"][0]["step"] == "vector_embedding_creation"

    database_module = importlib.import_module("app.database")
    models_module = importlib.import_module("app.models")
    db = DBFake()
    db.query_value = QueryFake([])
    monkeypatch.setattr(database_module, "get_db_session", lambda: db)
    monkeypatch.setattr(importlib.import_module("app.services.openai_service"), "OpenAIService", lambda: object())
    adapter = SimpleNamespace(test_connection=lambda: True, get_sellers_from_remote=lambda: [{"seller_id": "s", "categories": ["A"]}])
    monkeypatch.setattr(importlib.import_module("app.services.seller_data_adapter"), "SellerDataAdapter", lambda: adapter)
    result = await vector_task._async_map_sellers_to_categories()
    assert result["success"] is False and result["error"] == "No learning categories found"
    assert db.closed
    del models_module


# Tools and utility fallbacks ------------------------------------------------


def test_interaction_logger_optional_openai_input_and_serializer_fallback(tmp_path, monkeypatch):
    logger = interaction_logger.InteractionLogger(str(tmp_path))
    logger.log_intent_classification("hello", "greeting", 0.8, "reason", "model")
    assert "openai_input" not in json.loads(logger.log_file.read_text(encoding="utf-8").splitlines()[0])

    class Broken:
        def model_dump(self):
            raise RuntimeError("dump")

        def dict(self):
            raise RuntimeError("dict")

    assert logger._json_serializer(Broken()).startswith("<")
    monkeypatch.setattr("builtins.open", Mock(side_effect=OSError("disk")))
    logger._write_log_entry({"interaction_type": "failure"})


@pytest.mark.asyncio
async def test_user_selection_low_confidence_equal_result_and_error(monkeypatch):
    tool = selection_tool.UserSelectionTool(SimpleNamespace())
    options = [{"number": 1, "action": "Register new", "display": "Register"}]
    tool._rule_based_analysis = Mock(return_value={"confidence": 0.2, "selected_option": 1})
    tool._ai_based_analysis = AsyncMock(return_value={"confidence": 0.2, "selected_option": 2})
    assert (await tool.analyze_user_selection({"other": "value"}, options))["selected_option"] == 1
    tool._rule_based_analysis = Mock(side_effect=RuntimeError("rule"))
    result = await tool.analyze_user_selection("unclear", options)
    assert result["confidence"] == 0.0 and result["requires_clarification"] is True
    assert tool._fuzzy_email_match("x", "") ["confidence"] == 0.0


def test_datetime_logging_and_pincode_lookup_remaining_branches(monkeypatch):
    assert datetime_utils.format_utc_time_only(None) == "N/A"
    assert datetime_utils.format_utc_short(None) == "N/A"
    assert datetime_utils.format_date_display(None) == "N/A"

    logging_utils.clear_user_phone_context()
    monkeypatch.setattr(
        logging_utils,
        "_user_phone_context",
        SimpleNamespace(get=Mock(side_effect=RuntimeError("context"))),
    )
    logging_utils._thread_local.phone_number = "thread-phone"
    assert logging_utils.get_user_phone_context() == "thread-phone"
    record = logging.LogRecord("unit", logging.INFO, __file__, 1, "hello", (), None)
    assert "thread-phone" in logging_utils.CustomFormatter().format(record)
    delattr(logging_utils._thread_local, "phone_number")

    monkeypatch.setattr(
        pincode_lookup.requests,
        "get",
        Mock(side_effect=requests.exceptions.ConnectionError("offline")),
    )
    monkeypatch.setattr(pincode_lookup.time, "sleep", Mock())
    assert pincode_lookup.get_pincode_details("411005", max_retries=2) is None
    monkeypatch.setattr(
        pincode_lookup,
        "get_pincode_details",
        lambda _pin: [{"Status": "Success", "PostOffice": [{}]}],
    )
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None


def test_pincode_distance_and_parser_malformed_inputs(monkeypatch):
    pincode_distance.clear_pincode_cache()
    assert pincode_distance.get_coordinates_from_pincode("") is None
    assert pincode_distance.get_coordinates_from_pincode("123") is None
    monkeypatch.setattr(pincode_distance, "get_coordinates_from_pincode", lambda _pin: (1.0, 2.0))
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(side_effect=RuntimeError("geo")))
    assert pincode_distance.calculate_distance_from_pincode_to_coords("411005", 3, 4) is None

    item = {"description": "Laptop", "specification": "i7", "availableQuantity": 2, "sellPrice": 10}
    assert "Invalid header" in bid_parser.parse_bid_format("not a bid", [item])["error"]
    generated = bid_parser.generate_bid_format([item])
    valid = generated.replace("a. Your Price:", "a. Your Price: 9").replace("b. Your Qty:", "b. Your Qty: 1")
    assert bid_parser.parse_bid_format(valid, [item])["bids"][0]["price"] == 9
    assert bid_parser.generate_bid_format([{}]).startswith("1.")
    assert bfs_parser.generate_bfs_display_with_missing([{}])[1] == ["Product name"]
    assert bfs_parser.is_bfs_complete([{"description": ""}]) is False

    assert sectioned_parser.parse_delivery_format("intro only")["error"]
    assert sectioned_parser.parse_items_format("Item 1: Bolt\nQty: 2\nPlease confirm")["items"][0]["quantity"] == 2
    assert sectioned_parser._sanitize_text("a\x00b") == "ab"
    assert sectioned_parser._truncate_text("abcdef", 4) == "a..."
    assert sectioned_parser.generate_items_display_with_missing([{"description": "x", "quantity": 1}], [])[1] == []
