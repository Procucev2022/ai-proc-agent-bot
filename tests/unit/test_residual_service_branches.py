"""Deterministic residual branch coverage for the larger service modules.

All database, vector-store, AI, WhatsApp, HTTP, and filesystem boundaries in
this module are replaced with small in-memory fakes.
"""

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.conversation_analytics_service as analytics_mod
import app.services.enhanced_auto_categorization_service as auto_mod
import app.services.learning_categorization_service as learning_mod
import app.services.seller_categorization_service as seller_cat_mod
import app.services.seller_service as seller_mod
import app.services.whatsapp_service as whatsapp_mod
import app.services.enhanced_excel_report_service as report_mod
import app.services.enhanced_seller_matching_service as matching_mod
from app.services.whatsapp_service import MessageResponse


class Query:
    def __init__(self, *, all_rows=None, first=None, scalar=0, count=None):
        self.all_rows = list(all_rows or [])
        self.first_value = first
        self.scalar_value = scalar
        self.count_value = scalar if count is None else count

    def filter(self, *args, **kwargs):
        return self

    def with_entities(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def group_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def distinct(self, *args, **kwargs):
        return self

    def first(self):
        return self.first_value

    def all(self):
        return self.all_rows

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.count_value

    def delete(self):
        return 0


class DB:
    def __init__(self, query=None):
        self.query_obj = query or Query()
        self.bind = "fake-bind"
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()

    def query(self, *args, **kwargs):
        return self.query_obj

    def add(self, value):
        self.added.append(value)


class DBContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


class Collection:
    def __init__(self):
        self.query = MagicMock(return_value={"documents": [[]], "metadatas": [[]], "distances": [[]]})
        self.count = MagicMock(return_value=0)
        self.upsert = MagicMock()


def bare(cls, **attrs):
    service = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


def analytics_service():
    return bare(
        analytics_mod.ConversationAnalyticsService,
        batch_size=2,
        settings=SimpleNamespace(),
        openai_service=SimpleNamespace(
            client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())),
            _load_prompt=MagicMock(return_value="system"),
            default_model="test-model",
            tools_dir=".",
        ),
    )


def seller_service(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock())
    manager = SimpleNamespace(save_session=AsyncMock())
    api = SimpleNamespace()
    db = DB()
    monkeypatch.setattr(seller_mod, "DatabaseManager", lambda **_: db)
    monkeypatch.setattr(seller_mod, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_mod, "SessionManagementService", lambda *a, **k: manager)
    monkeypatch.setattr(seller_mod, "ChatSummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "RFQStatusService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_mod, "ResponseHelpers", lambda *_: MagicMock())
    monkeypatch.setattr(seller_mod, "get_settings", lambda: SimpleNamespace(
        support_contact_info="support@example.com",
        procucev_rfq_details_url="https://portal.example/rfqs",
    ))
    return seller_mod.SellerService(wa, manager, db), wa, api, manager


# Conversation analytics ----------------------------------------------------


def test_analytics_dataframe_empty_malformed_and_alternate_history():
    service = analytics_service()
    sessions = [
        {
            "session_id": "buyer",
            "phone_number": "+911",
            "user_type": "buyer",
            "buyer_identities": [{"buyer_email": "b@x", "buyer_metrics": {"bfs_search_details": [{"search_keyword": "bolt", "results_found": ["A", "", 2]}]}}],
        },
        {"session_id": "seller", "seller_identities": [{"seller_email": "s@x", "seller_metrics": {}}]},
        {"session_id": "unknown", "unknown_user_metrics": {"unregistered_buyer_bfs_only": 1}},
        {"session_id": "malformed", "buyer_identities": [{}], "seller_identities": [{}]},
    ]
    buyer, seller, unknown = service._create_dataframes_from_sessions(sessions, "2025-01-01")
    assert list(buyer["buyer_email"]) == ["b@x", ""]
    assert list(seller["seller_email"]) == ["s@x", ""]
    assert unknown.iloc[0]["unregistered_buyer_bfs_only"] == 1
    bfs = service._create_bfs_search_dataframe(sessions, "2025-01-01")
    assert bfs.iloc[0]["searched_result"] == "A, 2"
    assert service._create_bfs_search_dataframe([], "2025-01-01").empty
    assert service._create_seller_rfq_interest_event_df([{"seller_rfq_interest_event": []}], "x").empty
    assert service._extract_chat_sequence({"messages": [{"role": "assistant", "content": {"other": 1}}]})
    assert service._extract_chat_sequence([{"role": "user"}]) == ""
    assert service._safe_json_value({}) is None
    assert service._safe_json_value(4) == 4


@pytest.mark.asyncio
async def test_analytics_batch_success_none_exception_and_individual_fallback(monkeypatch):
    service = analytics_service()
    session = SimpleNamespace(
        session_id="S1", external_user_id="U1", created_at=None,
        conversation_history=[{"role": "user", "content": "hello"}, "bad"],
    )
    assert (await service._prepare_batch_session_data([session]))[0]["conversation_history"][0]["content"] == "hello"
    service._prepare_batch_session_data = AsyncMock(return_value=[{"session_id": "S1"}])
    service._analyze_batch_with_ai = AsyncMock(return_value={"sessions": [{"session_id": "S1"}]})
    monkeypatch.setattr(analytics_mod.asyncio, "sleep", AsyncMock())
    result = await service._process_sessions_in_batches([session, session, session], date(2025, 1, 1))
    assert result["total_sessions"] == 2
    service._analyze_batch_with_ai.side_effect = [None, RuntimeError("AI down")]
    result = await service._process_sessions_in_batches([session, session], date(2025, 1, 1))
    assert result["sessions"] == []
    service._analyze_batch_with_ai = AsyncMock(side_effect=[{"sessions": [{"session_id": "S1"}]}, RuntimeError("rate")])
    individual = await service._process_sessions_individually([{"session_id": "S1"}, {"session_id": "S2"}], 1, date(2025, 1, 1))
    assert individual["total_sessions"] == 1


@pytest.mark.asyncio
async def test_analytics_ai_output_empty_and_daily_empty(monkeypatch):
    service = analytics_service()
    response = SimpleNamespace(output=[])
    service.openai_service.client.responses.create.return_value = response
    assert await service._analyze_batch_with_ai([], 1, date(2025, 1, 1)) is None
    service.openai_service.client.responses.create.side_effect = RuntimeError("ordinary failure")
    assert await service._analyze_batch_with_ai([], 1, date(2025, 1, 1)) is None
    db = DB(Query(all_rows=[]))
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: DBContext(db))
    result = await service.analyze_daily_conversations(date(2025, 1, 1))
    assert result["success"] and result["total_sessions"] == 0
    db.query_obj.all = lambda: (_ for _ in ()).throw(RuntimeError("db failure"))
    result = await service.analyze_daily_conversations(date(2025, 1, 1))
    assert result["success"] is False and "db failure" in result["error"]


# Enhanced auto categorization ---------------------------------------------


def auto_service():
    return bare(
        auto_mod.EnhancedAutoCategorizationService,
        collection=Collection(),
        category_collection=Collection(),
        fallback_service=MagicMock(),
        openai_service=MagicMock(),
        chroma_path="fake-chroma",
    )


def taxonomy_meta(category="Tools"):
    return {
        "item_description": "bolt",
        "client_category_name": category,
        "level_1_category": "Hardware",
        "level_2_category": "Fasteners",
        "level_3_category": "Bolts",
        "category_path": "Hardware > Fasteners > Bolts",
        "confidence_score": 0.8,
    }


@pytest.mark.asyncio
async def test_enhanced_auto_hybrid_hierarchical_and_pipeline_alternates(monkeypatch):
    service = auto_service()
    assert service._build_enhanced_description("x", {"level_3_category": "X", "level_2_category": "Y"}) == "x Y"
    service.category_collection.count.return_value = 0
    assert service._search_by_category_name("x")["success"] is False
    service.category_collection.count.return_value = 1
    service.category_collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service._search_by_category_name("x")["matches"] == []
    service.category_collection.query.return_value = {"documents": [["Tools"]], "metadatas": [[{"item_count": 3}]], "distances": [[0.4]]}
    assert service._search_by_category_name("bolt")["best_match"]["similarity"] == 0.8
    assert service._hybrid_category_selection("Tools", .8, {"success": False})["method"] == "item_based_only"
    assert service._hybrid_category_selection("Tools", .8, {"success": True, "matches": [{"category_name": "tools", "similarity": .4}]})["agreement"]
    assert service._hybrid_category_selection("Wrong", .95, {"success": True, "matches": [{"category_name": "Right", "similarity": .9}]})["method"] == "hybrid_item_trusted"
    override = service._hybrid_category_selection("Wrong", .4, {"success": True, "matches": [{"category_name": "Right", "similarity": .9}]})
    assert override["method"] == "hybrid_category_override"
    service.collection.query.side_effect = [
        {"documents": [[]], "metadatas": [[]], "distances": [[]]},
        {"documents": [["x"]], "metadatas": [[{**taxonomy_meta(), "level_3_category": "", "level_2_category": "Fasteners"}]], "distances": [[0.8]]},
        {"documents": [["x"]], "metadatas": [[taxonomy_meta()]], "distances": [[0.8]]},
    ]
    found = service._search_hierarchical_levels("bolt", similarity_threshold=.75)
    assert found["success"] and found["matched_level"] == "level_1"
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={"success": False})
    service.fallback_service._get_similar_items.return_value = []
    service._log_fallback_categorization = MagicMock()
    result = await service.categorize_item("unknown", "u")
    assert result["method"] == "enhanced_no_match"
    service.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .7}]
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Tools", "confidence": .7})
    service._update_learning_taxonomy = AsyncMock(return_value=True)
    result = await service.categorize_item("bolt", "u")
    assert result["method"] == "enhanced_fallback_openai" and result["learning_updated"]
    service._search_hierarchical_levels = MagicMock(return_value={"success": True, "similarity_score": .95, "best_match": taxonomy_meta("Tools"), "all_level_matches": [{"metadata": taxonomy_meta("Tools"), "similarity_score": .95, "matched_level": "level_3"}]})
    service._log_categorization = MagicMock()
    result = await service.categorize_item("bolt", "u")
    assert result["method"] == "enhanced_taxonomy_high_similarity"


def test_enhanced_auto_keyword_logging_health_stats_and_errors(monkeypatch):
    service = auto_service()
    monkeypatch.setattr(auto_mod, "execute_remote_query", lambda *_: [])
    assert service._keyword_lookup_source_of_truth("ab")["success"] is False
    monkeypatch.setattr(auto_mod, "execute_remote_query", lambda *_: (_ for _ in ()).throw(RuntimeError("sql")))
    assert service._keyword_lookup_source_of_truth("bolt")["success"] is False
    db = DB()
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    service._log_categorization("x", "u", None, None, "Tools", .8, .7, "test", 1, {"match_level": "level_2", "level_2_category": "Fasteners"})
    db.commit.side_effect = RuntimeError("commit")
    service._log_fallback_categorization("x", "u", None, None, "Other", .3, "fallback", 1, "none")
    service.collection.count.return_value = 2
    service.fallback_service.get_collection_stats.return_value = {"total_items": 1}
    assert service.health_check()["overall_status"] == "healthy"
    service.collection.count.side_effect = RuntimeError("chroma")
    assert service.health_check()["overall_status"] == "unhealthy"
    service.collection.count.side_effect = None
    service.collection.query.side_effect = RuntimeError("query")
    service.collection.count.return_value = 2
    assert service.get_stats()["unified_vector_store"]["category_items"] == "unknown"


# Learning and seller categorization ---------------------------------------


def test_learning_suggestions_dedup_and_db_failure(monkeypatch):
    service = bare(learning_mod.LearningCategorizationService, openai_service=MagicMock())
    category = SimpleNamespace(id="c1", level_1_category="Hardware", level_2_category="Fasteners", level_3_category="Bolts", usage_frequency=4, confidence_score=.9)
    item = SimpleNamespace(normalized_keywords={"keywords": ["bolt", "steel"]}, learning_category_id="c1")
    class SuggestDB(DB):
        def query(self, model=None):
            if model is learning_mod.LearningCategoryItem:
                return Query(all_rows=[item])
            return Query(first=category)
    db = SuggestDB()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    suggestions = service.get_learning_category_suggestions("steel bolt")
    assert suggestions[0]["learning_category_id"] == "c1"
    assert db.close.called
    db.query_obj = Query()
    db.query = MagicMock(side_effect=RuntimeError("db"))
    assert service.get_learning_category_suggestions("steel bolt") == []
    assert service._find_similar_l2("unrelated", {"Valves"}, {}, "A") is None
    conflict_db = DB(Query(all_rows=[SimpleNamespace(level_1_category="Pumps", level_2_category="Hydraulics", level_3_category="Water", usage_frequency=2)]))
    assert service._deduplicate_category_hierarchy(conflict_db, "Other", "Hydraulics", "X")["level_1"] == "Pumps"
    conflict_db.query_obj.all = lambda: (_ for _ in ()).throw(RuntimeError("bad"))
    assert service._deduplicate_category_hierarchy(conflict_db, "A", "B", "C")["level_1"] == "A"


@pytest.mark.asyncio
async def test_learning_validation_create_failure_and_stats_error(monkeypatch):
    service = bare(learning_mod.LearningCategorizationService, openai_service=MagicMock())
    service.openai_service.validate_learning_category = AsyncMock(return_value={"is_valid": True})
    service.openai_service.close_sync = MagicMock()
    assert (await service.validate_learning_category("A", "B", "C", "x"))["is_valid"]
    service.openai_service.validate_learning_category.side_effect = RuntimeError("AI")
    assert not (await service.validate_learning_category("A", "B", "C", "x"))["is_valid"]
    db = DB(Query())
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    service.openai_service.generate_3_level_categorization = AsyncMock(side_effect=RuntimeError("AI"))
    assert (await service.create_3_level_category("x", "A", [], "u"))["success"] is False
    db.query_obj.count = lambda: (_ for _ in ()).throw(RuntimeError("stats"))
    assert "error" in service.get_learning_category_stats()


@pytest.mark.asyncio
async def test_seller_categorization_success_partial_and_batch_exception(monkeypatch):
    db = DB(Query())
    monkeypatch.setattr(seller_cat_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(seller_cat_mod, "OpenAIService", lambda: MagicMock())
    service = seller_cat_mod.SellerCategorizationService(db)
    seller = SimpleNamespace(seller_id="s1", seller_name="Seller", location={}, ranking=None, categories=["Tools", "Other"])
    service._get_seller_details = AsyncMock(return_value=seller)
    service._create_categorization_job = AsyncMock(return_value=SimpleNamespace(job_id="job"))
    service._generate_3_level_mapping_for_category = AsyncMock(side_effect=[{"success": True, "mapping": {"x": 1}}, {"success": False, "error": "bad"}])
    service._store_seller_mappings = AsyncMock()
    service._update_categorization_job = AsyncMock()
    result = await service.categorize_seller_categories("s1")
    assert result["success"] and result["mappings_created"] == 1
    service._generate_3_level_mapping_for_category.side_effect = RuntimeError("AI")
    assert (await service.categorize_seller_categories("s1"))["success"] is False
    service.categorize_seller_categories = AsyncMock(side_effect=[{"success": True}, RuntimeError("down"), {"success": False, "error": "bad"}])
    batch = await service._process_seller_batch([seller, seller, seller])
    assert batch["processed"] == 1 and batch["errors"] == 2
    service._get_sellers_needing_categorization = AsyncMock(return_value=[])
    assert (await service.process_all_sellers())["sellers_processed"] == 0
    service._get_sellers_needing_categorization.side_effect = RuntimeError("query")
    assert (await service.process_all_sellers())["success"] is False


# Seller workflow -----------------------------------------------------------


@pytest.mark.asyncio
async def test_seller_workflow_routes_all_states_and_fallbacks(monkeypatch):
    service, wa, api, manager = seller_service(monkeypatch)
    user = SimpleNamespace(id="seller", org_id="org", phone_number="919999999999", email="s@x")
    session = SimpleNamespace(workflow_state={}, workflow_type=None, conversation_history={})
    service._display_rfqs_to_seller = AsyncMock(return_value={"step": "display"})
    assert (await service.handle_seller_workflow(user, session, "show available rfqs"))["step"] == "display"
    service._handle_plan_selection_response = AsyncMock(return_value={"step": "plan"})
    session.workflow_state = {"seller_workflow_state": "awaiting_plan_selection"}
    assert (await service.handle_seller_workflow(user, session, "basic"))["step"] == "plan"
    service._handle_general_seller_response = AsyncMock(return_value={"step": "general"})
    session.workflow_state = {"seller_workflow_state": "awaiting_general_response"}
    assert (await service.handle_seller_workflow(user, session, "hello"))["step"] == "general"
    service._display_rfqs_to_seller.side_effect = RuntimeError("workflow")
    service._handle_workflow_error = AsyncMock(return_value={"step": "error"})
    session.workflow_state = {}
    assert (await service.handle_seller_workflow(user, session, "hello"))["step"] == "error"
    assert service._fallback_intent_classification("sure", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("subscribe", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("send RFQ details", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("???", {})["intent"] == "general_question"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "choose a subscription plan"}]}
    service._handle_plan_upgrade_request = AsyncMock(return_value={"step": "upgrade"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "upgrade"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "RFQ details"}]}
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 0})
    service._handle_no_credits_response = AsyncMock(return_value={"step": "credits"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "credits"
    session.conversation_history = {"messages": []}
    service._handle_general_affirmative_response = AsyncMock(return_value={"step": "generic"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "generic"


@pytest.mark.asyncio
async def test_seller_plan_email_and_credit_alternates(monkeypatch):
    service, wa, api, manager = seller_service(monkeypatch)
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="response")
    user = SimpleNamespace(id="seller", org_id="org", phone_number="1", email="s@x")
    session = SimpleNamespace(workflow_state={}, workflow_type=None, outcome=None)
    api.get_subscription_plans = AsyncMock(return_value={"success": False})
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["workflow_step"] == "plan_fetch_error"
    api.get_subscription_plans.return_value = {"success": True, "plans": [{"id": "p", "planName": "Basic"}]}
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="plans")
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["success"]
    service._extract_plan_selection = AsyncMock(return_value=None)
    assert not (await service._handle_plan_selection_response(user, session, "bad"))["success"]
    service._extract_plan_selection.return_value = {"id": "p"}
    api.generate_payment_link = AsyncMock(return_value={"success": False})
    assert not (await service._handle_plan_selection_response(user, session, "p"))["success"]
    api.generate_payment_link.return_value = {"success": True, "payment_url": "https://pay"}
    assert (await service._handle_plan_selection_response(user, session, "p"))["success"]
    service.response_helpers.generate_seller_contextual_response.return_value = "status"
    api.send_rfq_email = AsyncMock(return_value={"data": {"success": True, "results": {"successful": [{"rfq_id": "R1"}], "failed": [{"rfq_id": "R2", "error_code": "NO_CREDITS"}]}}})
    result = await service._process_rfq_email_requests(user, session, ["R1", "R2"])
    assert result["emails_sent"] == 1 and result["error_analysis"]["total_failed"] == 1
    api.send_rfq_email.side_effect = RuntimeError("HTTP")
    result = await service._process_rfq_email_requests(user, session, ["R3"])
    assert result["error_analysis"]["error_counts"]["API_ERROR"] == 1
    api.get_subscription_plans.return_value = {"success": False}
    assert (await service._handle_no_credits_response(user, session))["workflow_step"] == "plan_fetch_error"


# WhatsApp ------------------------------------------------------------------


class Retry:
    def __init__(self, result):
        self.result = result
        self.max_retries = 0
        self.initial_delay = 0

    async def retry_with_backoff(self, operation):
        return self.result


def whatsapp_service(monkeypatch, result=None):
    monkeypatch.setattr(whatsapp_mod, "get_settings", lambda: SimpleNamespace(
        WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p", WHATSAPP_FROM_NUMBER="f",
        WHATSAPP_BASE_URL="https://wa", WHATSAPP_TEMPLATE_BASE_URL="https://wa/templates",
        WHATSAPP_MOCK_MODE=False, retry_max_attempts=1, retry_initial_delay=0,
    ))
    retry = Retry(result or {"success": True, "attempts": 1, "result": MessageResponse(True, "m")})
    monkeypatch.setattr(whatsapp_mod, "get_retry_service", lambda: retry)
    return whatsapp_mod.WhatsAppService()


@pytest.mark.asyncio
async def test_whatsapp_send_template_interactive_payloads_and_failures(monkeypatch):
    service = whatsapp_service(monkeypatch)
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    monkeypatch.setattr(whatsapp_mod, "post_to_gateway", AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok", json=lambda: {"mid": "m1"})))
    assert (await service.send_message("+919999999999", "hello", session_id="sid")).success
    assert service._track_message_in_history.await_count == 1
    assert (await service.send_template_message("919999999999", "welcome", ["A"])).message_id == "m"
    assert (await service.send_interactive_message("919999999999", "button", {"body": {"text": "x"}})).success
    assert not (await service.send_interactive_message("bad", "button", {})).success
    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True, "m"))
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))
    result = await service.send_list_message("1", "H", "B", [{"title": str(i)} for i in range(11)])
    assert result.success
    result = await service.send_button_message("1", "H", "B", [{"title": "1"}, {"title": "2"}, {"title": "3"}, {"title": "4"}])
    assert result.success
    assert (await service.send_configurable_buttons("919999999999", "body", [{"id": "a", "title": "A"}], session_id="sid")).success
    assert not (await service.send_configurable_buttons("1", "body", [{"id": "a"}])).success
    assert not (await service.send_configurable_buttons("1", "body", [])).success
    assert service._format_phone_number("+91 99999 99999") == "919999999999"
    assert service._format_phone_number("12") == ""


def test_whatsapp_api_response_variants_and_formatters(monkeypatch):
    service = whatsapp_service(monkeypatch)
    response = lambda status, payload: SimpleNamespace(status_code=status, text="body", json=lambda: payload)
    assert service._handle_api_response(response(200, [{"mid": 4}])).message_id == "4"
    assert service._handle_api_response(response(200, {"status": "ok"})).success
    assert not service._handle_api_response(response(500, {"Error": "down"})).success
    bad = SimpleNamespace(status_code=200, text="bad", json=MagicMock(side_effect=ValueError("json")))
    assert not service._handle_api_response(bad).success
    assert service.format_vendor_results([]).startswith("No vendors")
    assert "more vendors" in service.format_vendor_results([{"name": str(i)} for i in range(6)])
    assert service.format_bfs_results([]).startswith("No products")
    assert "Product" in service.format_rfq_summary({"product_name": "Bolt", "delivery_city": "Pune", "delivery_state": "MH"})


# Excel reports -------------------------------------------------------------


class RollingFrame:
    empty = False

    def __init__(self):
        self.iloc = self

    def __getitem__(self, index):
        return {0: {"unique_buyers": 2}}


class Writer:
    def __init__(self):
        self.book = SimpleNamespace(sheetnames=[])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def report_service():
    return bare(report_mod.EnhancedExcelReportService, settings=SimpleNamespace())


def test_excel_report_metric_fallbacks_and_sanitization(monkeypatch):
    service = report_service()
    assert service.sanitize_for_excel(None) == ""
    assert service.sanitize_for_excel("a\x00b\x7fc") == "abc"
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Seller Chats Initiated", 3]], columns=["metric_name", "total_value"])))
    assert service._calculate_seller_metrics(db, date(2025, 1, 1), date(2025, 1, 2))["seller_chats_initiated"] == 3
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("missing")))
    assert service._calculate_seller_metrics(db, date(2025, 1, 1), date(2025, 1, 2))["unique_sellers"] == 0
    assert service._get_seller_metric_value("unknown", {}) == 0
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Total RFQs Submitted", 4], ["Total Items in All RFQs", 10]], columns=["metric_name", "total_value"])))
    assert service._calculate_buyer_metrics(db, date(2025, 1, 1), date(2025, 1, 2))["avg_products_per_rfq"] == 2.5
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=RollingFrame()))
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 7) == {"unique_buyers": 2}
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 2) is None
    assert service._get_metric_value("Unique Buyers", {"unique_buyers": 2}) == 2
    assert service._get_empty_metrics()["total_rfqs"] == 0


def test_excel_report_sheet_queries_and_email_boundary(monkeypatch, tmp_path):
    service = report_service()
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: DBContext(db))
    writer = Writer()
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame({"x": ["a\x00b"]})))
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    service._generate_buyer_details_sheet(writer, date(2025, 1, 2))
    service._generate_seller_details_sheet(writer, date(2025, 1, 2))
    service._generate_category_details_sheet(writer, date(2025, 1, 2))
    service._generate_aggregate_sheet_90d(writer, date(2025, 1, 2))
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("query")))
    assert service._generate_category_details_fallback(db, date(2025, 1, 1), date(2025, 1, 2)).shape[0] == 3
    service._generate_buyer_details_sheet = MagicMock()
    service._generate_seller_details_sheet = MagicMock()
    service._generate_category_details_sheet = MagicMock()
    service._generate_buyer_summary_sheet = MagicMock()
    service._generate_seller_summary_sheet = MagicMock()
    service._generate_category_summary_sheet = MagicMock()
    service._generate_aggregate_sheet_90d = MagicMock()
    service._format_excel_sheets = MagicMock()
    monkeypatch.setattr(report_mod.pd, "ExcelWriter", lambda *a, **k: writer)
    assert service.generate_report(date(2025, 1, 2), str(tmp_path / "report.xlsx"))
    monkeypatch.setattr(report_mod, "init_procucev_api_client", AsyncMock(), raising=False)
    monkeypatch.setattr(report_mod, "close_procucev_api_client", AsyncMock(), raising=False)


# Enhanced seller matching --------------------------------------------------


def matching_service():
    return bare(matching_mod.EnhancedSellerMatchingService, collection=Collection(), openai_service=MagicMock(), chroma_path="fake")


def seller_meta(seller_id="s1", location=None, ranking="Gold"):
    return {
        "seller_id": seller_id, "seller_name": seller_id, "phone_number": "9199", "email": "",
        "original_category": "Tools", "level_1_category": "Hardware", "level_2_category": "Fasteners",
        "level_3_category": "Bolts", "category_path": "Hardware > Fasteners > Bolts", "confidence_score": .8,
        "ranking": ranking, "location": location,
    }


@pytest.mark.asyncio
async def test_enhanced_matching_filters_duplicates_malformed_locations_and_openai(monkeypatch):
    service = matching_service()
    service.collection.query.return_value = {
        "documents": [["a", "b", "c"]],
        "metadatas": [[seller_meta("s1", "bad-json", "Gold"), seller_meta("s1", {"lat": 1, "lng": 1}, "Diamond"), seller_meta("s2", {"lat": .01, "lng": .01}, "Titanium")]],
        "distances": [[.1, .2, .1]],
    }
    service.openai_service.select_best_sellers = AsyncMock(return_value={"success": True, "selected_sellers": [{"seller_id": "s1"}], "confidence_score": .9})
    result = await service.find_sellers_for_item("bolt", {"lat": 0, "lng": 0}, max_distance_km=10)
    assert result["success"] and result["method"].endswith("openai_seller_search")
    service.openai_service.select_best_sellers.return_value = {"success": False, "error": "AI"}
    result = await service.find_sellers_for_item("bolt", ranking_priority=False)
    assert result["success"] and result["method"].endswith("fallback")
    service.collection.query.return_value = {"documents": [["a"]], "metadatas": [[seller_meta()]], "distances": [[.1]]}
    assert service.find_sellers_by_category_path("Hardware > Fasteners > Bolts")["success"]
    service.collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not service.find_sellers_by_category_path("missing")["success"]
    service.collection.query.side_effect = RuntimeError("chroma")
    assert not (await service.find_sellers_for_item("bolt"))["success"]


def test_enhanced_matching_categories_health_stats_and_helpers(monkeypatch):
    service = matching_service()
    service.collection.query.return_value = {"metadatas": [[seller_meta()]]}
    assert service.get_seller_categories("s1")["total_categories"] == 1
    service.collection.query.return_value = {"metadatas": [[]]}
    assert not service.get_seller_categories("missing")["success"]
    service.collection.query.side_effect = RuntimeError("query")
    assert not service.get_seller_categories("bad")["success"]
    assert service._calculate_distance(0, 0, 0, 0) == 0
    assert service._ranking_priority("Diamond") == 4 and service._ranking_priority("other") == 0
    service.collection.query.side_effect = None
    service.collection.count.return_value = 2
    service.collection.query.return_value = {"documents": [["x"]]}
    assert service.health_check()["overall_status"] == "healthy"
    service.collection.count.side_effect = RuntimeError("health")
    assert service.health_check()["overall_status"].startswith("unhealthy")
    db = DB(Query(count=3))
    monkeypatch.setattr(matching_mod, "get_db_session", lambda: db)
    service.collection.count.side_effect = None
    service.collection.count.return_value = 2
    assert service.get_stats()["database_sellers"]["total_sellers"] == 3
    service.collection.count.side_effect = RuntimeError("stats")
    assert "error" in service.get_stats()
