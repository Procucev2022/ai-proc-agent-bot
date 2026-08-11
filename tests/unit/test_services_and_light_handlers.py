from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.global_error_handler as geh
import app.services.user_cache_service as ucs
from app.services.domain_check_service import DomainCheckService
from app.services.handlers.attachment_decision_handler import AttachmentDecisionHandler
from app.services.handlers.bfs_seller_bid_handler import BFSSellerBidHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.processors.interactive_message_processor import InteractiveMessageProcessor
import app.services.processors.message_processor_factory as processor_factory
from app.services.processors.text_message_processor import TextMessageProcessor


def _session(**state):
    return SimpleNamespace(workflow_state=state, workflow_type=None, outcome=None, conversation_history={})


def _user(registered=True):
    return SimpleNamespace(phone_number="9199", is_registered=registered)


@pytest.mark.asyncio
async def test_global_error_handler_formats_and_notifies(monkeypatch):
    handler = geh.GlobalErrorHandler.__new__(geh.GlobalErrorHandler)
    handler.settings = SimpleNamespace(
        support_team_numbers=["1", "2"], support_email="support@example.com",
        enable_error_notifications=True, support_contact_info="help@example.com",
        database_mode="local", local_database_url="mysql+pymysql://u:secret@host/db",
        client_database_url=None, remote_database_url="mysql+pymysql://r:pw@remote/db",
        enable_remote_categorization=True, sql_debug=False,
        is_ssl_enabled=lambda: True,
    )
    handler.whatsapp_service = MagicMock()
    handler.whatsapp_service.send_message = AsyncMock(side_effect=[SimpleNamespace(success=True, error=None), SimpleNamespace(success=False, error="no")])
    handler.email_service = SimpleNamespace(email_api=SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"})))
    handler.support_team_numbers = ["1", "2"]
    handler.support_email = "support@example.com"
    handler.notification_enabled = True
    handler.user_error_message = "technical"

    context = geh.ErrorContext("Database Error", "secret failure", "9", "Alice", "a@x", "flow", "API", "/x", {"long": "x" * 600}, datetime.now(timezone.utc))
    formatted = handler._format_support_message(context)
    assert "****" in formatted and "password" not in formatted.lower() and "Database Configuration" in formatted
    assert handler._mask_database_url("mysql+pymysql://u:secret@host/db").endswith("@host/db")
    assert await handler._send_whatsapp_notification("message") is True
    handler.whatsapp_service.send_message.side_effect = RuntimeError("down")
    assert await handler._send_whatsapp_notification("message") is False
    await handler._send_email_notification(context)
    handler.email_service.email_api.send_email.side_effect = RuntimeError("mail")
    await handler._send_email_notification(context)
    await handler._send_user_response("9")
    handler.whatsapp_service.send_message.side_effect = RuntimeError("user")
    await handler._send_user_response("9")

    assert await handler.handle_error(context)
    no_notify = geh.ErrorContext("API Error", "bad", user_phone=None)
    handler.notification_enabled = False
    assert await handler.handle_error(no_notify)
    handler._send_user_response = AsyncMock(side_effect=RuntimeError("fail"))
    assert not await handler.handle_error(geh.ErrorContext("X", "bad", user_phone="1"))
    handler.notification_enabled = True
    assert await handler.handle_error(no_notify)

    captured = AsyncMock()
    monkeypatch.setattr(geh, "get_global_error_handler", lambda: SimpleNamespace(handle_error=captured))
    await geh.handle_api_error("A", "/a", "bad", {"x": 1}, "1", "N", "e", "flow")
    await geh.handle_database_error("db", "1")
    await geh.handle_server_error("server")
    assert captured.await_count == 3


def test_global_error_singleton(monkeypatch):
    geh._global_error_handler = None
    fake = MagicMock()
    monkeypatch.setattr(geh, "GlobalErrorHandler", lambda: fake)
    assert geh.get_global_error_handler() is fake
    assert geh.get_global_error_handler() is fake


@pytest.mark.asyncio
async def test_user_cache_storage_filtering_and_meaningful_data(monkeypatch):
    redis = AsyncMock()
    service = ucs.UserCacheService.__new__(ucs.UserCacheService)
    service.redis_service = redis
    assert service._get_cache_key("+91 99-1") == "user_cache:91991"
    redis.get.return_value = {"meaningful_message": "buy", "meaningful_intent_result": {"intent": "buy"}, "meaningful_message_cached_at": "t", "irrelevant_response": "x"}
    assert await service.store_user_data("+1", [{"username": "a", "selfClient": True}], 10)
    stored = redis.set.await_args.args[1]
    assert stored["meaningful_message"] == "buy" and stored["count"] == 1
    redis.set.return_value = False
    assert not await service.store_user_data("1", [])
    redis.get.side_effect = RuntimeError("down")
    assert not await service.store_user_data("1", [])
    redis.get.side_effect = None

    redis.get.return_value = {"user_data": [{"username": "a"}]}
    assert await service.get_user_data("1") == [{"username": "a"}]
    redis.get.return_value = {"user_data": []}
    assert await service.get_user_data("1") is None
    redis.get.side_effect = RuntimeError("down")
    assert await service.get_user_data("1") is None
    redis.get.side_effect = None

    raw = [{"id": "b", "username": "buyer@example.com", "selfClient": True}, {"id": "s", "username": "seller@example.com", "selfClient": False}]
    result = service._filter_users_by_intent(raw, "buy_something")
    assert result["count"] == 1 and result["unique_emails"] == ["buyer@example.com"]
    assert service._filter_users_by_intent(raw, "sell_something")["count"] == 1
    assert service._filter_users_by_intent(raw, "other")["count"] == 2
    assert not service._filter_users_by_intent([{"bad": object()}], "other")["success"]
    service.get_user_data = AsyncMock(return_value=raw)
    assert (await service.get_filtered_user_data("1", "buy_something"))["count"] == 1
    service.get_user_data.return_value = None
    assert await service.get_filtered_user_data("1", "buy_something") is None
    service.get_user_data.side_effect = RuntimeError("bad")
    assert await service.get_filtered_user_data("1", "buy_something") is None

    service.get_user_data = AsyncMock(return_value={"bad": True})
    assert (await service.get_account_options_for_intent_switch("1", "sell_something"))["has_target_accounts"] is False
    service.get_user_data.return_value = raw
    options = await service.get_account_options_for_intent_switch("1", "sell_something", "seller@example.com")
    assert options["has_target_accounts"] is False and len(options["formatted_options"]) == 2
    options = await service.get_account_options_for_intent_switch("1", "buy_something", "other@example.com")
    assert options["has_target_accounts"] is True and len(options["formatted_options"]) == 3

    redis.get.return_value = {"user_data": raw, "meaningful_message": "m", "meaningful_intent_result": {"x": 1}, "meaningful_message_cached_at": "t"}
    redis.delete.return_value = True
    assert await service.clear_user_data("1")
    assert redis.set.await_args.args[1]["meaningful_message"] == "m"
    redis.get.return_value = {"user_data": raw}
    assert await service.clear_user_data("1", preserve_meaningful_message=False)
    redis.get.return_value = None
    assert not await service.clear_user_data("1")
    redis.get.side_effect = RuntimeError("down")
    assert not await service.clear_user_data("1")
    redis.get.side_effect = None

    redis.exists.return_value = True
    assert await service.is_data_cached("1")
    redis.expire.return_value = True
    assert await service.refresh_cache_expiry("1", 3)
    redis.expire.side_effect = RuntimeError("down")
    assert not await service.refresh_cache_expiry("1")
    redis.expire.side_effect = None

    redis.get.return_value = None
    redis.set.return_value = True
    assert await service.store_meaningful_message("1", "hello", {"intent": "buy"})
    redis.get.return_value = {"meaningful_message": "hello", "meaningful_intent_result": {"intent": "buy"}}
    assert (await service.get_meaningful_message("1"))["message"] == "hello"
    assert await service.clear_meaningful_message("1")
    redis.get.return_value = {"x": 1}
    assert await service.get_meaningful_message("1") is None
    assert await service.clear_meaningful_message("1")
    redis.get.side_effect = RuntimeError("down")
    assert await service.get_meaningful_message("1") is None
    redis.get.side_effect = None


def test_user_cache_singleton(monkeypatch):
    ucs._user_cache_service = None
    fake = MagicMock()
    monkeypatch.setattr(ucs, "UserCacheService", lambda: fake)
    assert ucs.get_user_cache_service() is fake
    assert ucs.get_user_cache_service() is fake


@pytest.mark.asyncio
async def test_domain_check_fallback_ai_and_approval(monkeypatch, tmp_path):
    service = DomainCheckService.__new__(DomainCheckService)
    service.tools_dir = tmp_path
    service.openai_service = SimpleNamespace(default_model="m", _load_prompt=MagicMock(return_value="system"), client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    service.register_api_service = SimpleNamespace(user_approval=AsyncMock())
    service.whatsapp_service = AsyncMock()
    service.support_notification_service = AsyncMock()
    assert service.normalize("Acme Pvt.!") == "acmepvt"
    assert service.fallback_domain_match_score("", "Acme") == 0
    assert service.fallback_domain_match_score("a@gmail.com", "Acme") == 0
    assert service.fallback_domain_match_score("sales@acme.com", "Acme") == 95
    assert service.fallback_domain_match_score("sales@acmeindustries.com", "Acme") == 85
    assert service.fallback_domain_match_score("sales@acme.com", "Acme") == 95
    assert service.fallback_domain_match_score("sales@zzzz.com", "Acme") == 0

    tool = {"type": "function", "name": "analyze_domain_match"}
    (tmp_path / "domain_matching.json").write_text(json.dumps(tool), encoding="utf-8")
    fn_call = SimpleNamespace(type="function_call", arguments=json.dumps({"score": 80, "match_type": "strong", "confidence": "high", "reasoning": "match"}))
    service.openai_service.client.responses.create.return_value = SimpleNamespace(output=[fn_call])
    result = await service.ai_domain_match_analysis("a@acme.com", "Acme")
    assert result["success"] and result["analysis"]["score"] == 80
    service.openai_service.client.responses.create.return_value = SimpleNamespace(output=[])
    assert not (await service.ai_domain_match_analysis("a@acme.com", "Acme"))["success"]
    service.openai_service.client.responses.create.side_effect = RuntimeError("AI")
    assert not (await service.ai_domain_match_analysis("a@acme.com", "Acme"))["success"]

    service.ai_domain_match_analysis = AsyncMock(return_value={"success": True, "analysis": {"score": 80, "match_type": "strong", "confidence": "high", "reasoning": "yes"}, "method": "ai"})
    assert (await service.check_domain_match("a@acme.com", "Acme"))["approved"]
    assert (await service.check_domain_match("", "Acme"))["match_type"] == "empty_input"
    service.ai_domain_match_analysis.return_value = {"success": False, "error": "down"}
    assert (await service.check_domain_match("a@acme.com", "Acme"))["method"] == "fallback"
    service.ai_domain_match_analysis.side_effect = RuntimeError("bad")
    assert (await service.check_domain_match("a@acme.com", "Acme"))["match_type"] == "error"

    service.register_api_service.user_approval.return_value = {"statusCode": "200", "status": "Success", "message": "ok"}
    assert (await service.user_approval_api_call("u"))["approved"]
    service.register_api_service.user_approval.return_value = {"status": "Failed"}
    assert not (await service.user_approval_api_call("u"))["approved"]
    service.register_api_service.user_approval.side_effect = RuntimeError("api")
    assert (await service.user_approval_api_call("u"))["status"] == "error"

    session = _session()
    assert (await service.process_user_approval("1", "u", session, {"approved": False, "method": "fallback", "reasoning": "no"}))["status"] == "redirected_to_support"
    session = _session()
    service.user_approval_api_call = AsyncMock(return_value={"approved": False, "message": "no"})
    assert (await service.process_user_approval("1", "u", session, {"approved": True, "method": "ai"}))["reason"] == "api_approval_failed"
    session = _session()
    service.user_approval_api_call.return_value = {"approved": True}
    monkeypatch.setattr("app.procucev_apis.auth_apis.AuthAPIService.authenticate_user", AsyncMock(return_value={"success": False}))
    assert (await service.process_user_approval("1", "u", session, {"approved": True, "method": "ai"}))["reason"] == "refresh_failed_after_approval"
    session = _session()
    monkeypatch.setattr("app.procucev_apis.auth_apis.AuthAPIService.authenticate_user", AsyncMock(return_value={"success": True, "data": [{"id": "u", "approved": True, "username": "a"}]}))
    monkeypatch.setattr("app.services.authentication_service.AuthenticationService.store_user_session_with_email", AsyncMock(return_value=True))
    success = await service.process_user_approval("1", "u", session, {"approved": True, "method": "ai"})
    assert success["approved"] and session.workflow_state == {}
    assert (await service._redirect_to_support("1", "x", "d", session))["exit_flow"]


@pytest.mark.asyncio
async def test_attachment_handler_paths(monkeypatch):
    whatsapp, response, purchase, manager = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    handler = AttachmentDecisionHandler(whatsapp, response, purchase, manager)
    user, session = _user(), _session(awaiting_attachment_decision=True)
    monkeypatch.setattr("app.services.helpers.attachment_helpers.AttachmentHelpers.get_attachment_summary", lambda _s: {"approved_count": 2})
    monkeypatch.setattr("app.services.helpers.attachment_helpers.AttachmentHelpers.approve_pending_attachment", lambda _s: True)
    response.generate_contextual_response.return_value = "ok"
    purchase.handle_purchase_intent.return_value = {"status": "next"}
    assert (await handler.handle_attachment_decision(user, session, "yes"))["status"] == "next"
    monkeypatch.setattr("app.services.helpers.attachment_helpers.AttachmentHelpers.approve_pending_attachment", lambda _s: False)
    await handler.handle_attachment_decision(user, session, "attach")
    monkeypatch.setattr("app.services.helpers.attachment_helpers.AttachmentHelpers.reject_pending_attachments", lambda _s: None)
    await handler.handle_attachment_decision(user, session, "no")
    monkeypatch.setattr("app.services.helpers.attachment_helpers.AttachmentHelpers.get_attachment_summary", MagicMock(side_effect=RuntimeError("bad")))
    session.workflow_state = {"awaiting_attachment_decision": True}
    await handler.handle_attachment_decision(user, session, "x")
    assert manager.save_session.await_count >= 2


@pytest.mark.asyncio
async def test_bfs_bid_handler_paths(monkeypatch):
    whatsapp, auth, manager, otp, api = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    handler = BFSSellerBidHandler.__new__(BFSSellerBidHandler)
    handler.whatsapp_service, handler.authentication_service = whatsapp, auth
    handler.session_manager, handler.otp_service, handler.bfs_api_service = manager, otp, api
    handler.settings = SimpleNamespace()
    session = _session()
    monkeypatch.setattr("app.services.workflow_manager.WorkflowManager.set_workflow_type", lambda *args, **kwargs: None)
    assert (await handler.handle_accept_bid_click("1", "", "s", session))["status"] == "error"
    handler.check_seller_auth_state = AsyncMock(return_value={"state": "not_auth"})
    handler.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await handler.handle_accept_bid_click("1", "b", "s", session))["status"] == "otp"
    handler.check_seller_auth_state.return_value = {"state": "wrong", "current_user": {}}
    handler.prompt_seller_account_switch = AsyncMock(return_value={"status": "switch"})
    assert (await handler.handle_reject_bid_click("1", "b", "s", session))["status"] == "switch"
    handler.check_seller_auth_state.return_value = {"state": "correct_seller"}
    api.accept_bid_by_seller.return_value = {"success": True}
    assert (await handler.handle_accept_bid_click("1", "b", "s", session))["status"] == "bfs_bid_accepted"
    api.reject_bid_by_seller.return_value = {"success": False, "error": "no"}
    assert (await handler.handle_reject_bid_click("1", "b", "s", session))["status"] == "error"
    api.accept_bid_by_seller.side_effect = RuntimeError("api")
    assert (await handler.handle_accept_bid_click("1", "b", "s", session))["status"] == "error"
    session.workflow_state = {}
    assert (await handler.handle_otp_validated("1", session))["status"] == "error"
    session.workflow_state = {"bfs_user_uuid": "b", "action": "reject"}
    handler._execute_bid_action = AsyncMock(return_value={"status": "done"})
    assert (await handler.handle_otp_validated("1", session))["status"] == "done"
    session.workflow_state = {"action": "accept"}
    handler.handle_seller_switch_response = AsyncMock(return_value={"status": "declined"})
    assert (await handler.handle_switch_response("1", session, "no"))["status"] == "declined"


@pytest.mark.asyncio
async def test_intent_switch_handler_paths():
    handler = IntentSwitchHandler.__new__(IntentSwitchHandler)
    handler.whatsapp_service = AsyncMock()
    handler.response_helpers = AsyncMock()
    handler.response_helpers.generate_contextual_response.return_value = "choice"
    handler.openai_service = AsyncMock()
    empty = _session()
    assert not await handler.should_handle_intent_switch(empty, "buy_something", 100)
    active = _session(extracted_entities=[{"product_name": "laptop"}])
    assert not await handler.should_handle_intent_switch(active, "buy_something", 50)
    active.workflow_state["pending_intent_switch"] = {}
    assert not await handler.should_handle_intent_switch(active, "buy_something", 100)
    active.workflow_state.pop("pending_intent_switch")
    active.workflow_type = SimpleNamespace(value="seller_rfq_view")
    assert await handler.should_handle_intent_switch(active, "buy_something", 100)
    active.workflow_type = SimpleNamespace(value="rfq_creation")
    assert await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "new_request"})
    assert not await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "collecting", "references_existing_data": True})
    active.workflow_type = SimpleNamespace(value="other")
    assert await handler.should_handle_intent_switch(active, "general_inquiry", 100)

    user, session = _user(), _session(extracted_entities=[{"product_name": "laptop"}])
    session.workflow_type = "rfq_creation"
    assert (await handler.handle_intent_switch_choice(user, session, "buy", "buy_something", {"confidence": 1}))["status"] == "intent_switch_choice_presented"
    handler.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "continue_current"}
    assert (await handler.handle_intent_switch_response(user, session, "1"))["status"] == "continue_current_workflow"
    session.workflow_state["pending_intent_switch"] = {"new_intent": "sell_something", "new_intent_message": "sell", "intent_result": {}}
    handler.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "switch_to_new"}
    assert (await handler.handle_intent_switch_response(user, session, "2"))["status"] == "switch_to_new_intent"
    session.workflow_state["pending_intent_switch"] = "bad"
    assert (await handler.handle_intent_switch_response(user, session, "x"))["status"] == "corrupted_intent_switch_data"
    session.workflow_state["pending_intent_switch"] = {"new_intent": "x"}
    assert (await handler.handle_intent_switch_response(user, session, "x"))["status"] == "error"
    session.workflow_state["pending_intent_switch"] = {"new_intent": "x", "new_intent_message": "x", "intent_result": {}}
    handler.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("AI")
    assert (await handler.handle_intent_switch_response(user, session, "1 continue current"))["status"] == "continue_current_workflow"
    assert handler._get_intent_description("unknown") == "handle your request"
    handler._abandon_current_workflow(session)
    assert session.workflow_type is None


@pytest.mark.asyncio
async def test_processors_and_factory(monkeypatch):
    deps = {"x": 1}
    factory = processor_factory.MessageProcessorFactory(deps)
    monkeypatch.setattr(processor_factory, "TextMessageProcessor", lambda **kwargs: "text")
    monkeypatch.setattr(processor_factory, "InteractiveMessageProcessor", lambda **kwargs: "interactive")
    monkeypatch.setattr(processor_factory, "ExcelMessageProcessor", lambda **kwargs: "excel")
    monkeypatch.setattr(processor_factory, "ImageMessageProcessor", lambda **kwargs: "image")
    assert factory.get_processor("text") == "text" and factory.get_processor("text") == "text"
    assert factory.get_processor("interactive") == "interactive"
    assert factory.get_processor("excel_upload") == "excel"
    assert factory.get_processor("image") == "image" and factory.get_processor("document") == "image"
    with pytest.raises(ValueError): factory.get_processor("bad")

    interactive = InteractiveMessageProcessor(AsyncMock())
    user, session = _user(), _session()
    assert (await interactive.process_interactive_message(user, session, '{"type":"list_reply","list_reply":{"id":"l"}}'))["status"] == "list_handled"
    assert (await interactive.process_interactive_message(user, session, "bad"))["status"] == "delegate_to_text_processor"
    assert (await interactive.process_interactive_message(user, session, {"type": "button_reply", "button_reply": {"id": "other"}}))["status"] == "button_handled"
    monkeypatch.setattr("app.services.processors.interactive_message_processor.SellerNotificationService.BUTTON_INTERESTED", "rfq_interested")
    monkeypatch.setattr("app.services.processors.interactive_message_processor.SellerRFQInterestHandler", lambda **kwargs: SimpleNamespace(handle_rfq_interest_click=AsyncMock(return_value={"status": "interest"})))
    assert (await interactive.process_interactive_message(user, session, {"type": "button_reply", "button_reply": {"id": "rfq_interested_r_s"}}))["status"] == "interest"
    assert (await interactive._handle_rfq_interested(user, "rfq_interested_", session))["status"] == "error"

    intent = MagicMock(classify_intent=MagicMock(return_value={"intent": "general_inquiry", "confidence": 0.8}))
    response, whatsapp, rfq = AsyncMock(), AsyncMock(), AsyncMock()
    text = TextMessageProcessor(intent, MagicMock(), whatsapp, response, AsyncMock(), MagicMock(), rfq, MagicMock())
    response.generate_contextual_response.return_value = "answer"
    assert (await text.process_text_message(_user(False), session, "hi"))["status"] == "registration_needed"
    assert (await text.process_text_message(_user(True), session, "hi"))["status"] == "general_inquiry_handled"
    intent.classify_intent.return_value = {"intent": "unknown", "confidence": 0.1}
    response.generate_clarification_response.return_value = "clarify"
    assert (await text.process_text_message(_user(True), session, "huh"))["status"] == "clarification_sent"
    session.workflow_state["clarification_retry_count"] = 2
    assert (await text.process_text_message(_user(True), session, "huh"))["status"] == "clarification_limit_reached_exit"
    intent.classify_intent.return_value = {"intent": "rfq_status_check", "confidence": 0.9}
    rfq.process_rfq_status_request.return_value = {"status": "ok", "response_message": "status", "rfq_ids": [1], "rfq_statuses": ["open"]}
    assert (await text.process_text_message(_user(True), _session(), "status"))["status"] == "ok"
    assert await text._handle_purchase_intent(_user(), session, "x")
    assert await text._handle_attachment_decision(_user(), session, "x")
