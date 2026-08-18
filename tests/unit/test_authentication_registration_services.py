"""Deterministic unit coverage for authentication and registration workflows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.authentication_service as auth_module
import app.services.registration_service as registration_module
from app.models import ConversationOutcome, UserType, WorkflowType
from app.schemas.user import User
from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService


PHONE = "+919876543210"
EMAIL = "buyer@example.com"


def make_session(state=None):
    return SimpleNamespace(
        session_id="session-1",
        workflow_state=dict(state or {}),
        workflow_type=None,
        user_type=UserType.unknown,
        outcome=None,
        conversation_history={"messages": [], "metadata": []},
        extracted_entities={},
        completed_at=None,
    )


def buyer_user_dict(**overrides):
    data = {
        "id": "buyer-1",
        "username": EMAIL,
        "fullName": "Buyer User",
        "selfClient": True,
        "companyName": "Buyer Co",
        "approved": True,
    }
    data.update(overrides)
    return data


def seller_user_dict(**overrides):
    data = buyer_user_dict(
        id="seller-1",
        username="seller@example.com",
        fullName="Seller User",
        selfClient=False,
        approved=False,
    )
    data.update(overrides)
    return data


def valid_buyer_entities():
    return {
        "name": "Buyer User",
        "companyName": "Buyer Co",
        "email": EMAIL,
        "zipCode": "110001",
    }


def valid_seller_entities():
    return {
        "name": "Seller User",
        "companyName": "Seller Co",
        "email": "seller@example.com",
        "address1": "Delhi",
        "zipCode": "110001",
        "gstin": "27ABCDE1234F1Z5",
        "details": "industrial pumps",
    }


@pytest.fixture
def auth_dependencies():
    whatsapp = MagicMock()
    whatsapp.send_message = AsyncMock()
    whatsapp.send_configurable_buttons = AsyncMock(
        return_value=SimpleNamespace(success=True)
    )
    openai = MagicMock()
    openai.generate_response.return_value = "generated response"
    response_helpers = MagicMock()
    redis = MagicMock()
    redis.retrieve = AsyncMock()
    redis.store = AsyncMock()
    redis.delete_auth = AsyncMock()
    cache = MagicMock()
    cache.get_user_data = AsyncMock()
    cache.store_user_data = AsyncMock()
    cache.refresh_cache_expiry = AsyncMock()
    cache.clear_user_data = AsyncMock()
    auth_api = MagicMock()
    auth_api.authenticate_user = AsyncMock()
    register_api = MagicMock()
    register_api.register_buyer = AsyncMock()
    register_api.register_seller = AsyncMock()
    support = MagicMock()
    for name in (
        "notify_registration_failed",
        "notify_buyer_registration_not_approved",
        "notify_api_service_failure",
        "notify_otp_validation_failed",
    ):
        setattr(support, name, AsyncMock())
    domain = MagicMock()
    domain.check_domain_match = AsyncMock()
    domain.process_user_approval = AsyncMock()
    otp = MagicMock()
    otp.send_otp = AsyncMock()
    otp.handle_user_message = AsyncMock()
    verification = MagicMock()
    verification.check_and_enforce_verification = AsyncMock()
    verification._check_domain_approval = AsyncMock()
    session_manager = MagicMock()
    session_manager.send_and_track_message = AsyncMock()
    session_manager.save_session = AsyncMock()
    return SimpleNamespace(
        whatsapp=whatsapp,
        openai=openai,
        response_helpers=response_helpers,
        redis=redis,
        cache=cache,
        auth_api=auth_api,
        register_api=register_api,
        support=support,
        domain=domain,
        otp=otp,
        verification=verification,
        session_manager=session_manager,
    )


@pytest.fixture
def auth_service(auth_dependencies):
    d = auth_dependencies
    with (
        patch.object(auth_module, "get_auth_redis_service", return_value=d.redis),
        patch.object(auth_module, "AuthAPIService", return_value=d.auth_api),
        patch.object(auth_module, "RegisterAPIService", return_value=d.register_api),
        patch.object(auth_module, "SupportNotificationService", return_value=d.support),
        patch.object(auth_module, "get_user_cache_service", return_value=d.cache),
        patch.object(auth_module, "DomainCheckService", return_value=d.domain),
        patch.object(auth_module, "OTPService", return_value=d.otp),
        patch.object(auth_module, "VerificationCheckService", return_value=d.verification),
    ):
        yield AuthenticationService(
            whatsapp_service=d.whatsapp,
            openai_service=d.openai,
            response_helpers=d.response_helpers,
            session_manager=d.session_manager,
        )


@pytest.fixture
def registration_service(auth_dependencies):
    d = auth_dependencies
    settings = SimpleNamespace(
        support_contact_info="support@example.com",
        procucev_rfq_details_url="https://example.test/profile",
        categorization_init_timeout_seconds=20.0,
        categorization_item_timeout_seconds=15.0,
    )
    entity = MagicMock()
    entity.extract_entities = AsyncMock()
    confirmation = MagicMock()
    confirmation.parse_confirmation = AsyncMock()
    with (
        patch.object(registration_module, "RegisterAPIService", return_value=d.register_api),
        patch.object(registration_module, "get_auth_redis_service", return_value=d.redis),
        patch.object(registration_module, "SupportNotificationService", return_value=d.support),
        patch.object(registration_module, "DomainCheckService", return_value=d.domain),
        patch.object(registration_module, "OTPService", return_value=d.otp),
        patch.object(registration_module, "get_settings", return_value=settings),
        patch("app.procucev_apis.auth_apis.AuthAPIService", return_value=d.auth_api),
    ):
        service = RegistrationService(
            whatsapp_service=d.whatsapp,
            openai_service=d.openai,
            entity_service=entity,
            response_helpers=d.response_helpers,
            confirmation_service=confirmation,
            session_manager=d.session_manager,
        )
        service._test_entity = entity
        yield service


class TestAuthenticationConstructor:
    def test_constructor_wires_all_collaborators_and_defaults(self, auth_dependencies):
        d = auth_dependencies
        with (
            patch.object(auth_module, "WhatsAppService", return_value=d.whatsapp) as wa,
            patch.object(auth_module, "OpenAIService", return_value=d.openai) as ai,
            patch.object(auth_module, "ResponseHelpers", return_value=d.response_helpers),
            patch.object(auth_module, "get_auth_redis_service", return_value=d.redis),
            patch.object(auth_module, "AuthAPIService", return_value=d.auth_api),
            patch.object(auth_module, "RegisterAPIService", return_value=d.register_api),
            patch.object(auth_module, "SupportNotificationService", return_value=d.support),
            patch.object(auth_module, "get_user_cache_service", return_value=d.cache),
            patch.object(auth_module, "DomainCheckService", return_value=d.domain),
            patch.object(auth_module, "OTPService", return_value=d.otp),
            patch.object(auth_module, "VerificationCheckService", return_value=d.verification),
        ):
            service = AuthenticationService(session_manager=d.session_manager)
        assert service.whatsapp_service is d.whatsapp
        assert service.openai_service is d.openai
        assert service.auth_redis_service is d.redis
        assert service.session_manager is d.session_manager
        wa.assert_called_once()
        ai.assert_called_once()


class TestAuthenticationPublicMethods:
    @pytest.mark.asyncio
    async def test_validate_token_success_normalizes_and_sets_context(self, auth_service, auth_dependencies):
        auth_dependencies.redis.retrieve.return_value = buyer_user_dict()
        with patch.object(auth_module.user_context, "set") as set_context:
            result = await auth_service.validate_token(PHONE)
        assert result["id"] == "buyer-1"
        auth_dependencies.redis.retrieve.assert_awaited_once_with("919876543210")
        set_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_token_missing_and_exception_return_false(self, auth_service, auth_dependencies):
        auth_dependencies.redis.retrieve.return_value = None
        assert await auth_service.validate_token(PHONE) is False
        auth_dependencies.redis.retrieve.side_effect = RuntimeError("redis")
        assert await auth_service.validate_token(PHONE) is False

    @pytest.mark.asyncio
    async def test_store_user_session_success_refreshes_cache(self, auth_service, auth_dependencies):
        auth_dependencies.redis.store.return_value = True
        user = User(id="u1", name="User", email=EMAIL, self_client=True)
        assert await auth_service.store_user_session(PHONE, user) is True
        auth_dependencies.redis.store.assert_awaited_once()
        assert auth_dependencies.redis.store.await_args.args[0] == "919876543210"
        assert auth_dependencies.redis.store.await_args.kwargs["expiry_seconds"] == 43200
        auth_dependencies.cache.refresh_cache_expiry.assert_awaited_once_with("919876543210", 43200)

    @pytest.mark.asyncio
    async def test_store_user_session_false_and_exception(self, auth_service, auth_dependencies):
        user = User(id="u1")
        auth_dependencies.redis.store.return_value = False
        assert await auth_service.store_user_session("123", user) is False
        auth_dependencies.redis.store.side_effect = RuntimeError("store")
        assert await auth_service.store_user_session("123", user) is False

    @pytest.mark.asyncio
    async def test_clear_user_token_success_false_and_exception(self, auth_service, auth_dependencies):
        auth_dependencies.redis.delete_auth.return_value = True
        auth_dependencies.cache.clear_user_data.return_value = True
        assert await auth_service.clear_user_token(PHONE, False) is True
        auth_dependencies.redis.delete_auth.assert_awaited_with("919876543210")
        auth_dependencies.cache.clear_user_data.assert_awaited_with(PHONE, False)
        auth_dependencies.redis.delete_auth.return_value = False
        assert await auth_service.clear_user_token(PHONE) is False
        auth_dependencies.redis.delete_auth.side_effect = RuntimeError("delete")
        assert await auth_service.clear_user_token(PHONE) is False

    @pytest.mark.asyncio
    async def test_user_authenticate_cache_api_empty_failure_and_exception(self, auth_service, auth_dependencies):
        auth_dependencies.cache.get_user_data.return_value = [{"id": "cached"}]
        cached = await auth_service.user_authenticate(PHONE, "message", make_session(), "buy")
        assert cached["from_cache"] is True
        auth_dependencies.cache.get_user_data.return_value = None
        auth_dependencies.auth_api.authenticate_user.return_value = {
            "success": True, "data": [{"id": "api"}], "is_registered": False
        }
        result = await auth_service.user_authenticate(PHONE, "message", make_session())
        assert result == {
            "success": True, "response": [{"id": "api"}], "is_registered": False,
            "detected_intent": None,
        }
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": True, "data": []}
        assert (await auth_service.user_authenticate(PHONE, "m", make_session()))["message"] == "User details not found"
        failure = {"success": False, "message": "not found"}
        auth_dependencies.auth_api.authenticate_user.return_value = failure
        assert await auth_service.user_authenticate(PHONE, "m", make_session()) == failure
        auth_dependencies.auth_api.authenticate_user.side_effect = RuntimeError("api")
        assert (await auth_service.user_authenticate(PHONE, "m", make_session()))["message"] == "api"

    def test_filter_users_by_intent_success_no_match_and_exception(self, auth_service):
        users = [buyer_user_dict(), seller_user_dict(), buyer_user_dict(id="buyer-2", username="other@example.com")]
        buying = auth_service.filter_users_by_intent(users, "buy_something")
        assert buying["success"] and buying["count"] == 2
        assert buying["unique_emails"] == [EMAIL, "other@example.com"]
        selling = auth_service.filter_users_by_intent(users, "sell_something")
        assert selling["count"] == 1
        assert auth_service.filter_users_by_intent(users, "general_inquiry")["count"] == 3
        assert auth_service.filter_users_by_intent(users, "sell_something") ["filtered_users"][0]["id"] == "seller-1"
        assert auth_service.filter_users_by_intent(users, "buy_something") ["success"] is True
        assert auth_service.filter_users_by_intent([seller_user_dict()], "buy_something")["success"] is False
        with patch("app.schemas.user.APIUserSchema", side_effect=ValueError("schema")):
            result = auth_service.filter_users_by_intent(users, "buy_something")
        assert result["success"] is False and "schema" in result["message"]

    def test_create_user_details_exact_substring_missing_and_exception(self, auth_service):
        users = [buyer_user_dict(), seller_user_dict()]
        assert auth_service.create_user_details_from_email(users, EMAIL).id == "buyer-1"
        assert auth_service.create_user_details_from_email(users, "buyer@example").id == "buyer-1"
        assert auth_service.create_user_details_from_email(users, "missing@example") is None
        with patch.object(auth_module.User, "from_mixed_data", side_effect=ValueError("bad")):
            assert auth_service.create_user_details_from_email(users, EMAIL) is None

    @pytest.mark.asyncio
    async def test_initiate_email_confirmation_no_one_many_and_exception(self, auth_service):
        session = make_session()
        assert await auth_service.initiate_email_confirmation(PHONE, session, [], []) == {"status": "redirect_to_registration"}
        auth_service._process_selected_email = AsyncMock(return_value={"status": "processed"})
        result = await auth_service.initiate_email_confirmation(PHONE, session, [buyer_user_dict()], [EMAIL])
        assert result["status"] == "processed"
        auth_service._request_email_selection_with_text = AsyncMock(return_value={"status": "requested"})
        result = await auth_service.initiate_email_confirmation(PHONE, session, [buyer_user_dict(), seller_user_dict()], [EMAIL, "seller@example.com"])
        assert result["status"] == "requested"
        assert session.workflow_state["confirmation_stage"] == "selection"
        auth_service._request_email_selection_with_text.side_effect = RuntimeError("selection")
        assert (await auth_service.initiate_email_confirmation(PHONE, session, [buyer_user_dict()], [EMAIL, "x@y.com"]))["status"] == "error"

    @pytest.mark.asyncio
    async def test_handle_email_confirmation_all_stages(self, auth_service):
        assert await auth_service.handle_email_confirmation(PHONE, "x", make_session()) == {"status": "restart_authentication"}
        session = make_session({
            "email_options": [EMAIL, "seller@example.com"],
            "filtered_users": [buyer_user_dict(), seller_user_dict()],
            "confirmation_stage": "selection",
            "intent_result": {"intent": "general_inquiry", "confidence": 90},
        })
        profile = MagicMock()
        profile.handle_profile_selection_response = AsyncMock(return_value={"status": "no_selection"})
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            result = await auth_service.handle_email_confirmation(PHONE, "2", session)
        assert result["status"] == "no_selection"
        session.workflow_state["confirmation_stage"] = "intent_clarification"
        auth_service._handle_intent_clarification_response = AsyncMock(return_value={"status": "clarified"})
        assert (await auth_service.handle_email_confirmation(PHONE, "buy", session))["status"] == "clarified"
        session.workflow_state["confirmation_stage"] = "confirmation"
        session.workflow_state["selected_email"] = EMAIL
        auth_service._ai_validate_confirmation_response = AsyncMock(return_value=True)
        auth_service._process_selected_email = AsyncMock(return_value={"status": "authenticated"})
        assert (await auth_service.handle_email_confirmation(PHONE, "yes", session))["status"] == "authenticated"
        auth_service._ai_validate_confirmation_response.return_value = False
        auth_service._request_email_selection_with_text = AsyncMock(return_value={"status": "again"})
        result = await auth_service.handle_email_confirmation(PHONE, "no", session)
        assert result["status"] == "again"
        session.workflow_state["confirmation_stage"] = "confirmation"
        auth_service._ai_validate_confirmation_response.side_effect = RuntimeError("ai")
        assert (await auth_service.handle_email_confirmation(PHONE, "x", session))["status"] == "error"

    @pytest.mark.asyncio
    async def test_handle_email_confirmation_intent_refinement_single_and_multiple(self, auth_service):
        base = {"email_options": [EMAIL, "seller@example.com"], "filtered_users": [buyer_user_dict(), seller_user_dict()], "confirmation_stage": "selection"}
        session = make_session(base)
        auth_service.filter_users_by_intent = MagicMock(return_value={"success": True, "unique_emails": [EMAIL], "filtered_users": [buyer_user_dict()]})
        auth_service._process_selected_email = AsyncMock(return_value={"status": "auto"})
        result = await auth_service.handle_email_confirmation(PHONE, "x", session, {"intent": "buy_something", "confidence": 90})
        assert result["status"] == "auto"
        session = make_session(base)
        auth_service.filter_users_by_intent.return_value = {"success": True, "unique_emails": [EMAIL, "other@example.com"], "filtered_users": [buyer_user_dict(), buyer_user_dict(id="2", username="other@example.com")]}
        auth_service._request_email_selection_with_text = AsyncMock(return_value={"status": "refined"})
        assert (await auth_service.handle_email_confirmation(PHONE, "x", session, {"intent": "buy_something", "confidence": 90}))["status"] == "refined"

    @pytest.mark.asyncio
    async def test_store_session_with_email_success_missing_and_exception(self, auth_service):
        auth_service.store_user_session = AsyncMock(return_value=True)
        assert await auth_service.store_user_session_with_email(PHONE, [buyer_user_dict()], EMAIL) is True
        auth_service.store_user_session = AsyncMock(side_effect=RuntimeError("store"))
        assert await auth_service.store_user_session_with_email(PHONE, [buyer_user_dict()], EMAIL) is False
        assert await auth_service.store_user_session_with_email(PHONE, [], EMAIL) is False

    @pytest.mark.asyncio
    async def test_handle_email_otp_validation_all_outcomes(self, auth_service, auth_dependencies):
        assert await auth_service.handle_email_otp_validation(PHONE, "1234", make_session()) == {"status": "restart_authentication"}
        session = make_session({"filtered_users": [buyer_user_dict()]})
        auth_dependencies.otp.handle_user_message.return_value = {"status": "max_otp_exceeded"}
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await auth_service.handle_email_otp_validation(PHONE, "x", session))["exit_completed"] is True
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_invalid"}
        assert (await auth_service.handle_email_otp_validation(PHONE, "x", session))["status"] == "otp_invalid"
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_valid"}
        session.workflow_state.update({"otp_email": EMAIL, "selected_user": buyer_user_dict()})
        auth_service._store_verified_user_session = AsyncMock(return_value=True)
        assert (await auth_service.handle_email_otp_validation(PHONE, "1234", session))["status"] == "authentication_completed"
        session = make_session({"filtered_users": [buyer_user_dict()], "otp_email": EMAIL, "selected_user": buyer_user_dict(approved=False, id="b2")})
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_valid"}
        auth_dependencies.domain.check_domain_match.return_value = {"match": True}
        auth_dependencies.domain.process_user_approval.return_value = {"status": "approved_and_updated"}
        assert (await auth_service.handle_email_otp_validation(PHONE, "1234", session))["approved"] is True
        auth_dependencies.domain.process_user_approval.return_value = {"status": "pending"}
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            result = await auth_service.handle_email_otp_validation(PHONE, "1234", session)
        assert result["status"] == "redirected_to_support"
        session = make_session({"filtered_users": [buyer_user_dict()], "otp_email": EMAIL, "selected_user": buyer_user_dict(id=None, approved=False)})
        assert (await auth_service.handle_email_otp_validation(PHONE, "1234", session))["error"] == "User ID missing for domain check"
        session = make_session({"filtered_users": [seller_user_dict()], "otp_email": "seller@example.com", "selected_user": seller_user_dict()})
        auth_service._store_verified_user_session = AsyncMock(return_value=True)
        assert (await auth_service.handle_email_otp_validation(PHONE, "1234", session, "i", "m"))["user_type"] == "seller"
        session = make_session({"filtered_users": [buyer_user_dict()]})
        auth_dependencies.otp.handle_user_message.side_effect = RuntimeError("otp")
        assert (await auth_service.handle_email_otp_validation(PHONE, "x", session))["status"] == "error"

    @pytest.mark.asyncio
    async def test_handle_domain_matching_approved_mismatch_missing_and_exception(self, auth_service):
        assert await auth_service.handle_domain_matching(PHONE, "x", make_session()) == {"status": "restart_authentication"}
        session = make_session({"selected_user_details": {"id": "u"}, "selected_email": EMAIL})
        auth_service._check_domain_approval = AsyncMock(return_value={"approved": True})
        auth_service._complete_buyer_authentication = AsyncMock(return_value={"status": "done"})
        assert (await auth_service.handle_domain_matching(PHONE, "x", session))["status"] == "done"
        auth_service._check_domain_approval.return_value = {"approved": False}
        auth_service._handle_domain_mismatch = AsyncMock(return_value={"status": "redirected"})
        assert (await auth_service.handle_domain_matching(PHONE, "x", session))["status"] == "redirected"
        session.workflow_state["selected_user_details"] = {"id": None}
        assert (await auth_service.handle_domain_matching(PHONE, "x", session))["error"] == "User ID not found"
        auth_service._check_domain_approval.side_effect = RuntimeError("domain")
        session.workflow_state["selected_user_details"] = {"id": "u"}
        assert (await auth_service.handle_domain_matching(PHONE, "x", session))["status"] == "error"


class TestAuthenticationHelpersAndPrivateBranches:
    def test_small_sync_helpers(self, auth_service):
        assert auth_service._determine_user_type({"selfClient": True}) == "buyer"
        assert auth_service._determine_user_type({"selfClient": False}) == "seller"
        assert auth_service._extract_user_details([]) is None
        assert auth_service._extract_user_details([buyer_user_dict()]).id == "buyer-1"
        assert auth_service._extract_emails_from_response([{"email": [EMAIL, "bad"]}, {"email": EMAIL}]) == [EMAIL]
        assert auth_service._get_user_type_for_email(EMAIL, [buyer_user_dict()]) == "Seller"
        assert auth_service._get_user_type_for_email(EMAIL, [{"email": EMAIL, "self_client": True}]) == "Buyer"
        assert auth_service._get_username_from_users([{"fullName": "  Alice Smith "}]) == "Alice Smith"
        assert auth_service._get_username_from_users([]) == "there"
        assert auth_service._get_username_from_users([{"fullName": ""}]) == "there"

    @pytest.mark.asyncio
    async def test_email_parsers_ai_and_fallbacks(self, auth_service, auth_dependencies):
        assert await auth_service._parse_email_selection("1", [EMAIL]) == EMAIL
        assert await auth_service._parse_email_selection("nope", [EMAIL]) is None
        assert await auth_service._parse_email_selection(EMAIL, [EMAIL]) == EMAIL
        auth_dependencies.openai.generate_response.return_value = f"Use {EMAIL}"
        assert await auth_service._validate_email_confirmation_with_ai("yes", [EMAIL]) == EMAIL
        auth_dependencies.openai.generate_response.return_value = "none"
        assert await auth_service._validate_email_confirmation_with_ai("x", [EMAIL]) is None
        auth_dependencies.openai.generate_response.side_effect = RuntimeError("ai")
        assert "Could you" in await auth_service._generate_email_confirmation_response("A", [EMAIL])
        auth_dependencies.openai.generate_response.side_effect = None
        auth_dependencies.openai.generate_response.return_value = "yes"
        assert await auth_service._validate_confirmation_with_ai("yes") is True
        assert await auth_service._validate_confirmation_response("yes") is True
        assert await auth_service._validate_confirmation_response("no") is False
        assert await auth_service._is_email_rejection("not my email") is True
        assert await auth_service._is_email_rejection("okay") is False
        auth_dependencies.openai.generate_response.return_value = EMAIL
        assert await auth_service._ai_parse_email_selection("x", [EMAIL]) == EMAIL
        auth_dependencies.openai.generate_response.return_value = "yes"
        assert await auth_service._ai_validate_confirmation_response("x") is True
        auth_dependencies.openai.generate_response.return_value = "no"
        assert await auth_service._ai_validate_confirmation_response("x") is False
        auth_dependencies.openai.generate_response.return_value = "maybe"
        assert await auth_service._ai_validate_confirmation_response("x") is None
        assert await auth_service._ai_detect_email_rejection("x") is False

    @pytest.mark.asyncio
    async def test_auth_private_message_and_session_helpers(self, auth_service, auth_dependencies):
        session = make_session({"filtered_users": [{"fullName": "A", "self_client": True, "email": EMAIL}]})
        result = await auth_service._request_email_confirmation(PHONE, session, EMAIL, session.workflow_state["filtered_users"])
        assert result["status"] == "email_confirmation_requested"
        result = await auth_service._request_email_selection(PHONE, session, [EMAIL])
        assert result["status"] == "email_selection_requested"
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await auth_service._request_email_selection_text_fallback(PHONE, session, [EMAIL], session.workflow_state["filtered_users"]))["status"] == "error"
        auth_dependencies.whatsapp.send_message.side_effect = None
        auth_dependencies.whatsapp.send_configurable_buttons.side_effect = RuntimeError("buttons")
        await auth_service._send_seller_menu_options(PHONE, "A")
        session.workflow_state["last_activity_at"] = "now"
        assert await auth_service._clear_session_only(session) is True
        assert session.outcome == ConversationOutcome.abandoned
        auth_dependencies.session_manager.save_session.side_effect = RuntimeError("save")
        assert await auth_service._clear_session_only(make_session()) is False

    @pytest.mark.asyncio
    async def test_auth_process_selected_buyer_seller_verification_branches(self, auth_service, auth_dependencies):
        buyer = buyer_user_dict(approved=False)
        session = make_session({"original_message": "buy"})
        auth_dependencies.verification.check_and_enforce_verification.return_value = {
            "access_granted": False, "redirect_info": {"flow": "email_verification"}
        }
        result = await auth_service._process_selected_email(PHONE, session, EMAIL, [buyer])
        assert result["status"] == "otp_sent"
        assert session.workflow_type == WorkflowType.authentication
        auth_dependencies.verification.check_and_enforce_verification.return_value = {"access_granted": True}
        auth_service.store_user_session_with_email = AsyncMock(return_value=True)
        assert (await auth_service._process_selected_email(PHONE, session, EMAIL, [buyer]))["status"] == "authentication_completed"
        seller = seller_user_dict()
        auth_dependencies.verification.check_and_enforce_verification.return_value = {
            "access_granted": False, "redirect_info": {}
        }
        auth_dependencies.otp.send_otp.return_value = {"status": "otp_sent"}
        result = await auth_service._process_selected_email(PHONE, make_session(), "seller@example.com", [seller])
        assert result["user_type"] == "seller"
        auth_dependencies.verification.check_and_enforce_verification.return_value = {
            "access_granted": False, "redirect_to_support": True, "redirect_info": {"reason": "blocked", "message": "help"}
        }
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            result = await auth_service._process_selected_email(PHONE, make_session(), EMAIL, [buyer])
        assert result["status"] == "redirected_to_support"
        assert (await auth_service._process_selected_email(PHONE, make_session(), "missing", [buyer]))["reason"] == "user_not_found"

    @pytest.mark.asyncio
    async def test_auth_domain_and_verified_session_helpers(self, auth_service, auth_dependencies):
        auth_dependencies.verification._check_domain_approval.return_value = {"approved": True}
        assert await auth_service._check_domain_approval("u") == {"approved": True}
        auth_dependencies.verification._check_domain_approval.side_effect = RuntimeError("api")
        assert (await auth_service._check_domain_approval("u"))["approved"] is False
        auth_service.store_user_session_with_email = AsyncMock(return_value=True)
        assert (await auth_service._complete_buyer_authentication(PHONE, buyer_user_dict(), EMAIL))["status"] == "authentication_completed"
        auth_service.store_user_session_with_email.side_effect = RuntimeError("store")
        assert (await auth_service._complete_buyer_authentication(PHONE, buyer_user_dict(), EMAIL))["status"] == "error"
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await auth_service._handle_domain_mismatch(PHONE, buyer_user_dict(), make_session()))["status"] == "redirect_to_support"
        auth_dependencies.redis.store.return_value = True
        with patch("app.services.user_cache_service.get_user_cache_service", return_value=auth_dependencies.cache):
            assert await auth_service._store_verified_user_session(PHONE, buyer_user_dict(), EMAIL) is True
        auth_dependencies.redis.store.side_effect = RuntimeError("redis")
        assert await auth_service._store_verified_user_session(PHONE, buyer_user_dict(), EMAIL) is False


class TestRegistrationConstructor:
    def test_constructor_wires_supplied_dependencies(self, auth_dependencies):
        d = auth_dependencies
        settings = SimpleNamespace(support_contact_info="s", procucev_rfq_details_url="u")
        with (
            patch.object(registration_module, "RegisterAPIService", return_value=d.register_api),
            patch.object(registration_module, "get_auth_redis_service", return_value=d.redis),
            patch.object(registration_module, "SupportNotificationService", return_value=d.support),
            patch.object(registration_module, "DomainCheckService", return_value=d.domain),
            patch.object(registration_module, "OTPService", return_value=d.otp),
            patch.object(registration_module, "get_settings", return_value=settings),
            patch("app.procucev_apis.auth_apis.AuthAPIService", return_value=d.auth_api),
        ):
            service = RegistrationService(d.whatsapp, d.openai, MagicMock(), d.response_helpers, None, d.session_manager)
        assert service.whatsapp_service is d.whatsapp
        assert service.confirmation_service is None
        assert service.settings is settings


class TestRegistrationPublicMethods:
    @pytest.mark.asyncio
    async def test_initiate_registration_buyer_seller_manager_and_error(self, registration_service, auth_dependencies):
        session = make_session()
        result = await registration_service.initiate_registration(PHONE, session, "buyer")
        assert result["status"] == "registration_initiated" and session.user_type == UserType.buyer
        result = await registration_service.initiate_registration(PHONE, session, "seller")
        assert result["user_type"] == "seller" and session.user_type == UserType.seller
        registration_service.session_manager = None
        assert (await registration_service.initiate_registration(PHONE, session, "buyer"))["status"] == "registration_initiated"
        registration_service.authentication_helpers.generate_registration_message = MagicMock(side_effect=RuntimeError("helper"))
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service.initiate_registration(PHONE, session, "buyer"))["status"] == "redirected"

    @pytest.mark.asyncio
    async def test_registration_data_collection_exit_missing_complete_and_error(self, registration_service, auth_dependencies):
        registration_service._check_exit_command = AsyncMock(return_value=True)
        registration_service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
        assert (await registration_service.handle_registration_data_collection(PHONE, "exit", make_session()))["status"] == "exit"
        registration_service._check_exit_command = AsyncMock(return_value=False)
        registration_service._test_entity.extract_entities.return_value = {"entities": {"name": "New User"}}
        registration_service.authentication_helpers.validate_entities = AsyncMock(return_value=({"name": "New User"}, None))
        with patch.object(registration_module.AuthenticationHelpers, "get_missing_fields", return_value=["email"]):
            result = await registration_service.handle_registration_data_collection(PHONE, "name", make_session())
        assert result["status"] == "data_collection_in_progress"
        entities = valid_buyer_entities()
        registration_service._test_entity.extract_entities.return_value = {"entities": entities}
        registration_service.authentication_helpers.validate_entities = AsyncMock(return_value=(entities, None))
        registration_service._send_confirmation_with_buttons = AsyncMock()
        with patch.object(
            registration_module,
            "get_location_from_pincode_async",
            new=AsyncMock(return_value={"city": "Delhi", "state": "DL"}),
        ):
            with patch.object(registration_module.AuthenticationHelpers, "get_missing_fields", return_value=[]):
                result = await registration_service.handle_registration_data_collection(PHONE, "all", make_session())
        assert result["status"] == "awaiting_confirmation"
        registration_service._test_entity.extract_entities.side_effect = RuntimeError("entity")
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service.handle_registration_data_collection(PHONE, "bad", make_session()))["status"] == "redirected"

    @pytest.mark.asyncio
    async def test_registration_confirmation_yes_no_unknown_exit(self, registration_service, auth_dependencies):
        session = make_session({"user_type": "buyer", "registration_entities": valid_buyer_entities()})
        registration_service._submit_registration = AsyncMock(return_value={"status": "registration_completed"})
        auth_dependencies.otp.send_otp.return_value = {"status": "otp_sent"}
        assert (await registration_service.handle_registration_confirmation(PHONE, "confirm", session))["status"] == "otp_sent"
        session = make_session({"user_type": "seller", "registration_entities": valid_seller_entities()})
        registration_service.confirmation_service.parse_confirmation.return_value = "no"
        assert (await registration_service.handle_registration_confirmation(PHONE, "no", session))["status"] == "registration_restarted"
        registration_service._send_clarification_with_buttons = AsyncMock()
        registration_service.confirmation_service.parse_confirmation.return_value = None
        assert (await registration_service.handle_registration_confirmation(PHONE, "maybe", session))["status"] == "awaiting_confirmation"
        registration_service._check_exit_command = AsyncMock(return_value=True)
        registration_service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
        assert (await registration_service.handle_registration_confirmation(PHONE, "exit", session))["status"] == "exit"
        registration_service._check_exit_command = AsyncMock(side_effect=RuntimeError("check"))
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service.handle_registration_confirmation(PHONE, "x", session))["status"] == "redirected"

    def test_parse_button_response_variants(self, registration_service):
        assert registration_service._parse_button_response({"button_reply": {"id": "confirm_registration"}}) == "yes"
        assert registration_service._parse_button_response("restart") == "no"
        assert registration_service._parse_button_response({"button_reply": {"id": "other"}}) is None
        assert registration_service._parse_button_response(4) is None

    @pytest.mark.asyncio
    async def test_registration_store_session_success_failure_and_fallback(self, registration_service, auth_dependencies):
        user = User(id="u1")
        auth_dependencies.redis.store.return_value = True
        assert await registration_service.store_user_session(PHONE, user) is True
        auth_dependencies.redis.store.return_value = False
        assert await registration_service.store_user_session(PHONE, user) is False
        auth_dependencies.redis.store.side_effect = RuntimeError("redis")
        assert await registration_service.store_user_session(PHONE, user) is False
        auth_dependencies.redis.store.side_effect = None
        auth_dependencies.redis.store.return_value = True
        assert await registration_service._store_user_session_after_registration(PHONE, {"name": "A", "email": EMAIL}, "buyer") is True
        auth_dependencies.redis.store.side_effect = RuntimeError("redis")
        assert await registration_service._store_user_session_after_registration(PHONE, {}, "buyer") is False

    @pytest.mark.asyncio
    async def test_submit_registration_buyer_seller_conflict_failure_exception(self, registration_service, auth_dependencies):
        session = make_session({"user_type": "buyer"})
        auth_dependencies.register_api.register_buyer.return_value = {"statusCode": "200", "data": {"userId": "u", "orgId": "o"}}
        entities = valid_buyer_entities()
        result = await registration_service._submit_registration(PHONE, session, entities, "buyer")
        assert result["status"] == "registration_completed" and entities["user_id"] == "u"
        auth_dependencies.openai.parse_seller_product_items = AsyncMock(return_value={"success": False, "items": []})
        auth_dependencies.register_api.register_seller.return_value = {"status": "Success", "type": {"id": "s"}}
        assert (await registration_service._submit_registration(PHONE, session, valid_seller_entities(), "seller"))["status"] == "registration_completed"
        auth_dependencies.register_api.register_buyer.return_value = {"status_code": 409, "message": "already exists"}
        registration_service.session_manager = None
        with patch("app.services.cancel_service.CancelService") as cancel_cls:
            cancel_cls.return_value._clear_workflow_state = AsyncMock()
            assert (await registration_service._submit_registration(PHONE, session, valid_buyer_entities(), "buyer"))["status"] == "user_already_exists"
        auth_dependencies.register_api.register_buyer.return_value = {"statusCode": "500", "message": "bad"}
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service._submit_registration(PHONE, session, valid_buyer_entities(), "buyer"))["status"] == "redirected"
        auth_dependencies.register_api.register_buyer.side_effect = RuntimeError("api")
        assert (await registration_service._submit_registration(PHONE, session, valid_buyer_entities(), "buyer"))["status"] == "redirected"

    @pytest.mark.asyncio
    async def test_registration_otp_fresh_buyer_seller_and_statuses(self, registration_service, auth_dependencies):
        entities = valid_buyer_entities() | {"user_id": "buyer-1"}
        session = make_session({"pending_registration_data": entities, "user_type": "buyer", "current_intent_result": {"intent": "buy_something"}})
        registration_service._check_exit_command = AsyncMock(return_value=False)
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_valid"}
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": True, "data": [buyer_user_dict()]}
        with patch("app.services.verification_check_service.VerificationCheckService") as verification_cls:
            verification_cls.return_value._check_domain_approval = AsyncMock(return_value={"approved": True})
            result = await registration_service.handle_registration_otp_validation(PHONE, "1234", session)
        assert result["status"] == "buyer_options_presented"
        session = make_session({"pending_registration_data": entities, "user_type": "buyer"})
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": True, "data": [buyer_user_dict()]}
        with patch("app.services.verification_check_service.VerificationCheckService") as verification_cls:
            verification_cls.return_value._check_domain_approval = AsyncMock(return_value={"approved": False})
            with patch("app.services.exit_service.ExitService") as exit_cls:
                exit_cls.return_value.handle_exit_intent = AsyncMock()
                assert (await registration_service.handle_registration_otp_validation(PHONE, "1234", session))["status"] == "redirect_to_support"
        session = make_session({"pending_registration_data": valid_buyer_entities(), "user_type": "buyer"})
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": False, "data": []}
        with patch("app.services.verification_check_service.VerificationCheckService") as verification_cls:
            verification_cls.return_value._check_domain_approval = AsyncMock()
            with patch("app.services.exit_service.ExitService") as exit_cls:
                exit_cls.return_value.handle_exit_intent = AsyncMock()
                assert (await registration_service.handle_registration_otp_validation(PHONE, "1234", session))["reason"] == "missing_user_id"
        seller_entities = valid_seller_entities() | {"user_id": "seller-1"}
        session = make_session({"pending_registration_data": seller_entities, "user_type": "seller", "current_intent_result": {"intent": "sell_something", "original_message": "sell"}})
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": True, "data": [seller_user_dict(id="seller-1")]}
        assert (await registration_service.handle_registration_otp_validation(PHONE, "1234", session))["user_type"] == "seller"
        auth_dependencies.otp.handle_user_message.return_value = {"status": "max_otp_exceeded"}
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock(return_value={"status": "exit"})
            assert (await registration_service.handle_registration_otp_validation(PHONE, "x", session))["status"] == "exit"
        auth_dependencies.otp.handle_user_message.return_value = {"status": "redirect_to_support", "reason": "bad"}
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service.handle_registration_otp_validation(PHONE, "x", session))["status"] == "redirected"
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_invalid"}
        assert (await registration_service.handle_registration_otp_validation(PHONE, "x", session))["status"] == "otp_invalid"


class TestRegistrationHelpersAndPrivateBranches:
    @pytest.mark.asyncio
    async def test_context_questions_confirmation_and_button_fallback(self, registration_service, auth_dependencies):
        session = make_session()
        session.conversation_history = {"messages": [{"content": " hello "}, {"content": 3}]}
        assert registration_service._build_registration_context(session, {"button_reply": {"id": "confirm"}}) == "hello confirm"
        assert await registration_service._generate_confirmation_message(valid_buyer_entities(), "buyer")
        auth_dependencies.whatsapp.send_configurable_buttons.return_value = SimpleNamespace(success=False)
        await registration_service._send_confirmation_with_buttons(PHONE, valid_buyer_entities(), "buyer", session)
        await registration_service._send_clarification_with_buttons(PHONE, session)
        registration_service.session_manager = None
        await registration_service._send_confirmation_with_buttons(PHONE, valid_buyer_entities(), "buyer", session)
        assert "organization email" in await registration_service._generate_contextual_registration_questions(["email"], "buyer", {}, "x")

    @pytest.mark.asyncio
    async def test_pincode_exit_redirect_and_categorization(self, registration_service, auth_dependencies):
        entities = {}
        await registration_service._auto_fill_address_from_pincode(entities, PHONE, make_session())
        entities = {"zipCode": "110001"}
        with patch.object(registration_module, "get_location_from_pincode_async", new=AsyncMock(return_value={"city": "Delhi", "state": "DL"})):
            await registration_service._auto_fill_address_from_pincode(entities, PHONE, make_session())
        assert entities["address1"] == "Delhi, DL"
        entities = {"zipCode": "000000"}
        with patch.object(registration_module, "get_location_from_pincode_async", new=AsyncMock(return_value=None)):
            await registration_service._auto_fill_address_from_pincode(entities, PHONE, make_session())
        assert "_pincode_error" in entities
        assert await registration_service._check_exit_command(" EXIT ") is True
        assert await registration_service._check_exit_command({"button_reply": {"id": "stop"}}) is True
        assert await registration_service._check_exit_command(4) is False
        registration_service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock(return_value={"status": "exit"})
            assert (await registration_service._handle_registration_exit(PHONE, make_session()))["status"] == "exit"
        auth_dependencies.openai.parse_seller_product_items = AsyncMock(return_value={"success": True, "items": ["p1", "p2"]})
        categorizer = MagicMock()
        categorizer.categorize_item = AsyncMock(side_effect=[{"success": True, "category": "A", "confidence_score": .9, "method": "m"}, {"success": False}])
        with patch(
            "app.services.auto_categorization_service.get_auto_categorization_service_async",
            new=AsyncMock(return_value=categorizer),
        ):
            result = await registration_service._categorize_seller_products("p1 and p2", PHONE, make_session())
        assert result == [{"category": "A", "division": ""}]
        auth_dependencies.openai.parse_seller_product_items.return_value = {"success": False, "items": []}
        assert await registration_service._categorize_seller_products("x", PHONE, make_session()) == []

    @pytest.mark.asyncio
    async def test_neutral_greeting_and_redirect(self, registration_service, auth_dependencies):
        profiles = [{"role": "buyer", "email": EMAIL, "name": "Alice Smith"}, {"role": "seller", "email": "s@example.com"}]
        result = await registration_service._handle_neutral_greeting(PHONE, profiles, make_session())
        assert result["status"] == "profile_selection_sent"
        assert registration_service._extract_user_name([{"name": "alice smith"}]) == "Alice"
        assert registration_service._extract_user_name([]) == "there"
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await registration_service._redirect_to_support(PHONE, "x", "bad", make_session()))["status"] == "redirected_to_support"
        registration_service.session_manager = None
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await registration_service._redirect_to_support(PHONE, "x", "bad", None))["status"] == "error"


class TestAdditionalAuthenticationBranches:
    def test_filter_and_extract_edge_branches(self, auth_service):
        duplicate = buyer_user_dict(id="buyer-2")
        assert auth_service.filter_users_by_intent([buyer_user_dict(), duplicate], "general_inquiry")["unique_emails"] == [EMAIL]
        with patch.object(auth_module.AuthenticationHelpers, "extract_user_details", return_value=None):
            assert auth_service._extract_user_details([buyer_user_dict()]) is None
        with patch.object(auth_module.AuthenticationHelpers, "extract_user_details", side_effect=RuntimeError("helper")):
            assert auth_service._extract_user_details([buyer_user_dict()]) is None
        with patch.object(auth_module.AuthenticationHelpers, "extract_emails_from_user_data", side_effect=RuntimeError("bad")):
            assert auth_service._extract_emails_from_response([{}]) == []
        assert auth_service._get_user_type_for_email("none", [{"email": "other"}]) is None
        with patch.object(auth_module.AuthenticationHelpers, "extract_user_details", side_effect=RuntimeError("bad")):
            assert auth_service._extract_user_details([buyer_user_dict()]) is None

    @pytest.mark.asyncio
    async def test_email_selection_and_refinement_fallback_branches(self, auth_service):
        session = make_session({
            "email_options": [EMAIL],
            "filtered_users": [buyer_user_dict()],
            "confirmation_stage": "selection",
        })
        profile = MagicMock()
        profile.handle_profile_selection_response = AsyncMock(return_value={"status": "profile_selected_and_authenticated", "email": EMAIL})
        auth_service._process_selected_email = AsyncMock(return_value={"status": "selected"})
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            assert (await auth_service.handle_email_confirmation(PHONE, "1", session, {"intent": "general_inquiry"}))["status"] == "selected"
        session.workflow_state["confirmation_stage"] = "selection"
        profile.handle_profile_selection_response.return_value = {"status": "profile_selected_and_authenticated"}
        auth_service._process_selected_email = AsyncMock(return_value={"status": "selected"})
        profile.handle_profile_selection_response.return_value = {"status": "profile_selected_and_authenticated"}
        # A selected profile with no email returns the profile service result unchanged.
        profile.handle_profile_selection_response.return_value = {"status": "profile_selected_and_authenticated", "email": None}
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            assert (await auth_service.handle_email_confirmation(PHONE, "1", session, {"intent": "general_inquiry"}))["status"] == "profile_selected_and_authenticated"
        session.workflow_state["current_intent_result"] = {}
        session.workflow_state["intent_result"] = {}
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            await auth_service.handle_email_confirmation(PHONE, "1", session)
        auth_service.filter_users_by_intent = MagicMock(return_value={"success": False})
        session.workflow_state["email_options"] = [EMAIL, "seller@example.com"]
        session.workflow_state["filtered_users"] = [buyer_user_dict(), seller_user_dict()]
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            await auth_service.handle_email_confirmation(PHONE, "1", session, {"intent": "buy_something", "confidence": 90})
        profile.handle_profile_selection = AsyncMock(return_value={"status": "profile_list"})
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            assert (await auth_service._request_email_selection_with_text(PHONE, session, [EMAIL], [buyer_user_dict()]))["status"] == "profile_list"
        assert (await auth_service._parse_email_selection("99", [EMAIL])) is None

    @pytest.mark.asyncio
    async def test_auth_remaining_ai_and_process_paths(self, auth_service, auth_dependencies):
        auth_dependencies.openai.generate_response.return_value = "yes"
        assert await auth_service._validate_email_confirmation_with_ai("yes", [EMAIL]) == EMAIL
        auth_dependencies.openai.generate_response.side_effect = RuntimeError("ai")
        assert await auth_service._validate_email_confirmation_with_ai("x", [EMAIL]) is None
        assert await auth_service._ai_parse_email_selection("not my email", [EMAIL]) is None
        auth_dependencies.openai.generate_response.side_effect = None
        auth_dependencies.openai.generate_response.return_value = "maybe"
        assert await auth_service._ai_parse_email_selection("x", [EMAIL]) is None
        auth_dependencies.openai.generate_response.side_effect = RuntimeError("ai")
        assert await auth_service._ai_parse_email_selection("x", [EMAIL]) is None
        assert await auth_service._ai_validate_confirmation_response("x") is None
        assert await auth_service._ai_detect_email_rejection("not my email") is True
        auth_dependencies.openai.generate_response.side_effect = None
        auth_dependencies.openai.generate_response.return_value = "not a string"
        assert await auth_service._ai_detect_email_rejection("x") is False
        auth_dependencies.openai.generate_response.side_effect = RuntimeError("ai")
        assert await auth_service._ai_detect_email_rejection("wrong email") is True
        auth_dependencies.verification.check_and_enforce_verification.return_value = {
            "access_granted": False, "redirect_info": {"flow": "other"}
        }
        assert (await auth_service._process_selected_email(PHONE, make_session(), EMAIL, [buyer_user_dict()]))["status"] == "verification_required"
        auth_dependencies.verification.check_and_enforce_verification.return_value = {"access_granted": True}
        auth_service.store_user_session_with_email = AsyncMock(return_value=False)
        assert (await auth_service._process_selected_email(PHONE, make_session(), EMAIL, [buyer_user_dict()]))["status"] == "authentication_completed"
        auth_dependencies.verification.check_and_enforce_verification.return_value = {
            "access_granted": False, "redirect_to_support": True, "redirect_info": {"reason": "blocked"}
        }
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await auth_service._process_selected_email(PHONE, make_session(), "seller@example.com", [seller_user_dict()]))["status"] == "redirected_to_support"
        assert auth_service._determine_user_type(None) == "unknown"

    @pytest.mark.asyncio
    async def test_auth_lazy_helper_errors_and_notification_failure(self, auth_service, auth_dependencies):
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await auth_service._request_email_confirmation(PHONE, make_session(), EMAIL, []))["status"] == "error"
        auth_dependencies.whatsapp.send_message.side_effect = None
        auth_dependencies.whatsapp.send_configurable_buttons.side_effect = RuntimeError("buttons")
        await auth_service._send_seller_menu_options(PHONE, "seller")
        auth_dependencies.support.notify_buyer_registration_not_approved.side_effect = RuntimeError("notify")
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_valid"}
        auth_dependencies.domain.process_user_approval.return_value = {"status": "pending"}
        auth_dependencies.domain.check_domain_match.return_value = {}
        session = make_session({"filtered_users": [buyer_user_dict(approved=False)], "otp_email": EMAIL, "selected_user": buyer_user_dict(approved=False)})
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await auth_service.handle_email_otp_validation(PHONE, "otp", session))["status"] == "redirected_to_support"
        auth_dependencies.cache.store_user_data.return_value = False
        auth_dependencies.redis.store.return_value = True
        with patch("app.services.user_cache_service.get_user_cache_service", return_value=auth_dependencies.cache):
            assert await auth_service._store_verified_user_session(PHONE, buyer_user_dict(), EMAIL) is False


class TestAdditionalRegistrationBranches:
    @pytest.mark.asyncio
    async def test_registration_validation_errors_and_confirmation_result(self, registration_service, auth_dependencies):
        registration_service._check_exit_command = AsyncMock(return_value=False)
        registration_service._test_entity.extract_entities.return_value = {"entities": {"email": "bad"}}
        registration_service._auto_fill_address_from_pincode = AsyncMock(side_effect=lambda entities, *_: entities.update({"_pincode_error": "Pincode 123456 does not exist"}))
        registration_service.authentication_helpers.validate_entities = AsyncMock(return_value=({"email": "bad", "gstin": "x", "zipCode": "123456"}, "Organization email invalid; GSTIN invalid"))
        with patch.object(registration_module.AuthenticationHelpers, "get_missing_fields", return_value=["name"]):
            result = await registration_service.handle_registration_data_collection(PHONE, "bad", make_session())
        assert result["status"] == "data_collection_in_progress"
        registration_service._submit_registration = AsyncMock(return_value={"status": "failed"})
        session = make_session({"user_type": "buyer", "registration_entities": valid_buyer_entities()})
        registration_service.confirmation_service.parse_confirmation.return_value = "yes"
        assert (await registration_service.handle_registration_confirmation(PHONE, "yes", session))["status"] == "failed"
        registration_service.confirmation_service = None
        registration_service._send_clarification_with_buttons = AsyncMock()
        assert (await registration_service.handle_registration_confirmation(PHONE, "maybe", session))["status"] == "awaiting_confirmation"

    @pytest.mark.asyncio
    async def test_registration_submit_category_conflict_and_notifications(self, registration_service, auth_dependencies):
        registration_service._categorize_seller_products = AsyncMock(return_value=[{"category": "A", "division": ""}])
        auth_dependencies.register_api.register_seller.return_value = {"status": "Success", "data": {"id": "s", "orgId": "o"}}
        assert (await registration_service._submit_registration(PHONE, make_session(), valid_seller_entities(), "seller"))["status"] == "registration_completed"
        auth_dependencies.register_api.register_buyer.return_value = {"status": "Failure", "message": "Already Exists"}
        registration_service.session_manager.send_and_track_message = AsyncMock()
        registration_service.session_manager.save_session = AsyncMock()
        with patch("app.services.cancel_service.CancelService") as cancel_cls:
            cancel_cls.return_value._clear_workflow_state = AsyncMock()
            assert (await registration_service._submit_registration(PHONE, make_session({"user_type": "buyer"}), valid_buyer_entities(), "buyer"))["status"] == "user_already_exists"
        auth_dependencies.support.notify_registration_failed.side_effect = RuntimeError("notify")
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        auth_dependencies.register_api.register_buyer.return_value = {"statusCode": "500", "message": "bad"}
        assert (await registration_service._submit_registration(PHONE, make_session(), valid_buyer_entities(), "buyer"))["status"] == "redirected"

    @pytest.mark.asyncio
    async def test_registration_otp_matching_fallbacks_and_exceptions(self, registration_service, auth_dependencies):
        registration_service._check_exit_command = AsyncMock(return_value=False)
        auth_dependencies.otp.handle_user_message.return_value = {"status": "otp_valid"}
        entities = valid_buyer_entities() | {"user_id": "missing"}
        session = make_session({"pending_registration_data": entities, "user_type": "buyer"})
        auth_dependencies.auth_api.authenticate_user.return_value = {"success": True, "data": [seller_user_dict()]}
        with patch("app.services.verification_check_service.VerificationCheckService") as verification_cls:
            verification_cls.return_value._check_domain_approval = AsyncMock(return_value={"approved": True})
            assert (await registration_service.handle_registration_otp_validation(PHONE, "otp", session))["status"] == "buyer_options_presented"
        auth_dependencies.auth_api.authenticate_user.side_effect = RuntimeError("api")
        registration_service._redirect_to_support = AsyncMock(return_value={"status": "redirected"})
        assert (await registration_service.handle_registration_otp_validation(PHONE, "otp", session))["status"] == "redirected"
        registration_service._check_exit_command = AsyncMock(return_value=True)
        registration_service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
        assert (await registration_service.handle_registration_otp_validation(PHONE, "exit", session))["status"] == "exit"

    @pytest.mark.asyncio
    async def test_registration_auto_fill_and_categorization_errors(self, registration_service, auth_dependencies):
        await registration_service._auto_fill_address_from_pincode({"zipCode": "123"}, PHONE, make_session())
        await registration_service._auto_fill_address_from_pincode({"zipCode": "110001", "address1": "x"}, PHONE, make_session())
        with patch.object(registration_module, "get_location_from_pincode_async", new=AsyncMock(return_value={"city": "Delhi"})):
            await registration_service._auto_fill_address_from_pincode({"zipCode": "110001"}, PHONE, make_session())
        with patch.object(registration_module, "get_location_from_pincode_async", new=AsyncMock(side_effect=RuntimeError("lookup"))):
            await registration_service._auto_fill_address_from_pincode({"zipCode": "110001"}, PHONE, make_session())
        auth_dependencies.openai.parse_seller_product_items = AsyncMock(return_value={"success": True, "items": ["p"]})
        with patch("app.services.auto_categorization_service.get_auto_categorization_service", side_effect=RuntimeError("model")):
            assert await registration_service._categorize_seller_products("p", PHONE, make_session()) == []
        categorizer = MagicMock()
        categorizer.categorize_item = AsyncMock(side_effect=RuntimeError("item"))
        with patch("app.services.auto_categorization_service.get_auto_categorization_service", return_value=categorizer):
            assert await registration_service._categorize_seller_products("p", PHONE, make_session()) == []
        auth_dependencies.openai.parse_seller_product_items.side_effect = RuntimeError("parse")
        assert await registration_service._categorize_seller_products("p", PHONE, make_session()) == []

    @pytest.mark.asyncio
    async def test_registration_neutral_and_redirect_errors(self, registration_service, auth_dependencies):
        result = await registration_service._handle_neutral_greeting(PHONE, [{"role": "buyer", "email": EMAIL, "fullName": "Alice Smith"}], make_session())
        assert result["profiles_count"] == 1
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await registration_service._handle_neutral_greeting(PHONE, [], make_session()))["status"] == "error"
        registration_service.session_manager = MagicMock()
        registration_service.session_manager.send_and_track_message = AsyncMock()
        with patch("app.services.exit_service.ExitService") as exit_cls:
            exit_cls.return_value.handle_exit_intent = AsyncMock()
            assert (await registration_service._redirect_to_support(PHONE, "x", "bad", make_session()))["status"] == "redirected_to_support"


class TestAuthenticationCoverageEdges:
    @pytest.mark.asyncio
    async def test_auth_edge_exceptions_and_multi_email_rendering(self, auth_service, auth_dependencies):
        auth_dependencies.openai.generate_response.side_effect = RuntimeError("ai")
        assert await auth_service._validate_confirmation_with_ai("unclear") is False
        assert await auth_service._is_email_rejection(None) is False
        assert auth_service._get_user_type_for_email(EMAIL, None) is None
        assert await auth_service._parse_email_selection(None, [EMAIL]) is None
        assert await auth_service._clear_session_only(None) is True
        auth_dependencies.openai.generate_response.side_effect = None
        auth_dependencies.openai.generate_response.return_value = "rendered"
        await auth_service._generate_email_confirmation_response(
            "A", [EMAIL, "other@example.com"], [buyer_user_dict(), buyer_user_dict(id="b2", username="other@example.com")]
        )
        session = make_session({"filtered_users": [{}]})
        profile = MagicMock()
        profile.handle_profile_selection = AsyncMock(return_value={"status": "shown"})
        with patch("app.services.profile_selection_service.ProfileSelectionService", return_value=profile):
            assert (await auth_service._request_email_selection_with_text(PHONE, session, [EMAIL], [{}]))["status"] == "shown"
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await auth_service._request_email_selection(PHONE, session, [EMAIL]))["status"] == "error"
        auth_dependencies.whatsapp.send_message.side_effect = None
        auth_dependencies.whatsapp.send_message.side_effect = RuntimeError("wa")
        assert (await auth_service._handle_domain_mismatch(PHONE, buyer_user_dict(), session))["status"] == "error"
