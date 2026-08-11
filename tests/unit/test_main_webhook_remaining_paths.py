from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse

import app.api.webhook as webhook
import app.main as main


class RequestStub:
    def __init__(
        self,
        *,
        content_type="",
        body=b"{}",
        form=None,
        json_data=None,
        query=None,
        client=None,
        path="/x",
        method="POST",
    ):
        self.headers = {"content-type": content_type} if content_type else {}
        self._body = body
        self._form = form or {}
        self._json = json_data
        self.query_params = query or {}
        self.client = SimpleNamespace(host="127.0.0.1") if client is None else client
        self.url = SimpleNamespace(path=path)
        self.method = method

    async def body(self):
        return self._body

    async def form(self):
        return self._form

    async def json(self):
        if isinstance(self._json, BaseException):
            raise self._json
        return self._json


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    session_id = "session-1"


def _db_context(value=None):
    @contextmanager
    def context():
        yield value if value is not None else object()

    return context


def _settings(**overrides):
    values = dict(
        app_name="test",
        environment="unit",
        database_mode="local",
        redis_url="redis://unit",
        chroma_host="localhost",
        chroma_port=8000,
        azure_openai_base_url="https://unit",
        webhook_health_monitoring_enabled=False,
        pending_reply_ttl_seconds=25,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_main_middleware_precedence_and_missing_client():
    async def next_response(_request):
        return JSONResponse({"ok": True})

    middleware = main.IPRestrictionMiddleware(object(), ["10.0.0.1"])
    request = RequestStub(client=SimpleNamespace(host="10.0.0.2"))
    request.headers["x-real-ip"] = "10.0.0.1"
    request.headers["x-forwarded-for"] = "10.0.0.2, 10.0.0.1"
    assert (await middleware.dispatch(request, next_response)).status_code == 200

    request.headers["x-real-ip"] = "10.0.0.2"
    assert (await middleware.dispatch(request, next_response)).status_code == 403

    logging_middleware = main.RequestLoggingMiddleware(object())
    no_client = RequestStub(client=SimpleNamespace(host="unused"))
    no_client.client = None
    assert (await logging_middleware.dispatch(no_client, next_response)).status_code == 200


@pytest.mark.asyncio
async def test_main_global_exception_handles_missing_or_invalid_json_and_notify_failure(monkeypatch):
    notify = AsyncMock(side_effect=RuntimeError("support unavailable"))
    monkeypatch.setattr(main, "handle_server_error", notify)

    response = await main.global_exception_handler(
        RequestStub(json_data=["not", "a", "mapping"], path="/broken"),
        ValueError("bad"),
    )
    assert response.status_code == 500
    assert json.loads(response.body)["detail"].startswith("Internal server error")
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["user_phone"] is None

    response = await main.global_exception_handler(
        RequestStub(json_data=RuntimeError("invalid json"), path="/broken"),
        RuntimeError("worse"),
    )
    assert response.status_code == 500
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_main_chat_interactive_captures_messages_and_swallows_tracking_errors(monkeypatch):
    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.whatsapp_service.send_message = AsyncMock()
    fake_chat.whatsapp_service.send_configurable_buttons = AsyncMock()
    fake_chat.cleanup = AsyncMock()
    session = Session()
    redis_session = AsyncMock()
    history = MagicMock(side_effect=RuntimeError("history unavailable"))

    async def process(user_phone, message_content, message_type):
        await fake_chat.whatsapp_service.send_message(user_phone, "reply", session=session)
        await fake_chat.whatsapp_service.send_message(user_phone, "preferred", session_id=session)
        await fake_chat.whatsapp_service.send_configurable_buttons(
            user_phone,
            "choose",
            [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
            header="Header",
            footer="Footer",
            session=session,
        )
        return {"seen_type": message_type}

    fake_chat.process_message.side_effect = process
    monkeypatch.setattr(main, "get_db_session_context", _db_context())
    monkeypatch.setattr(main, "ChatService", lambda **kwargs: fake_chat)
    monkeypatch.setattr("app.utils.logging_utils.UserPhoneContext", lambda _phone: AsyncContext())
    monkeypatch.setattr(
        "app.services.helpers.summarization_helpers.SummarizationHelpers.add_to_conversation_history",
        history,
    )
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)

    result = await main.process_chat_message.__wrapped__(
        RequestStub(),
        main.ChatMessage(message={"type": "button_reply", "id": "yes"}, phone="9199"),
    )

    assert result["success"] is True
    assert result["status"] == "processed"
    assert result["responses"] == ["reply", "preferred", "choose"]
    assert result["interactive_buttons"] == [[{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}]]
    fake_chat.process_message.assert_called_once_with("9199", {"type": "button_reply", "id": "yes"}, "interactive")
    fake_chat.cleanup.assert_awaited_once()
    assert redis_session.append_message_to_history.await_count == 0


@pytest.mark.asyncio
async def test_main_chat_cleanup_and_notification_failure_on_setup_error(monkeypatch):
    monkeypatch.setattr(main, "get_db_session_context", MagicMock(side_effect=RuntimeError("database")))
    notify = AsyncMock(side_effect=RuntimeError("notification"))
    monkeypatch.setattr(main, "handle_server_error", notify)

    result = await main.process_chat_message.__wrapped__(
        RequestStub(), main.ChatMessage(message="hello", phone="9199")
    )
    assert result["success"] is False
    assert result["error"] == "database"
    notify.assert_awaited_once()

    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.cleanup = AsyncMock()
    fake_chat.process_message = AsyncMock(side_effect=RuntimeError("processing"))
    monkeypatch.setattr(main, "get_db_session_context", _db_context())
    monkeypatch.setattr(main, "ChatService", lambda **kwargs: fake_chat)
    monkeypatch.setattr(main, "handle_server_error", AsyncMock())
    result = await main.process_chat_message.__wrapped__(
        RequestStub(), main.ChatMessage(message="hello", phone="9199")
    )
    assert result["success"] is False
    fake_chat.cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_mime"),
    [
        ("quote.xls", "application/vnd.ms-excel"),
        ("macro.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ],
)
async def test_main_upload_excel_mime_and_callback_paths(monkeypatch, filename, expected_mime):
    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.whatsapp_service.send_message = AsyncMock()
    fake_chat.whatsapp_service.send_configurable_buttons = AsyncMock()
    fake_chat.cleanup = AsyncMock()
    session = Session()
    redis_session = AsyncMock()
    seen = {}

    async def process(phone, content, message_type):
        seen["content"] = content
        seen["type"] = message_type
        from app.services.excel_validation_service import ExcelValidationService
        await ExcelValidationService().validate_excel_file_from_url("mock://uploaded-file", content["document"]["filename"])
        await fake_chat.whatsapp_service.send_message(phone, "uploaded", session_id=session)
        await fake_chat.whatsapp_service.send_configurable_buttons(
            phone, "next", [{"id": "next", "title": "Next"}], session=session
        )
        return {}

    fake_chat.process_message.side_effect = process
    monkeypatch.setattr(main, "get_db_session_context", _db_context())
    monkeypatch.setattr("app.database.get_db_session_context", _db_context())
    monkeypatch.setattr(main, "ChatService", lambda **kwargs: fake_chat)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr(
        "app.services.helpers.summarization_helpers.SummarizationHelpers.add_to_conversation_history",
        MagicMock(),
    )
    validator = AsyncMock(return_value={"valid": True})
    monkeypatch.setattr(
        "app.services.excel_validation_service.ExcelValidationService.validate_excel_file_from_url",
        validator,
    )

    class Upload:
        async def read(self):
            return b"spreadsheet"

    upload = Upload()
    upload.filename = filename
    result = await main.upload_excel_file.__wrapped__(RequestStub(), "9199", upload)

    assert result["success"] is True
    assert seen["type"] == "excel_upload"
    assert seen["content"]["document"]["mime_type"] == expected_mime
    assert seen["content"]["document"]["data"] == "c3ByZWFkc2hlZXQ="
    assert result["status"] == "processed"
    assert redis_session.append_message_to_history.await_count == 2
    fake_chat.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_upload_file_read_and_notification_failures(monkeypatch):
    class Upload:
        filename = "bad.xlsx"

        async def read(self):
            raise OSError("cannot read")

    notify = AsyncMock(side_effect=RuntimeError("support down"))
    monkeypatch.setattr(main, "handle_server_error", notify)
    result = await main.upload_excel_file.__wrapped__(RequestStub(), "9199", Upload())
    assert result["success"] is False
    assert "cannot read" in result["error"]
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_unknown_route_and_error_without_phone_or_cancel_success(monkeypatch):
    background = MagicMock()
    request = RequestStub(body=b"payload")
    monkeypatch.setattr(
        webhook, "parse_webhook_data", AsyncMock(return_value={"type": "sticker", "from": "1"})
    )
    response = await webhook.handle_webhook.__wrapped__(request, background)
    assert response.status_code == 200
    background.add_task.assert_called_once_with(webhook.process_message_async, {"type": "sticker", "from": "1"})

    background.reset_mock()
    parse = AsyncMock(side_effect=[RuntimeError("first parse"), RuntimeError("second parse")])
    monkeypatch.setattr(webhook, "parse_webhook_data", parse)
    response = await webhook.handle_webhook.__wrapped__(request, background)
    assert response.status_code == 200 and b"error_handled" in response.body
    background.add_task.assert_not_called()

    parse = AsyncMock(side_effect=[RuntimeError("first parse"), {"type": "text", "from": "1"}])
    cancel = AsyncMock(side_effect=RuntimeError("cancel failed"))
    monkeypatch.setattr(webhook, "parse_webhook_data", parse)
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)
    await webhook.handle_webhook.__wrapped__(request, background)
    cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_delivery_json_fallback_and_missing_fields(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr(webhook, "get_redis_service", lambda: redis)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis)

    request = RequestStub(json_data={"status": "sent", "mobile": "1", "msg_ref": "m", "timestamp": "now"})
    request.form = AsyncMock(side_effect=RuntimeError("no form"))
    result = await webhook.handle_delivery_callback(request)
    assert result.status_code == 200

    request = RequestStub(json_data=["not a dict"])
    request.form = AsyncMock(side_effect=RuntimeError("no form"))
    result = await webhook.handle_delivery_callback(request)
    assert result.status_code == 200

    request = RequestStub(json_data=RuntimeError("bad json"))
    request.form = AsyncMock(side_effect=RuntimeError("bad form"))
    result = await webhook.handle_delivery_callback(request)
    assert result.status_code == 200

    redis.set.side_effect = RuntimeError("redis unavailable")
    result = await webhook.handle_delivery_callback(RequestStub(query={"status": "sent"}))
    assert result.status_code == 500


@pytest.mark.parametrize(
    ("reply_type", "payload"),
    [
        ("DOCUMENT", {"id": "doc-1", "mime_type": "application/pdf", "filename": "quote.pdf"}),
        ("VIDEO", ["media", "metadata"]),
        ("INTERACTIVE", {"button_reply": {"id": "yes"}}),
    ],
)
def test_webhook_media_and_parser_exception_variants(reply_type, payload):
    parsed = webhook.parse_user_response_callback(
        {
            "replytype": reply_type,
            "customernumber": "9199",
            "replymessage": json.dumps(payload),
        }
    )
    assert parsed["type"] == reply_type.lower()
    assert parsed["content"] == payload

    class BrokenData:
        def get(self, _key):
            raise RuntimeError("broken mapping")

    assert webhook.parse_user_response_callback(BrokenData()) is None


def test_webhook_parser_defaults_and_media_malformed_json():
    parsed = webhook.parse_user_response_callback(
        {"customernumber": "9199", "replymessage": "hello+there"}
    )
    assert parsed["type"] == "text"
    assert parsed["content"] == "hello there"
    assert webhook.parse_user_response_callback({"customernumber": "9199", "replymessage": 5}) is None


@pytest.mark.asyncio
async def test_webhook_enqueue_missing_phone_and_error_notification_failure(monkeypatch):
    timeout = AsyncMock()
    queue = AsyncMock()
    monkeypatch.setattr(webhook, "timeout_service", timeout)
    monkeypatch.setattr(webhook, "message_queue_service", queue)
    await webhook.enqueue_message_async({"content": "hello"})
    timeout.update_user_activity.assert_not_awaited()
    queue.enqueue_message.assert_awaited_once()

    queue.enqueue_message.side_effect = RuntimeError("queue down")
    cancel = AsyncMock(side_effect=RuntimeError("cancel down"))
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)
    await webhook.enqueue_message_async({"content": "hello"})
    cancel.assert_not_awaited()

    redis = AsyncMock()
    redis.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(webhook, "get_redis_service", lambda: redis)
    await webhook.enqueue_message_async({"from": "+9199", "content": "hello"})
    cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_process_message_missing_conflict_concurrent_and_error_paths(monkeypatch):
    monkeypatch.setattr(webhook, "timeout_service", AsyncMock())
    monkeypatch.setattr(webhook, "get_redis_service", lambda: AsyncMock())
    monkeypatch.setattr(webhook, "get_settings", lambda: SimpleNamespace(pending_reply_ttl_seconds=20))
    monkeypatch.setattr("app.utils.logging_utils.UserPhoneContext", lambda _phone: AsyncContext())

    await webhook.process_message_async({"from": "1", "type": "image"})

    create = AsyncMock(return_value=False)
    monkeypatch.setattr(webhook, "create_direct_processing_session", create)
    chat_constructor = MagicMock()
    monkeypatch.setattr("app.services.chat_service.ChatService", chat_constructor)
    await webhook.process_message_async({"from": "1", "type": "interactive", "content": {"id": "b"}})
    chat_constructor.assert_not_called()

    fake_chat = MagicMock()
    fake_chat.process_message = AsyncMock(side_effect=RuntimeError("service failed"))
    fake_chat.cleanup = AsyncMock()
    monkeypatch.setattr("app.services.chat_service.ChatService", lambda **kwargs: fake_chat)
    monkeypatch.setattr("app.database.get_db_session_context", _db_context())
    cleanup = AsyncMock()
    monkeypatch.setattr(webhook, "cleanup_direct_processing_session", cleanup)
    cancel = AsyncMock(side_effect=RuntimeError("cancel failed"))
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)
    await webhook.process_message_async({"from": "1", "type": "image", "content": {"id": "img"}})
    fake_chat.process_message.assert_awaited_once()
    fake_chat.cleanup.assert_awaited_once()
    cleanup.assert_not_awaited()
    cancel.assert_awaited_once()

    create.return_value = True
    fake_chat.process_message.side_effect = RuntimeError("document failed")
    await webhook.process_message_async({"from": "1", "type": "document", "content": {"id": "doc"}})
    cleanup.assert_awaited_once_with("1")


@pytest.mark.asyncio
async def test_webhook_document_processing_exception_is_swallowed():
    chat = MagicMock()
    chat.process_message = AsyncMock(side_effect=RuntimeError("processing"))
    await webhook.process_document_message(
        {"from": "1", "content": {"document": {"link": "mock://file", "filename": "quote.xlsx"}}},
        chat,
    )
    chat.process_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_lifespan_success_startup_and_shutdown(monkeypatch):
    settings = _settings(webhook_health_monitoring_enabled=True)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "init_database", MagicMock())

    import app.procucev_apis.procucev_api_client as api_client

    api_init = AsyncMock()
    api_close = AsyncMock()
    monkeypatch.setattr(api_client, "init_procucev_api_client", api_init)
    monkeypatch.setattr(api_client, "close_procucev_api_client", api_close)

    queue = MagicMock()
    queue._background_tasks = []
    queue.run_batch_poller = lambda: _awaitable()
    queue.run_monitoring_loop = lambda: _awaitable()
    queue.shutdown = AsyncMock()
    monkeypatch.setattr(webhook, "message_queue_service", queue)

    monitor = MagicMock()
    monitor.start_monitoring = lambda: _awaitable()
    monitor.stop_monitoring = AsyncMock()
    monitor._close_session = AsyncMock()
    monitor_module = __import__("app.services.webhook_health_monitor_service", fromlist=["WebhookHealthMonitorService"])
    monkeypatch.setattr(monitor_module, "WebhookHealthMonitorService", lambda: monitor)

    timeout = AsyncMock()
    timeout.try_start_monitoring_if_available.return_value = True
    timeout.stop_monitoring = AsyncMock()
    timeout_module = __import__("app.services.inactivity_timeout_service", fromlist=["get_timeout_service"])
    monkeypatch.setattr(timeout_module, "get_timeout_service", lambda: timeout)

    tasks = []

    def create_task(coro, **kwargs):
        coro.close()
        task = MagicMock(name=kwargs.get("name", "monitor"))
        tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", create_task)
    monkeypatch.setattr(main.gc, "get_objects", lambda: [])

    async with main.lifespan(main.app):
        assert len(queue._background_tasks) == 2
        assert len(tasks) == 3

    api_init.assert_awaited_once()
    api_close.assert_awaited_once()
    queue.shutdown.assert_awaited_once()
    monitor.stop_monitoring.assert_awaited_once()
    timeout.stop_monitoring.assert_awaited_once()


async def _awaitable():
    return None


@pytest.mark.asyncio
async def test_main_lifespan_startup_failures_and_disabled_health_monitor(monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(webhook_health_monitoring_enabled=False))
    monkeypatch.setattr(main, "init_database", MagicMock(side_effect=RuntimeError("db")))
    import app.procucev_apis.procucev_api_client as api_client
    monkeypatch.setattr(api_client, "init_procucev_api_client", AsyncMock(side_effect=RuntimeError("api")))

    queue = MagicMock()
    queue.run_batch_poller = MagicMock(side_effect=RuntimeError("queue"))
    monkeypatch.setattr(webhook, "message_queue_service", queue)
    timeout_module = __import__("app.services.inactivity_timeout_service", fromlist=["get_timeout_service"])
    timeout_module.get_timeout_service = lambda: (_ for _ in ()).throw(RuntimeError("timeout"))
    monkeypatch.setattr(main.gc, "get_objects", lambda: [])

    async with main.lifespan(main.app):
        pass


@pytest.mark.asyncio
async def test_main_lifespan_shutdown_timeout_errors_and_open_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(webhook_health_monitoring_enabled=True))
    monkeypatch.setattr(main, "init_database", MagicMock())
    import app.procucev_apis.procucev_api_client as api_client
    monkeypatch.setattr(api_client, "init_procucev_api_client", AsyncMock())
    monkeypatch.setattr(api_client, "close_procucev_api_client", AsyncMock(side_effect=RuntimeError("close api")))

    queue = MagicMock()
    queue.run_batch_poller = lambda: _awaitable()
    queue.run_monitoring_loop = lambda: _awaitable()
    queue._background_tasks = []
    queue.shutdown = AsyncMock(side_effect=RuntimeError("queue shutdown"))
    monkeypatch.setattr(webhook, "message_queue_service", queue)

    monitor = MagicMock()
    monitor.start_monitoring = lambda: _awaitable()
    monitor.stop_monitoring = AsyncMock()
    monitor._close_session = AsyncMock(side_effect=RuntimeError("session close"))
    monitor_module = __import__("app.services.webhook_health_monitor_service", fromlist=["WebhookHealthMonitorService"])
    monkeypatch.setattr(monitor_module, "WebhookHealthMonitorService", lambda: monitor)

    timeout = AsyncMock()
    timeout.try_start_monitoring_if_available.return_value = False
    timeout.stop_monitoring = AsyncMock(side_effect=RuntimeError("timeout stop"))
    timeout_module = __import__("app.services.inactivity_timeout_service", fromlist=["get_timeout_service"])
    monkeypatch.setattr(timeout_module, "get_timeout_service", lambda: timeout)

    task = MagicMock()
    task.cancel = MagicMock()
    monkeypatch.setattr(main.asyncio, "create_task", lambda coro, **kwargs: (coro.close(), task)[1])
    monkeypatch.setattr(main.asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError()))

    import aiohttp

    class FakeSession:
        closed = False

        def __init__(self):
            self.close = AsyncMock(side_effect=RuntimeError("aio close"))

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    session = FakeSession()
    monkeypatch.setattr(main.gc, "get_objects", lambda: [session])

    async with main.lifespan(main.app):
        pass

    task.cancel.assert_called_once()
    monitor._close_session.assert_awaited_once()
    timeout.stop_monitoring.assert_awaited_once()
    session.close.assert_awaited_once()
