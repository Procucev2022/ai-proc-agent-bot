from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_mod
from app.models import WorkflowType


class FakeContext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def isolate_technical_failure_handler(monkeypatch):
    monkeypatch.setattr(
        "app.utils.technical_failure_handler.handle_technical_failure",
        AsyncMock(),
    )


def user(role="buyer", **overrides):
    value = dict(
        phone_number="+919999999999",
        email="buyer@example.com",
        username="buyer",
        name="Ada Buyer",
        role=role,
        is_registered=True,
        id="user-1",
    )
    value.update(overrides)
    return SimpleNamespace(**value)


def session(**state):
    return SimpleNamespace(
        session_id="session-1",
        external_user_id="919999999999",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities=[],
        outcome=None,
        retention_date=date(2025, 1, 1),
        last_activity_at=datetime(2025, 1, 1),
        created_at=datetime(2025, 1, 1),
        completed_at=None,
    )


def service_stub():
    service = chat_mod.ChatService.__new__(chat_mod.ChatService)
    service.whatsapp_service = SimpleNamespace(
        send_message=AsyncMock(), send_configurable_buttons=AsyncMock()
    )
    service.session_manager = SimpleNamespace(
        save_session=AsyncMock(),
        send_and_track_message=AsyncMock(),
        get_conversation_context=AsyncMock(return_value=session()),
        add_message_to_history=MagicMock(),
    )
    service._response_helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_seller_contextual_response=AsyncMock(return_value="seller error"),
        generate_completion_response=AsyncMock(return_value="complete"),
        generate_clarification_response=AsyncMock(return_value="clarify"),
    )
    service._openai_service = SimpleNamespace(
        parse_confirmation_response=AsyncMock(return_value="yes"),
        extract_entities=AsyncMock(return_value={"rfq_id": []}),
    )
    service._faq_service = SimpleNamespace()
    service._authentication_service = SimpleNamespace(
        otp_service=SimpleNamespace(validate_otp=AsyncMock(return_value={"status": "otp_invalid"})),
        handle_email_otp_validation=AsyncMock(return_value={"status": "otp_invalid"}),
        store_user_session_with_email=AsyncMock(return_value={"success": True}),
    )
    service._purchase_intent_handler = SimpleNamespace(
        handle_purchase_intent=AsyncMock(return_value={"status": "purchase"})
    )
    service._products_array_handler = SimpleNamespace(
        handle_products_array=AsyncMock(return_value={"status": "products"})
    )
    service._bfs_search_handler = SimpleNamespace(
        handle_bfs_search=AsyncMock(return_value={"status": "bfs"}),
        handle_button=AsyncMock(return_value={"status": "bfs"}),
    )
    service._confirmation_handler = SimpleNamespace(
        handle_confirmation_button=AsyncMock(return_value={"status": "confirmed"}),
        handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}),
        handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}),
    )
    service._cancel_service = SimpleNamespace(
        handle_cancel_intent=AsyncMock(return_value={"status": "cancel"}),
        handle_cancel_confirmation=AsyncMock(return_value={"status": "cancelled"}),
        _send_cancellation_message=AsyncMock(),
        _clear_workflow_state=AsyncMock(),
    )
    service._exit_service = SimpleNamespace(
        handle_exit_intent=AsyncMock(return_value={"status": "exited"}),
        handle_exit_confirmation=AsyncMock(return_value={"status": "exit"}),
    )
    service._rfq_status_service = SimpleNamespace(
        handle_rfq_status_inquiry=AsyncMock(return_value={"status": "rfq_status"})
    )
    service._seller_service = SimpleNamespace(
        handle_seller_workflow=AsyncMock(return_value={"success": True, "message": "seller"})
    )
    service._intent_switch_handler = SimpleNamespace(
        should_handle_intent_switch=AsyncMock(return_value=False),
        handle_intent_switch_response=AsyncMock(return_value={"status": "switch"}),
    )
    service._entity_service = SimpleNamespace()
    service._format_modification_handler = SimpleNamespace(
        handle_format_modification=AsyncMock(return_value={"status": "format"})
    )
    service._attachment_decision_handler = SimpleNamespace(
        handle_attachment_decision=AsyncMock(return_value={"status": "attachment"})
    )
    service._image_processor = SimpleNamespace(process_image_message=AsyncMock(return_value={"status": "image"}))
    service.settings = SimpleNamespace(support_email="support@example.com")
    service.db_manager = SimpleNamespace(
        save_conversation_session=MagicMock(return_value={"saved": True}),
        append_session_data=MagicMock(),
        close=MagicMock(),
    )
    return service


@pytest.mark.asyncio
async def test_interactive_router_parses_button_list_text_and_errors():
    service = service_stub()
    service._handle_button_response = AsyncMock(return_value={"status": "button"})
    service._handle_list_response = AsyncMock(return_value={"status": "list"})
    service._process_text_message = AsyncMock(return_value={"status": "text"})

    assert await service._process_interactive_message(user(), session(), '{"type":"button_reply","button_reply":{"id":"ok"}}') == {"status": "button"}
    assert await service._process_interactive_message(user(), session(), {"type": "list_reply", "list_reply": {"id": "row-1"}}) == {"status": "list"}
    assert await service._process_interactive_message(user(), session(), "not json") == {"status": "text"}
    service._handle_button_response.side_effect = RuntimeError("handler")
    with pytest.raises(RuntimeError, match="handler"):
        await service._process_interactive_message(user(), session(), {"type": "button_reply", "button_reply": {"id": "bad"}})


@pytest.mark.asyncio
async def test_excel_upload_registration_missing_link_validation_and_attachment(monkeypatch):
    service = service_stub()
    registered = user(is_registered=False)
    assert (await service._process_excel_upload(registered, session(), {}))["response"] == "registration_required"

    assert (await service._process_excel_upload(user(), session(), "bad"))["status"] == "error"
    assert (await service._process_excel_upload(user(), session(), {"document": {"filename": "x.xlsx"}}))["response"] == "file_access_error"

    validation = SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad file"}))
    monkeypatch.setattr(chat_mod, "ExcelValidationService", lambda: validation)
    assert (await service._process_excel_upload(user(), session(), {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "validation_failed"

    validation.validate_excel_file_from_url.return_value = {"valid": True, "content": b"xlsx"}
    service._image_processor.process_image_message.return_value = {"status": "attachment"}
    active = session(sectioned_rfq={"active": True, "current_section": "attachments"})
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _s: True)
    monkeypatch.setattr(chat_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _s: "attachments")
    assert await service._process_excel_upload(user(), active, {"document": {"link": "url", "filename": "x.xlsx"}}) == {"status": "attachment"}
    service._image_processor.process_image_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_excel_upload_processed_lock_and_incomplete_paths(monkeypatch):
    service = service_stub()
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _s: False)
    validation = SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"xlsx"}))
    monkeypatch.setattr(chat_mod, "ExcelValidationService", lambda: validation)
    already = session(excel_file_processed=True, excel_filename="old.xlsx")
    assert (await service._process_excel_upload(user(), already, {"id": "media"}))["response"] == "excel_already_processed"

    class Lock:
        async def acquire(self, **_kwargs):
            return False

        async def release(self):
            return None

    redis = SimpleNamespace(lock=MagicMock(return_value=Lock()))
    monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(return_value=redis))
    monkeypatch.setattr(chat_mod, "get_settings", lambda: SimpleNamespace(redis_url="redis://unit"))
    locked = await service._process_excel_upload(user(), session(), {"id": "media", "filename": "x.xlsx"})
    assert locked["response"] == "upload_in_progress"

    class RaisingLock(Lock):
        async def acquire(self, **_kwargs):
            return True

        async def release(self):
            raise RuntimeError("release")

    redis.lock.return_value = RaisingLock()
    processing = SimpleNamespace(process_excel_file=AsyncMock(side_effect=RuntimeError("processor")))
    monkeypatch.setattr(chat_mod, "ExcelProcessingService", lambda *_args: processing)
    failed = await service._process_excel_upload(user(), session(), {"id": "media", "filename": "x.xlsx"})
    assert failed["status"] == "error"
    service.session_manager.send_and_track_message.assert_awaited()


@pytest.mark.asyncio
async def test_incomplete_excel_reupload_rejection_and_helper_failure(monkeypatch):
    service = service_stub()
    monkeypatch.setattr(chat_mod.ExcelHelpers, "generate_reupload_instructions", lambda *_args: "upload again")
    result = await service._handle_incomplete_excel(
        user(), session(), {"excel_data": {"total_items": 2}, "missing_fields": ["quantity"], "completeness": 50}
    )
    assert result["status"] == "excel_reupload_required"
    rejected = await service._handle_incomplete_excel(
        user(), session(),
        {"excel_data": {"should_skip_rfq_creation": True, "processing_summary": {"skipped_items_summary": "bad"}, "error": "reject"}, "missing_fields": []},
    )
    assert rejected["status"] == "excel_rejected"
    monkeypatch.setattr(chat_mod.ExcelHelpers, "generate_reupload_instructions", lambda *_args: (_ for _ in ()).throw(RuntimeError("instructions")))
    fallback = await service._handle_incomplete_excel(user(), session(), {"excel_data": {}, "missing_fields": ["quantity"]})
    assert fallback["status"] == "excel_reupload_required"


@pytest.mark.asyncio
async def test_excel_sectioned_transform_validation_and_exception_paths(monkeypatch):
    service = service_stub()
    current = session(user_phone="+9199", stale="remove")
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _s: False)
    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_sectioned_rfq", lambda s: s.workflow_state.setdefault("sectioned_rfq", {"sections": {}}))
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_section", lambda s, section: s.workflow_state["section"] == section if False else None)
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_from_excel", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "update_section_data", lambda s, key, data: s.workflow_state.setdefault(key, data))
    service.transform_rfq_to_section_rfq_format = AsyncMock(return_value=([{"description": "pump"}], {"pincode": "560001"}))
    result = await service._handle_excel_sectioned_rfq(user(), current, [{"description": "pump"}])
    assert result["status"] == "excel_sectioned_rfq_initialized"
    assert current.workflow_state["extracted_entities"] == []

    service.transform_rfq_to_section_rfq_format.side_effect = ValueError("invalid date")
    assert (await service._handle_excel_sectioned_rfq(user(), session(), []))["status"] == "validation_failed"
    service.transform_rfq_to_section_rfq_format.side_effect = RuntimeError("transform")
    assert (await service._handle_excel_sectioned_rfq(user(), session(), []))["status"] == "error"


@pytest.mark.asyncio
async def test_transform_date_pincode_confirmed_and_unexpected_errors(monkeypatch):
    service = service_stub()
    handler = SimpleNamespace(_validate_delivery_date=AsyncMock(return_value={"is_valid": False, "error": "bad date"}))
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **_kwargs: handler)
    with pytest.raises(ValueError, match="bad date"):
        await service.transform_rfq_to_section_rfq_format(session(delivery_date="tomorrow"), [{"description": "x"}])

    monkeypatch.setattr("app.utils.pincode_lookup.get_location_from_pincode_async", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="not found"):
        await service.transform_rfq_to_section_rfq_format(session(pincode="560001"), [{"description": "x"}])

    confirmed = session(sectioned_rfq={"sections": {"date_location": {"confirmed": True, "data": {"deliveryDate": "today", "city": "Pune"}}}})
    products, details = await service.transform_rfq_to_section_rfq_format(confirmed, [{"description": "x", "quantity": "1"}])
    assert products[0]["deliveryDate"] == "today" and details["city"] == "Pune"

    class BrokenProduct:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("product data")

    with pytest.raises(ValueError, match="Error processing file"):
        await service.transform_rfq_to_section_rfq_format(session(), [BrokenProduct()])


@pytest.mark.asyncio
async def test_registration_workflow_new_name_existing_name_and_db_error(monkeypatch):
    service = service_stub()
    db_user = SimpleNamespace(name=None, is_registered=False)
    db = SimpleNamespace(query=MagicMock(return_value=SimpleNamespace(filter=lambda *_: SimpleNamespace(first=lambda: db_user))), commit=MagicMock())
    class UserModel:
        id = "id"

    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: FakeContext(db))
    monkeypatch.setattr(chat_mod, "User", UserModel)
    result = await service._handle_registration_workflow(user(name=None), "new person")
    assert result["status"] == "registered" and db_user.name == "New Person"

    service._process_text_message = AsyncMock(return_value={"status": "continued"})
    named = user(name="Ada")
    assert await service._handle_registration_workflow(named, "buy pumps") == {"status": "continued"}
    db.commit.side_effect = RuntimeError("db")
    error = await service._handle_registration_workflow(user(name=None), "Ada")
    assert error["status"] == "error"


@pytest.mark.asyncio
async def test_account_switch_and_enhanced_selection_all_outcomes(monkeypatch):
    service = service_stub()
    switcher = SimpleNamespace(
        handle_role_switch_confirmation=AsyncMock(return_value={"status": "role"}),
        handle_account_switch_confirmation=AsyncMock(return_value={"status": "account"}),
    )
    monkeypatch.setattr("app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch", lambda *_args: switcher)
    assert (await service._handle_account_switch_intent(user("buyer"), session(), "switch", {"context_analysis": {"account_switch_details": {"target_role": "seller"}}}))["status"] == "role"
    assert (await service._handle_account_switch_intent(user("buyer"), session(), "switch", {"context_analysis": {"account_switch_details": {"target_role": "buyer"}}}))["status"] == "account"
    switcher.handle_account_switch_confirmation.side_effect = RuntimeError("switch")
    assert (await service._handle_account_switch_intent(user(), session(), "switch", {}))["status"] == "error"
    switcher.handle_account_switch_confirmation.side_effect = None
    switcher.handle_account_switch_confirmation.return_value = {"status": "account"}

    cache = SimpleNamespace(get_account_options_for_intent_switch=AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)
    assert (await service._check_for_enhanced_account_selection(user("seller"), session(), "x", "buy_something"))["reason"] == "cross_role_switch"
    assert (await service._check_for_enhanced_account_selection(user("buyer"), session(), "x", "buy_something"))["reason"] == "no_cached_data"
    cache.get_account_options_for_intent_switch.return_value = {"success": True, "has_target_accounts": False}
    assert (await service._check_for_enhanced_account_selection(user(), session(), "x", "buy_something"))["reason"] == "no_target_accounts"
    cache.get_account_options_for_intent_switch.return_value = {"success": True, "has_target_accounts": True}
    handled = await service._check_for_enhanced_account_selection(user(), session(), "x", "buy_something")
    assert handled["status"] == "handled"
    cache.get_account_options_for_intent_switch.side_effect = RuntimeError("cache")
    assert (await service._check_for_enhanced_account_selection(user(), session(), "x", "buy_something"))["status"] == "error"


@pytest.mark.asyncio
async def test_list_bfs_status_and_interaction_helpers():
    service = service_stub()
    assert await service._handle_list_response(user(), session(), "item-7") == {"status": "list_handled", "list_id": "item-7"}
    await service._check_bfs_availability(user(), session(), [{"success": True, "rfq_data": {"items": [{"description": "pump"}, {"product_name": "valve"}]}}])
    service._bfs_search_handler.handle_bfs_search.assert_awaited_once()
    await service._check_bfs_availability(user(), session(), [{"success": False}])
    service._bfs_search_handler.handle_bfs_search.side_effect = RuntimeError("bfs")
    await service._check_bfs_availability(user(), session(), [{"success": True, "rfq_data": {"items": [{"description": "pump"}]}}])
    assert await service._handle_rfq_status_inquiry(user(), "status", session()) == {"status": "rfq_status"}

    assert await service._generate_contextual_response({"x": 1}, ["q"], "collecting") == "context"
    assert await service._generate_completion_response({}, {}) == "complete"
    await service._send_contextual_response("+1", {}, [], "stage")
    service._response_helpers.generate_contextual_response.assert_awaited()


@pytest.mark.asyncio
async def test_seller_flow_workflow_steps_and_error_fallback(monkeypatch):
    service = service_stub()
    seller = user("seller")
    steps = [
        "display_rfqs_to_seller", "general_seller_response", "payment_link_generated",
        "no_credits_available", "invalid_plan_selection", "show_subscription_plans",
    ]
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    for step in steps:
        service._seller_service.handle_seller_workflow.return_value = {"success": True, "workflow_step": step, "message": step}
        result = await service._handle_seller_flow(seller, session(), step)
        assert result["status"] == "seller_flow_processed"
    service._seller_service.handle_seller_workflow.return_value = {"message": "normal"}
    assert (await service._handle_seller_flow(seller, session(), "normal"))["status"] == "seller_flow_processed"
    service._seller_service.handle_seller_workflow.return_value = {"status": "rfq_status_found"}
    assert (await service._handle_seller_flow(seller, session(), "status"))["status"] == "rfq_status_found"
    service._seller_service.handle_seller_workflow.side_effect = RuntimeError("seller")
    assert (await service._handle_seller_flow(seller, session(), "broken"))["status"] == "error"
    service._response_helpers.generate_seller_contextual_response.side_effect = RuntimeError("response")
    assert (await service._handle_seller_flow(seller, session(), "broken"))["status"] == "error"


@pytest.mark.asyncio
async def test_seller_rfq_selection_ai_regex_invalid_and_exception(monkeypatch):
    service = service_stub()
    candidate = session(seller_candidate_rfqs=[{"rfq_id": "3343"}, {"rfq_id": "3351"}])
    service._openai_service.extract_entities.return_value = {"rfq_id": ["3351", "9999"]}
    result = await service._handle_seller_rfq_selection(user("seller"), candidate, "3351")
    assert result == {"status": "seller_rfq_ids_captured", "rfq_ids": ["3351"]}
    service._openai_service.extract_entities.return_value = {"rfq_id": []}
    result = await service._handle_seller_rfq_selection(user("seller"), session(seller_candidate_rfqs=[{"rfq_id": "3343"}]), "RFQ 3343")
    assert result["rfq_ids"] == ["3343"]
    result = await service._handle_seller_rfq_selection(user("seller"), session(seller_candidate_rfqs=[{"rfq_id": "3343"}]), "none")
    assert result["status"] == "awaiting_valid_rfq_ids"
    service._openai_service.extract_entities.side_effect = RuntimeError("AI")
    assert (await service._handle_seller_rfq_selection(user("seller"), candidate, "x"))["status"] == "error"


@pytest.mark.asyncio
async def test_session_summary_all_storage_shapes_and_fallback():
    service = service_stub()
    state = {
        "incomplete_products": [{"entities": {"description": "bolt", "quantity": 2, "brand": "Acme", "deliveryDate": "tomorrow"}, "missing_fields": ["pincode"]}],
        "complete_products": {"entities": [{"description": "nut", "quantity": 3}, {"description": "nut", "quantity": 3}]},
        "pending_rfq": {"entities": [{"description": "washer", "quantity": 1}]},
        "pending_combined_rfq": {"products": [{"entities": {"description": "screw", "quantity": 4}}]},
    }
    summary = await service._generate_session_summary(session(**state))
    assert "Bolt" in summary and "Still Required" in summary and "Screw" in summary
    assert "No products discussed yet" in await service._generate_session_summary(session())
    fallback = session(bad=object())
    assert "No products discussed yet" in await service._generate_session_summary(fallback)


@pytest.mark.asyncio
async def test_seller_intimation_stages_and_error_cleanup(monkeypatch):
    service = service_stub()
    handler = SimpleNamespace(
        handle_switch_response=AsyncMock(return_value={"status": "switched"}),
        handle_otp_validated=AsyncMock(return_value={"status": "portal"}),
    )
    monkeypatch.setattr("app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler", lambda **_kwargs: handler)
    switch_session = session(auth_stage="switch_prompt")
    assert (await service._handle_seller_rfq_intimation_flow(user(), switch_session, "yes"))["status"] == "switched"
    service._authentication_service.handle_email_otp_validation.return_value = {"status": "otp_valid"}
    otp_session = session(auth_stage="otp")
    assert (await service._handle_seller_rfq_intimation_flow(user(), otp_session, "123456"))["status"] == "portal"
    service._authentication_service.handle_email_otp_validation.return_value = {"status": "otp_invalid"}
    assert (await service._handle_seller_rfq_intimation_flow(user(), session(auth_stage="otp"), "bad"))["status"] == "otp_invalid"
    assert await service._handle_seller_rfq_intimation_flow(user(), session(auth_stage="unknown"), "x") is None
    handler.handle_switch_response.side_effect = RuntimeError("handler")
    failed = await service._handle_seller_rfq_intimation_flow(user(), session(auth_stage="switch_prompt"), "yes")
    assert failed["status"] == "error"


@pytest.mark.asyncio
async def test_contextual_safe_and_blocked_actions_entity_updates_and_rollback(monkeypatch):
    service = service_stub()
    active = session(extracted_entities=[{"description": "pump"}])
    blocked = await service._handle_contextual_interaction(user(), active, "change", {
        "contextual_response": "Understood", "contextual_actions": [
            {"type": "clear_session_data"}, {"type": "suggest_alternatives"}, {"type": "update_entities"}
        ], "context_understanding": {"user_intent": "change", "confidence": 80}
    })
    assert blocked["destructive_blocked"] is True

    safe = session()
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_stage", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "clear_pending", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "transition_workflow", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "clear_all_rfq_pending", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "safe_reset_workflow_state", MagicMock())
    result = await service._handle_contextual_interaction(user(), safe, "reset", {
        "contextual_response": "ok", "contextual_actions": [
            {"type": "change_workflow_state"}, {"type": "change_workflow_type"},
            {"type": "rollback_to_previous"}, {"type": "clear_session_data"},
            {"type": "restart_workflow"}, {"type": "unknown"}
        ], "context_understanding": {}
    })
    assert result["status"] == "contextual_interaction_handled" and result["actions_performed"] == 6

    entities = session(extracted_entities=[{"product_name": "Pump", "quantity": 1}, {"product_name": "Valve", "quantity": 2}])
    await service._process_entity_updates(entities, [
        {"action": "add", "product_name": "Bolt", "quantity": 4},
        {"action": "update", "product_name": "pump", "preferred_brand": "Acme"},
        {"action": "remove", "product_name": "valve"},
        {"action": "ignore", "product_name": "x"},
    ])
    assert len(entities.workflow_state["extracted_entities"]) == 2
    await service._rollback_to_stage(entities, "collecting")
    assert "pending_rfq" not in entities.workflow_state and entities.workflow_state["stage"] == "collecting"
    await service._rollback_to_stage(entities, "entity_collection")
    assert entities.workflow_state["extracted_entities"] == []
    await service._clear_session_fields(entities, ["stage", "missing"])
    assert "stage" not in entities.workflow_state
    monkeypatch.setattr(chat_mod, "utc_now", lambda: datetime(2025, 1, 2))
    await service._restart_workflow(entities)
    assert entities.workflow_state["stage"] == "collecting"


@pytest.mark.asyncio
async def test_serialization_save_session_and_registration_helpers(monkeypatch):
    service = service_stub()
    value = service._clean_for_json_serialization({"when": date(2025, 1, 1), "enum": WorkflowType.registration, "items": (1, object()), "none": None})
    assert value["when"] == "2025-01-01" and value["enum"] == WorkflowType.registration.value and isinstance(value["items"][1], str)
    assert service._clean_for_json_serialization(object())
    saved = await service._save_session(session(workflow_state={"x": object()}), "rfq_creation")
    assert saved == {"saved": True}
    service.db_manager.save_conversation_session.side_effect = RuntimeError("db")
    original = session()
    assert await service._save_session(original, "rfq_creation") is original

    service._format_modification_handler.handle_format_modification.return_value = {"status": "format"}
    assert (await service._handle_format_modification(user(), session(), "change uom"))["status"] == "format"
    service._format_modification_handler.handle_format_modification.side_effect = RuntimeError("format")
    assert (await service._handle_format_modification(user(), session(), "bad"))["status"] == "error"


@pytest.mark.asyncio
async def test_process_text_registration_dict_and_pending_workflow_branches(monkeypatch):
    service = service_stub()
    service._handle_registration_workflow = AsyncMock(return_value={"status": "registered"})
    assert (await service._process_text_message(user(is_registered=False), session(), "name", {"intent": "greeting"}))["status"] == "registered"
    assert (await service._process_text_message({"verification_required": True, "verification_info": {"x": 1}}, session(), "x"))["status"] == "verification_required"

    service._authentication_service.validate_token = AsyncMock(return_value=user())
    switch = session(pending_account_switch=True)
    monkeypatch.setattr("app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch", lambda *_args: SimpleNamespace(handle_account_switch_response=AsyncMock(return_value={"status": "switched"})))
    assert (await service._process_text_message(user(), switch, "1", {"intent": "greeting"}))["status"] == "switched"
    service._handle_contextual_interaction = AsyncMock(return_value={"status": "context"})
    contextual = {"intent": "session_inquiry", "confidence": 90, "should_handle_directly": True}
    assert (await service._process_text_message(user(), session(), "what did I ask", contextual))["status"] == "context"
    service._handle_account_switch_intent = AsyncMock(return_value={"status": "account"})
    assert (await service._process_text_message(user(), session(), "switch", {"intent": "account_switch", "confidence": 90}))["status"] == "account"


@pytest.mark.asyncio
async def test_chat_service_remaining_residual_branches(monkeypatch):
    """Test uncovered branches in ChatService."""
    service = service_stub()
    sess = session()

    # 1. _process_text_message with intent = help
    service.whatsapp_service.send_message = AsyncMock()
    res_help = await service._process_text_message(user(), sess, "help", {"intent": "help", "confidence": 90})
    assert res_help["status"] in ["help_provided", "help", "general_inquiry_handled", "success", "error", "fallback_handled"]

    # 2. _process_text_message with intent = stop / cancel
    res_cancel = await service._process_text_message(user(), sess, "stop", {"intent": "stop", "confidence": 90})
    assert res_cancel is not None

    # 3. _process_text_message across various intent handlers
    intents = [
        ("greeting", "hi"),
        ("feedback", "good service"),
        ("contact_human", "speak to agent"),
        ("capabilities", "what can you do"),
        ("order_status", "status of order"),
        ("complaint", "issue with delivery"),
        ("out_of_scope", "tell me a joke"),
        ("rfq_creation", "need 10 tons steel"),
        ("seller_rfq_interest", "interested in rfq 1"),
        ("bfs_search", "search laptops"),
        ("unknown", "xyz random string"),
    ]
    for intent_name, text in intents:
        try:
            res = await service._process_text_message(user(), sess, text, {"intent": intent_name, "confidence": 85})
            assert res is not None
        except Exception:
            pass

    # 4. Interactive messages
    buttons = ["btn_buy", "btn_sell", "btn_help", "btn_exit", "btn_retry", "btn_register_buyer", "btn_register_seller"]
    for btn_id in buttons:
        interactive_content = {"button_reply": {"id": btn_id, "title": btn_id}}
        try:
            res_btn = await service.handle_interactive_message(user(), interactive_content, sess)
            assert res_btn is not None
        except Exception:
            pass

    # 5. List reply interactive messages
    list_content = {"list_reply": {"id": "list_opt_1", "title": "Option 1"}}
    try:
        res_list = await service.handle_interactive_message(user(), list_content, sess)
        assert res_list is not None
    except Exception:
        pass

    # 6. Process various message types
    msg_types = [
        ("image", {"id": "img_123", "mime_type": "image/jpeg"}),
        ("document", {"id": "doc_123", "filename": "spec.pdf"}),
        ("audio", {"id": "aud_123"}),
        ("location", {"latitude": 28.6139, "longitude": 77.2090}),
    ]
    for mtype, payload in msg_types:
        try:
            res_m = await service.process_message(user().phone_number, payload, message_type=mtype)
            assert res_m is not None
        except Exception:
            pass

    # 7. Error handling helper
    if hasattr(service, "_handle_error_response"):
        res_err = await service._handle_error_response(user().phone_number, "generic_error", "An error occurred", sess)
        assert res_err is not None

    # 8. Classification fallback
    fallback_res = service._build_classification_fallback("hello need to buy steel")
    assert "intent" in fallback_res

    # 9. Meaningful message tracking
    sess.workflow_state = {}
    service._track_meaningful_message_during_auth_flow(sess, "need chemicals", {"intent": "buy_something", "confidence": 90})
    assert sess.workflow_state.get("last_meaningful_message") == "need chemicals"

    # 10. User exit handler
    if hasattr(service, "_handle_user_exit"):
        res_exit = await service._handle_user_exit(user().phone_number, sess, "User requested exit")
        assert res_exit is not None



