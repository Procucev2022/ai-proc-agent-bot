from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_summary_service as chat_summary_module
import app.services.enhanced_whatsapp_service as enhanced_module
import app.services.location_service as location_module
import app.services.media_downloader_service as media_module
import app.services.opt_out_service as opt_out_module
import app.services.procucev_service as procucev_module
import app.services.rfq_service as rfq_module
import app.services.rfq_status_service as rfq_status_module
import app.services.seller_data_adapter as seller_module
import app.services.support_notification_service as support_module
import app.services.whatsapp_webhook_service as webhook_module
from app.models import SellerRanking, WorkflowType
from app.schemas.user import UserRole
from app.services.whatsapp_service import MessageResponse


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_enhanced_whatsapp_constructor_delegates_all_methods_and_propagates_errors(monkeypatch):
    monkeypatch.setattr(enhanced_module.WhatsAppService, "__init__", lambda self: None)
    monitor = MagicMock()
    contexts = {}

    def monitor_operation(name):
        contexts[name] = AsyncContext()
        return contexts[name]

    monitor.monitor_whatsapp_operation.side_effect = monitor_operation
    monkeypatch.setattr(enhanced_module, "get_service_monitor", lambda: monitor)
    service = enhanced_module.EnhancedWhatsAppService()
    assert service.service_monitor is monitor

    base_methods = {
        "send_message": AsyncMock(return_value="message-result"),
        "send_template_message": AsyncMock(return_value="template-result"),
        "send_interactive_message": AsyncMock(return_value="interactive-result"),
        "send_configurable_buttons": AsyncMock(return_value="buttons-result"),
    }
    for name, method in base_methods.items():
        monkeypatch.setattr(enhanced_module.WhatsAppService, name, method)

    assert await service.send_message("to", "hello") == "message-result"
    assert await service.send_template_message("to", "welcome", ["A"]) == "template-result"
    assert await service.send_interactive_message("to", "list", {"x": 1}) == "interactive-result"
    assert await service.send_configurable_buttons("to", "body", [{"id": "x"}], "head", "foot") == "buttons-result"
    assert [call.args[0] for call in monitor.monitor_whatsapp_operation.call_args_list] == [
        "send_message", "send_template_message", "send_interactive_message", "send_configurable_buttons"
    ]
    assert base_methods["send_message"].call_args.args == ("to", "hello")
    assert base_methods["send_configurable_buttons"].call_args.args == ("to", "body", [{"id": "x"}], "head", "foot")

    failing = AsyncMock(side_effect=RuntimeError("whatsapp down"))
    monkeypatch.setattr(enhanced_module.WhatsAppService, "send_message", failing)
    with pytest.raises(RuntimeError, match="whatsapp down"):
        await service.send_message("to", "hello")


@pytest.mark.asyncio
async def test_support_notification_methods_forward_templates_and_deprecated_handler(monkeypatch):
    email = MagicMock()
    email.send_support_email = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(support_module, "EmailService", lambda: email)
    service = support_module.SupportNotificationService()
    assert service.email_service is email

    cases = [
        (service.notify_registration_failed, ("Full", "u@example.com", "999", "seller"), "user_registration_failed", "seller", {"user_email": "u@example.com", "full_name": "Full", "email": "u@example.com", "contact_number": "999"}),
        (service.notify_otp_validation_failed, ("Full", "u@example.com", "999"), "otp_validation_failed", "buyer", {"user_email": "u@example.com", "full_name": "Full", "email": "u@example.com", "contact_number": "999"}),
        (service.notify_buyer_registration_not_approved, ("Full", "u@example.com", "999"), "buyer_registration_not_approved", "buyer", {"user_email": "u@example.com", "full_name": "Full", "email": "u@example.com", "contact_number": "999"}),
        (service.notify_seller_registration_declined, ("999",), "seller_registration_notification", "seller", {"phone_number": "999"}),
        (service.notify_rfq_categorization_issue, ("R1",), "rfq_item_categorization", "buyer", {"rfq_id": "R1"}),
        (service.notify_non_standard_request, ("details", "Full", "u@example.com", "999", "seller"), "support_non_standard_request", "seller", {"request_details": "details", "full_name": "Full", "email": "u@example.com", "contact_number": "999"}),
        (service.notify_rfq_selected, ("buyer@example.com", "R1"), "rfq_selected_notification", "seller", {"buyer_email": "buyer@example.com", "rfq_id": "R1"}),
        (service.notify_bid_submission_otp_failed, ("Full", "u@example.com", "999"), "bid_submission_otp_failed", "seller", {"full_name": "Full", "email": "u@example.com", "phone_number": "999"}),
    ]
    for method, args, template, role, variables in cases:
        email.send_support_email.reset_mock()
        assert await method(*args) == {"status": "ok"}
        email.send_support_email.assert_awaited_once_with(template, variables, role)

    handler = MagicMock()
    handler.handle_error = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(support_module, "get_global_error_handler", lambda: handler)
    assert await service.notify_api_service_failure("api failed") == {"statusCode": "200", "message": "Notification sent", "status": "Success"}
    assert await service.notify_api_service_failure("api failed", "seller") == {"statusCode": "500", "message": "Failed to send notification", "status": "Failure"}
    assert handler.handle_error.await_args_list[0].args[0].error_message == "api failed"


@pytest.fixture
def rfq_service(monkeypatch):
    settings = SimpleNamespace(
        rfq_max_allowed=2, rfq_followup_note="follow up", support_email="support@example.com", support_contact_info="call us"
    )
    api = MagicMock()
    seller = MagicMock()
    openai = MagicMock()
    helpers = MagicMock()
    openai.extract_entities = AsyncMock()
    helpers.generate_rfq_status_contextual_response = AsyncMock()
    monkeypatch.setattr(rfq_module, "get_settings", lambda: settings)
    monkeypatch.setattr(rfq_module, "RFQAPIService", lambda: api)
    monkeypatch.setattr(rfq_module, "SellerAPIService", lambda: seller)
    monkeypatch.setattr(rfq_module, "OpenAIService", lambda: openai)
    monkeypatch.setattr(rfq_module, "ResponseHelpers", lambda _: helpers)
    return rfq_module.RFQService(), api, seller, openai, helpers


@pytest.mark.asyncio
async def test_rfq_service_constructor_buyer_nested_data_and_empty_ids(rfq_service):
    service, api, seller, openai, helpers = rfq_service
    openai.extract_entities.return_value = {"rfq_id": ["R1", "R2", "R3"]}
    api.get_rfq_status = AsyncMock(return_value={"data": {"data": [{"id": "R1"}]}})
    helpers.generate_rfq_status_contextual_response.return_value = "buyer response"
    user = SimpleNamespace(role="buyer", id="buyer-1", org_id="org", email="b@example.com")
    result = await service.process_rfq_status_request(user, "status")
    assert result == {"status": "rfq_status_found", "rfq_ids": ["R1", "R2"], "rfq_statuses": [{"id": "R1"}], "response_message": "buyer response"}
    api.get_rfq_status.assert_awaited_once_with(client_id="buyer-1", rfq_ids=["R1", "R2"])
    assert helpers.generate_rfq_status_contextual_response.await_args.kwargs["context"]["max_allowed"] == 2

    openai.extract_entities.return_value = {}
    api.get_rfq_status.return_value = {"data": []}
    helpers.generate_rfq_status_contextual_response.return_value = "recent"
    result = await service.process_rfq_status_request(user, "recent")
    assert result["status"] == "recent_rfqs_found"
    assert result["rfq_ids"] == [] and result["rfq_statuses"] == []


@pytest.mark.asyncio
async def test_rfq_service_seller_list_and_error_branches(rfq_service):
    service, api, seller, openai, helpers = rfq_service
    user = SimpleNamespace(role="seller", id="buyer-1", org_id="org-1", email="s@example.com")
    openai.extract_entities.return_value = {"rfq_id": ["R1"]}
    seller.check_seller_rfq_status = AsyncMock(return_value={"data": [{"id": "R1"}]})
    helpers.generate_rfq_status_contextual_response.return_value = "seller response"
    result = await service.process_rfq_status_request(user, "status")
    assert result["rfq_statuses"] == [{"id": "R1"}]
    seller.check_seller_rfq_status.assert_awaited_once_with(seller_id="org-1", rfq_ids=["R1"])

    api.get_rfq_status = AsyncMock(side_effect=RuntimeError("api down"))
    openai.extract_entities.return_value = {"rfq_id": ["R1"]}
    with pytest.raises(RuntimeError, match="api down"):
        await service.process_rfq_status_request(SimpleNamespace(role="buyer", id="b", org_id="o", email="e"), "x")

    with pytest.raises(UnboundLocalError):
        await service.process_rfq_status_request(SimpleNamespace(role="unknown", id="b", org_id="o", email="e"), "x")


@pytest.fixture
def rfq_status_service(monkeypatch):
    rfq = MagicMock()
    monkeypatch.setattr(rfq_status_module, "RFQService", lambda: rfq)
    monkeypatch.setattr(rfq_status_module, "DatabaseManager", lambda session=None: MagicMock(session=session))
    monkeypatch.setattr(rfq_status_module, "ChatSummaryService", lambda: MagicMock())
    monkeypatch.setattr(rfq_status_module, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(rfq_status_module, "SessionManagementService", lambda *args: MagicMock())
    return rfq_status_module.RFQStatusService(whatsapp_service=MagicMock(), session_manager=MagicMock(), db_session=object()), rfq


def test_rfq_status_menus_and_constructor_fallback(monkeypatch, rfq_status_service):
    service, _ = rfq_status_service
    assert len(service._get_role_based_menu_options(SimpleNamespace(role=UserRole.BUYER))) == 3
    assert service._get_role_based_menu_options(SimpleNamespace(role=UserRole.SELLER))[1]["id"] == "rfq_status"
    assert service._get_role_based_menu_options(SimpleNamespace(role=UserRole.UNKNOWN)) == [{"id": "get_support", "title": "Get Support Info"}]

    class Broken:
        @property
        def role(self):
            raise RuntimeError("bad role")

    assert service._get_role_based_menu_options(Broken()) == []

    fallback_wa = MagicMock()
    fallback_session = MagicMock()
    monkeypatch.setattr(rfq_status_module, "WhatsAppService", lambda: fallback_wa)
    monkeypatch.setattr(rfq_status_module, "SessionManagementService", lambda *args: fallback_session)
    rebuilt = rfq_status_module.RFQStatusService()
    assert rebuilt.whatsapp_service is fallback_wa and rebuilt.session_manager is fallback_session


@pytest.mark.asyncio
async def test_rfq_status_handle_buttons_simple_send_save_and_failure(rfq_status_service, monkeypatch):
    service, rfq = rfq_status_service
    wa = service.whatsapp_service
    session_manager = service.session_manager
    rfq.process_rfq_status_request = AsyncMock(return_value={"status": "found", "response_message": "msg", "rfq_ids": ["R1"], "rfq_statuses": ["ok"]})
    wa.send_configurable_buttons = AsyncMock()
    session_manager.save_session = AsyncMock()
    set_workflow = MagicMock()
    monkeypatch.setattr(rfq_status_module.WorkflowManager, "set_workflow_type", set_workflow)
    user = SimpleNamespace(role=UserRole.BUYER, phone_number="919999999999")
    session = SimpleNamespace()
    result = await service.handle_rfq_status_inquiry(user, "status", session)
    assert result == {"status": "found", "message": "msg", "rfq_ids": ["R1"], "rfq_statuses": ["ok"]}
    wa.send_configurable_buttons.assert_awaited_once()
    set_workflow.assert_called_once_with(session, WorkflowType.rfq_status_check, caller="rfq_status_service")
    session_manager.save_session.assert_awaited_once_with(session, WorkflowType.rfq_status_check)

    service._get_role_based_menu_options = MagicMock(return_value=[])
    wa.send_message = AsyncMock()
    assert (await service.handle_rfq_status_inquiry(user, "status", session))["status"] == "found"
    wa.send_message.assert_awaited_once_with(recipient_id=user.phone_number, message="msg")

    rfq.process_rfq_status_request.side_effect = RuntimeError("rfq error")
    result = await service.handle_rfq_status_inquiry(user, "status", session)
    assert result == {"status": "error", "error": "rfq error"}


@pytest.mark.asyncio
async def test_mock_procucev_service_success_failures_statistics(monkeypatch):
    monkeypatch.setattr(procucev_module, "get_db_session", lambda: "db")
    monkeypatch.setattr(procucev_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(procucev_module.asyncio, "sleep", AsyncMock())
    service = procucev_module.MockProcucevService(db_session=None)
    assert service.db_session == "db" and service.stats["api_calls_total"] == 0

    monkeypatch.setattr(procucev_module.uuid, "uuid4", lambda: SimpleNamespace(hex="abcdef1234567890"))
    payment = await service.generate_payment_link("seller", 100, "basic", 10)
    assert payment["success"] and payment["payment_id"] == "pay_abcdef123456"
    email = await service.send_rfq_email("seller", "R1")
    assert email["success"] and email["email_id"] == "email_abcdef1234567890"
    monkeypatch.setattr(procucev_module.random, "randint", lambda low, high: 1)
    monkeypatch.setattr(procucev_module.random, "sample", lambda values, count: values[:count])
    bids = await service.get_seller_pending_bids("seller")
    assert bids["success"] and bids["total_pending"] == 1
    stats = service.get_statistics()
    assert stats["statistics"]["api_calls_total"] == 3
    stats["statistics"]["api_calls_total"] = 0
    assert service.stats["api_calls_total"] == 3

    monkeypatch.setattr(procucev_module.asyncio, "sleep", AsyncMock(side_effect=RuntimeError("sleep failed")))
    assert (await service.generate_payment_link("s", 1, "p", 1))["success"] is False
    assert (await service.send_rfq_email("s", "r"))["success"] is False
    assert (await service.get_seller_pending_bids("s"))["success"] is False


class MediaResponse:
    def __init__(self, status_code=200, headers=None, content=b"data"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


@pytest.fixture
def media_service(monkeypatch):
    settings = SimpleNamespace(WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p", WHATSAPP_MEDIA_DOWNLOAD_URL="https://media")
    monkeypatch.setattr(media_module, "get_settings", lambda: settings)
    path = MagicMock()
    monkeypatch.setattr(media_module, "Path", MagicMock(return_value=path))
    service = media_module.MediaDownloaderService()
    service.download_dir = MagicMock()
    return service


@pytest.mark.asyncio
async def test_media_downloader_success_filename_extensions_and_errors(monkeypatch, media_service):
    service = media_service
    monkeypatch.setattr(media_module.requests, "get", MagicMock(return_value=MediaResponse(headers={"content-type": "image/png"}, content=b"123")))
    writer = MagicMock()
    writer.__enter__.return_value = writer
    monkeypatch.setattr("builtins.open", MagicMock(return_value=writer))
    result = await service.download_media("M1")
    assert result["success"] and result["file_info"]["filename"] == "M1.png"
    writer.write.assert_called_once_with(b"123")

    monkeypatch.setattr(media_module.requests, "get", MagicMock(return_value=MediaResponse(headers={"content-disposition": 'attachment; filename="file.pdf"', "content-type": "application/pdf"}, content=b"pdf")))
    result = await service.download_media("M2")
    assert result["file_info"]["filename"] == "file.pdf"
    result = await service.download_media("M3", "explicit.bin")
    assert result["file_info"]["filename"] == "explicit.bin"
    assert service._get_extension_from_content_type("application/pdf") == ".pdf"
    assert service._get_extension_from_content_type("unknown/type") == ".bin"

    monkeypatch.setattr(media_module.requests, "get", MagicMock(return_value=MediaResponse(status_code=404)))
    assert (await service.download_media("missing"))["error"] == "HTTP 404"
    monkeypatch.setattr(media_module.requests, "get", MagicMock(side_effect=media_module.requests.exceptions.RequestException("offline")))
    assert "Network error: offline" in (await service.download_media("offline"))["error"]
    monkeypatch.setattr(media_module.requests, "get", MagicMock(return_value=MediaResponse()))
    monkeypatch.setattr("builtins.open", MagicMock(side_effect=OSError("disk")))
    assert "Unexpected error: disk" in (await service.download_media("disk"))["error"]


@pytest.mark.asyncio
async def test_media_downloader_file_listing_webhook_and_singleton(monkeypatch, media_service):
    service = media_service
    file_path = MagicMock()
    file_path.is_file.return_value = True
    file_path.name = "a.txt"
    file_path.__str__.return_value = "a.txt"
    file_path.stat.return_value = SimpleNamespace(st_size=4, st_mtime=8)
    service.download_dir.iterdir.return_value = [file_path]
    assert service.list_downloaded_files() == [{"filename": "a.txt", "file_path": "a.txt", "file_size": 4, "modified_time": 8}]
    not_file = MagicMock()
    not_file.is_file.return_value = False
    service.download_dir.iterdir.return_value = [not_file]
    assert service.list_downloaded_files() == []
    service.download_dir.iterdir.side_effect = OSError("list")
    assert service.list_downloaded_files() == []

    existing = MagicMock()
    existing.exists.return_value = True
    existing.stat.return_value = SimpleNamespace(st_size=2, st_mtime=3)
    missing = MagicMock()
    missing.exists.return_value = False
    service.download_dir.__truediv__.side_effect = [existing, missing]
    assert service.get_file_info("yes")["exists"] is True
    assert service.get_file_info("no") == {"exists": False}
    service.download_dir.__truediv__.side_effect = OSError("stat")
    assert service.get_file_info("bad")["error"] == "stat"

    service.download_media = AsyncMock(return_value={"success": True, "file_info": {"filename": "x"}})
    content = {"id": "M", "filename": "x", "mime_type": "image/png"}
    result = await service.download_from_webhook_content(content)
    assert result["file_info"]["original_mime_type"] == "image/png" and result["file_info"]["webhook_content"] is content
    assert (await service.download_from_webhook_content({"filename": "x"}))["error"] == "No media ID found in content"
    service.download_media.return_value = {"success": False, "error": "bad"}
    assert (await service.download_from_webhook_content(content))["success"] is False
    service.download_media.side_effect = RuntimeError("download")
    assert "Webhook download error: download" in (await service.download_from_webhook_content(content))["error"]

    media_module._media_downloader = None
    fake = MagicMock()
    monkeypatch.setattr(media_module, "MediaDownloaderService", lambda: fake)
    assert media_module.get_media_downloader() is fake
    assert media_module.get_media_downloader() is fake


@pytest.fixture
def summary_service(monkeypatch):
    settings = SimpleNamespace(enable_session_summarization=True, max_chat_summaries_for_context=2, redis_session_storage_enabled=False)
    openai = MagicMock()
    openai.generate_session_summary = AsyncMock(return_value="summary")
    monkeypatch.setattr(chat_summary_module, "OpenAIService", lambda: openai)
    monkeypatch.setattr(chat_summary_module, "get_settings", lambda: settings)
    return chat_summary_module.ChatSummaryService(), openai, settings


class DbContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_chat_summary_constructor_generation_success_disabled_and_failure(monkeypatch, summary_service):
    service, openai, settings = summary_service
    session = SimpleNamespace(
        session_id="S1", external_user_id="U1", rfq_ids=["R1", None], rfq_id="legacy", extracted_entities={"a": 1},
        product_items=[{"name": "item"}], conversation_history={"openai_messages": [{"role": "user"}]},
        user_type="buyer", session_state="complete", workflow_type="workflow", outcome="done", rfq_metadata={},
        seller_responses=[], interaction_metrics={}, parent_session_id=None, workflow_state={}, retention_date=None,
        last_activity_at=None, completed_at=datetime(2024, 1, 1, 1, 0), created_at=datetime(2024, 1, 1, 0, 0)
    )
    monkeypatch.setattr(chat_summary_module.SummarizationHelpers, "extract_rich_entities_for_summary", lambda _: {"rich": True})
    summary_class = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(chat_summary_module, "ChatSummary", summary_class)
    db = MagicMock()
    db_manager = MagicMock()
    monkeypatch.setattr(chat_summary_module, "get_db_session", lambda: DbContext(db))
    monkeypatch.setattr(chat_summary_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.DatabaseManager", lambda session=None: db_manager)
    result = await service.generate_session_summary(session)
    assert result.ai_generated_summary == "summary"
    assert summary_class.call_args.kwargs["rfq_ids"] == ["R1"]
    db.add.assert_called_once_with(result)
    db_manager.append_session_data.assert_called_once()

    settings.enable_session_summarization = True
    session.rfq_ids = "R2"
    session.extracted_entities = {}
    session.product_items = []
    session.conversation_history = {}
    openai.generate_session_summary.side_effect = None
    assert (await service.generate_session_summary(session)).rfq_ids == ["R2"]

    settings.enable_session_summarization = False
    assert await service.generate_session_summary(session) is None
    settings.enable_session_summarization = True
    openai.generate_session_summary.side_effect = RuntimeError("openai")
    assert await service.generate_session_summary(session) is None


@pytest.mark.asyncio
async def test_chat_summary_context_duration_and_database_error(monkeypatch, summary_service):
    service, _, settings = summary_service
    record = SimpleNamespace(ai_generated_summary="s", extracted_entities={"x": 1}, rfq_ids=["R"], session_outcome="done", created_at=datetime(2024, 2, 3))
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = [record]
    shared_db = MagicMock(query=MagicMock(return_value=query))
    service.db_session = shared_db
    assert (await service.load_user_context("U"))[0]["date"] == "2024-02-03"

    service.db_session = None
    fallback_db = MagicMock(query=MagicMock(return_value=query))
    monkeypatch.setattr(chat_summary_module, "get_db_session_context", lambda: DbContext(fallback_db))
    assert len(await service.load_user_context("U")) == 1
    query.all.side_effect = RuntimeError("query")
    assert await service.load_user_context("U") == []

    assert service._calculate_duration(SimpleNamespace(created_at=datetime(2024, 1, 1), completed_at=datetime(2024, 1, 1, 1))) == 60
    assert service._calculate_duration(SimpleNamespace(created_at=None, completed_at=datetime.now())) is None
    assert service._calculate_duration(SimpleNamespace(created_at=object(), completed_at=datetime.now())) is None

    settings.redis_session_storage_enabled = True
    service.db_session = None
    query.all.side_effect = None
    openai = service.openai_service
    openai.generate_session_summary.side_effect = RuntimeError("again")
    assert await service.generate_session_summary(SimpleNamespace(session_id="bad")) is None


@pytest.fixture
def fresh_location():
    location_module.LocationService._instance = None
    location_module.LocationService._initialized = False
    service = location_module.LocationService()
    yield service
    location_module.LocationService._instance = None
    location_module.LocationService._initialized = False


@pytest.mark.asyncio
async def test_location_singleton_lookup_approximation_distance_and_statistics(monkeypatch, fresh_location):
    service = fresh_location
    assert location_module.LocationService() is service
    monkeypatch.setattr(location_module, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "Maharashtra"}))
    assert await service.get_coordinates_from_pincode("411001") == {"city": "Pune", "state": "Maharashtra"}
    monkeypatch.setattr(location_module, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert (await service.get_coordinates_from_pincode("561234"))["state"] == "Karnataka"
    assert (await service.get_coordinates_from_pincode("991234"))["state"] == ""
    assert await service.get_coordinates_from_pincode("bad") is None
    monkeypatch.setattr(location_module, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert await service.get_coordinates_from_pincode("411001") is None

    assert (await service._get_approximate_coordinates("110001"))["city"] == "Delhi Region"
    assert (await service._get_approximate_coordinates("991234"))["city"] == "Unknown"
    geodesic_mock = MagicMock(return_value=SimpleNamespace(kilometers=12.345))
    monkeypatch.setattr(location_module, "geodesic", geodesic_mock)
    assert await service.calculate_distance({"lat": 1, "lng": 2}, {"lat": 3, "lng": 4}) == 12.35
    assert await service.calculate_distance({"lat": 1}, {"lat": 3, "lng": 4}) == float("inf")
    geodesic_mock.side_effect = RuntimeError("distance")
    assert await service.calculate_distance({"lat": 1, "lng": 2}, {"lat": 3, "lng": 4}) == float("inf")

    service.add_pincode_mapping("999999", 1, 2, "Town", "State")
    stats = service.get_statistics()
    assert stats["service_name"] == "LocationService" and stats["total_pincode_mappings"] >= 1


@pytest.mark.asyncio
async def test_location_delivery_filter_and_error_branches(monkeypatch, fresh_location):
    service = fresh_location
    lookup = AsyncMock(return_value={"city": "", "state": "API state"})
    monkeypatch.setattr(service, "get_coordinates_from_pincode", lookup)
    assert await service.get_delivery_location({"pincode": "1", "city": "Caller", "state": "Caller state"}) == {"city": "Caller", "state": "API state"}
    lookup.return_value = {}
    assert await service.get_delivery_location({"pincode": "1", "city": "Caller", "state": "Caller state"}) == {"city": "Caller", "state": "Caller state"}
    lookup.return_value = None
    lookup.side_effect = RuntimeError("delivery")
    assert await service.get_delivery_location({}) == {"city": "", "state": ""}

    service.calculate_distance = AsyncMock(side_effect=[5, 20])
    sellers = [{"seller_id": "near", "location": {"lat": 1, "lng": 1}}, {"seller_id": "far", "location": {"lat": 2, "lng": 2}}, {"seller_id": "missing", "location": {}}]
    result = await service.filter_sellers_by_distance(sellers, {"lat": 0, "lng": 0}, 10)
    assert result == [{"seller_id": "near", "location": {"lat": 1, "lng": 1}, "distance_km": 5}]
    service.calculate_distance = AsyncMock(side_effect=RuntimeError("filter"))
    assert await service.filter_sellers_by_distance(sellers[:1], {"lat": 0, "lng": 0}, 10) == []


@pytest.fixture
def opt_out_service(monkeypatch):
    db = MagicMock()
    wa = MagicMock()
    wa.send_message = AsyncMock(return_value=MessageResponse(True))
    openai = MagicMock()
    monkeypatch.setattr(opt_out_module, "get_db_session", lambda: db)
    monkeypatch.setattr(opt_out_module, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(opt_out_module, "OpenAIService", lambda: openai)
    return opt_out_module.OptOutService(), db, wa, openai


def make_seller(**kwargs):
    values = {"seller_id": "S1", "seller_name": "Seller", "categories": ["Tools"], "phone_number": "9199", "opted_out_notifications": None}
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_opt_out_constructor_handlers_permission_eligibility_and_intent(opt_out_service):
    service, db, wa, openai = opt_out_service
    seller = make_seller()
    db.query.return_value.filter.return_value.first.return_value = seller
    service._update_seller_opt_out_status = AsyncMock()
    openai.generate_opt_out_confirmation.return_value = "out"
    assert await service.handle_opt_out_request("9199") == {"success": True, "seller_id": "S1", "opted_out": True, "message_sent": True}
    wa.send_message.return_value = MessageResponse(False, error="not delivered")
    assert (await service.handle_opt_out_request("9199"))["message_sent"] is False
    wa.send_message.return_value = MessageResponse(True)
    openai.generate_opt_in_confirmation.return_value = "in"
    assert await service.handle_opt_in_request("9199") == {"success": True, "seller_id": "S1", "opted_out": False, "message_sent": True}
    service._get_seller_by_phone = AsyncMock(return_value=None)
    assert (await service.handle_opt_out_request("none"))["error"] == "Seller not found"
    service._get_seller_by_phone = AsyncMock(return_value=seller)
    wa.send_message.side_effect = RuntimeError("send")
    assert "send" in (await service.handle_opt_in_request("9199"))["error"]

    service._get_seller_by_id = AsyncMock(return_value=None)
    assert (await service.send_permission_request("missing"))["error"] == "Seller not found"
    service._get_seller_by_id = AsyncMock(return_value=make_seller(opted_out_notifications=True))
    assert (await service.send_permission_request("S1"))["error"] == "Seller already has consent status"
    permission_seller = make_seller(opted_out_notifications=None)
    service._get_seller_by_id = AsyncMock(return_value=permission_seller)
    wa.send_message.side_effect = None
    assert (await service.send_permission_request("S1"))["success"] is True
    wa.send_message.return_value = MessageResponse(False, error="not delivered")
    assert (await service.send_permission_request("S1"))["message_sent"] is False

    for value, expected in [(True, (False, "Seller opted out")), (False, (True, "Seller opted in")), (None, (False, "Permission needed"))]:
        service._get_seller_by_id = AsyncMock(return_value=make_seller(opted_out_notifications=value))
        result = await service.check_seller_notification_eligibility("S1")
        assert (result["eligible"], result["reason"]) == expected
    service._get_seller_by_id = AsyncMock(return_value=None)
    assert (await service.check_seller_notification_eligibility("S1"))["reason"] == "Seller not found"
    service._get_seller_by_id = AsyncMock(side_effect=RuntimeError("lookup"))
    assert "Error: lookup" in (await service.check_seller_notification_eligibility("S1"))["reason"]

    openai.detect_opt_out_intent.return_value = {"intent": "opt_out", "confidence": 90, "reasoning": "clear", "success": True}
    assert service.detect_opt_out_intent("stop")["intent"] == "opt_out"
    openai.detect_opt_out_intent.side_effect = RuntimeError("ai")
    assert service.detect_opt_out_intent("?")["success"] is False


@pytest.mark.asyncio
async def test_opt_out_remote_local_update_and_error_paths(opt_out_service, monkeypatch):
    service, db, _, _ = opt_out_service
    seller = make_seller()
    db.query.return_value.filter.return_value.first.return_value = seller
    remote = MagicMock()
    remote.execute.return_value = SimpleNamespace(rowcount=1)
    monkeypatch.setattr(opt_out_module, "get_remote_db_session", lambda: remote)
    await service._update_seller_opt_out_status("S1", True)
    assert seller.opted_out_notifications is True and db.commit.called and remote.commit.called
    assert remote.execute.call_args.args[1]["opt_out_value"] == 1
    remote.execute.return_value.rowcount = 0
    assert await service._update_remote_opt_out_status("S1", False) is False
    remote.execute.side_effect = RuntimeError("remote")
    assert await service._update_remote_opt_out_status("S1", False) is False
    remote.rollback.assert_called()
    remote.close.assert_called()

    db.query.return_value.filter.return_value.first.return_value = None
    remote.execute.side_effect = None
    remote.execute.return_value.rowcount = 1
    await service._update_seller_opt_out_status("missing", False)
    assert remote.execute.call_args.args[1]["opt_out_value"] == 0


@pytest.fixture
def webhook_service(monkeypatch):
    chat = MagicMock()
    chat.cleanup = AsyncMock()
    chat.process_message = AsyncMock(return_value={"reply": "ok"})
    opt = MagicMock()
    monkeypatch.setattr(webhook_module, "ChatService", lambda db_session=None: chat)
    monkeypatch.setattr(webhook_module, "OptOutService", lambda: opt)
    return webhook_module.WhatsAppWebhookService(db_session="db"), chat, opt


@pytest.mark.asyncio
async def test_webhook_constructor_cleanup_processing_opt_intents_and_errors(webhook_service):
    service, chat, opt = webhook_service
    assert await service.cleanup() is None
    chat.cleanup.side_effect = RuntimeError("cleanup")
    await service.cleanup()
    service.chat_service = None
    await service.cleanup()
    service.chat_service = chat

    result = await service.process_webhook({"from": "919999999999", "content": "hello"})
    assert result["response"] == {"reply": "ok"}
    chat.process_message.assert_awaited_with(user_phone="919999999999", message_content="hello", message_type="text")
    assert (await service.process_webhook({"from": "", "content": "hello"}))["message"] == "Missing required data"
    opt.detect_opt_out_intent.return_value = {"intent": "opt_out", "confidence": 80}
    opt.handle_opt_out_request = AsyncMock(return_value={"success": True})
    result = await service.process_webhook({"type": "text", "from": "919999999999", "content": "stop"})
    assert result["processed_message"]["handled_by"] == "opt_out_service"
    opt.handle_opt_out_request.assert_awaited_once_with("919999999999")
    opt.detect_opt_out_intent.return_value = {"intent": "opt_in", "confidence": 80}
    opt.handle_opt_in_request = AsyncMock(return_value={"success": True})
    assert (await service.process_webhook({"type": "text", "from": "919999999999", "content": "start"}))["processed_message"]["intent_detected"] == "opt_in"
    opt.detect_opt_out_intent.return_value = {"intent": "opt_out", "confidence": 60}
    assert (await service.process_webhook({"type": "text", "from": "919999999999", "content": "border"}))["processed_message"]["handled_by"] == "chat_service"
    assert (await service.process_webhook({"type": "image", "from": "919999999999", "content": {"id": "m"}}))["processed_message"]["type"] == "image"
    chat.process_message.side_effect = RuntimeError("chat")
    assert (await service.process_webhook({"from": "919999999999", "content": "hello"}))["message"] == "chat"


def test_webhook_validation_stats_and_processing_exception(webhook_service):
    service, chat, opt = webhook_service
    valid = {"type": "text", "from": "919999999999", "content": "x"}
    assert service.validate_webhook_data(valid)
    for missing in ("type", "from", "content"):
        data = valid.copy(); data.pop(missing)
        assert service.validate_webhook_data(data) is False
    assert service.validate_webhook_data({**valid, "type": "audio"}) is False
    assert service.validate_webhook_data({**valid, "from": "123"}) is False
    assert service.validate_webhook_data(None) is False
    stats = service.get_processing_stats()
    assert stats["service_status"] == "active" and stats["chat_service_available"] is True
    service.chat_service = None
    assert service.get_processing_stats()["chat_service_available"] is False


@pytest.fixture
def seller_adapter():
    return seller_module.SellerDataAdapter()


def seller_row(**overrides):
    row = {
        "seller_id": "seller-1", "seller_name": "Acme Tools", "ranking": 30, "opted_out_notifications": 0,
        "phone_number": "919999999999", "email": "acme@example.com", "categories": '["Tools", "Hardware"]',
        "location": '{"lat": 1, "lng": 2, "city": "Pune", "state": "Maharashtra"}', "last_active_at": datetime(2024, 1, 1),
        "subscription_credits": 7,
    }
    row.update(overrides)
    return row


def test_seller_adapter_constructor_query_transform_helpers_and_connection(monkeypatch, seller_adapter):
    assert seller_adapter.default_location["city"] == "Bangalore"
    query = seller_adapter._build_seller_query()
    assert "organization o" in query and "HAVING COUNT(odc.category) > 0" in query
    assert seller_adapter._get_subscription_credits("s", SellerRanking.Diamond) == 10
    assert seller_adapter._get_subscription_credits("s", SellerRanking.Platinum) == 5
    assert seller_adapter._get_subscription_credits("s", SellerRanking.Gold) == 0
    assert seller_adapter._get_coverage_km(SellerRanking.Diamond) == 500
    assert seller_adapter._get_coverage_km(SellerRanking.Platinum) == 300
    assert seller_adapter._get_coverage_km(SellerRanking.Gold) == 200
    assert seller_adapter._get_coverage_km(SellerRanking.Titanium) == 150

    seller = seller_adapter._transform_to_seller(seller_row())
    assert seller.ranking is SellerRanking.Diamond and seller.geographic_coverage_km == 500 and seller.subscription_credits == 7
    fallback = seller_adapter._transform_to_seller(seller_row(categories="bad", location="bad", ranking="999", phone_number=None, email=None))
    assert fallback.categories == ["General Trading"] and fallback.location == seller_adapter.default_location
    assert fallback.ranking is SellerRanking.Titanium and fallback.phone_number.startswith("91") and fallback.email == "contact@acmetools.com"

    remote = MagicMock()
    remote.return_value = [seller_row()]
    monkeypatch.setattr(seller_module, "execute_remote_query", remote)
    result = seller_adapter.get_sellers_from_remote(limit=2)
    assert len(result) == 1 and remote.call_args.args[1] == {"limit": 2}
    remote.return_value = [seller_row(), {"bad": True}]
    assert len(seller_adapter.get_sellers_from_remote()) == 1
    remote.side_effect = RuntimeError("remote")
    with pytest.raises(RuntimeError, match="remote"):
        seller_adapter.get_sellers_from_remote()


def test_seller_adapter_lookup_categories_and_connection_branches(monkeypatch, seller_adapter):
    remote = MagicMock(return_value=[seller_row()])
    monkeypatch.setattr(seller_module, "execute_remote_query", remote)
    assert seller_adapter.get_seller_by_id("seller-1").seller_id == "seller-1"
    remote.return_value = []
    assert seller_adapter.get_seller_by_id("missing") is None
    remote.side_effect = RuntimeError("lookup")
    assert seller_adapter.get_seller_by_id("bad") is None

    one = SimpleNamespace(categories=["Tools"])
    two = SimpleNamespace(categories=["Food"])
    seller_adapter.get_sellers_from_remote = MagicMock(return_value=[one, two])
    assert seller_adapter.get_sellers_by_categories(["tools"]) == [one]
    assert seller_adapter.get_sellers_by_categories(["FOOD", "none"]) == [two]

    for result, expected in [([{"seller_count": 2}], True), ([{"seller_count": 0}], False), ([], False), ([{}], False)]:
        remote.side_effect = None
        remote.return_value = result
        assert seller_adapter.test_connection() is expected
    remote.side_effect = RuntimeError("connection")
    assert seller_adapter.test_connection() is False
