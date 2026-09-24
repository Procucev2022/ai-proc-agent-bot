"""
Regression tests for the four QUA AI WhatsApp Dev defects reported on 2026-08-17,
plus the additional errors found in the same log export (`query_data (3).csv`).

Issue 1 - after confirming the pincode the bot asked for missing product details even
though the user had never mentioned a product. The extraction prompt signals "no
products" with a synthetic product whose description is the sentinel
`NO_PRODUCTS_MENTIONED`; nothing stripped it, so it was stored as item 1 and echoed back:

    whatsapp_service | DEBUG | Button payload: {'body': {'text': "Quantity Required\\n\\n
    ...\\nItem 1: NO_PRODUCTS_MENTIONED\\nQty: [Please provide quantity in number]..."}}

Issue 4 - clicking "Create new RFQ" answered "Missing Some Details / Delivery Date /
Delivery Pincode Required" straight away. The previous attempt had left the sectioned
workflow active and awaiting a delivery-format reply, so the button's own title was fed
to the delivery parser:

    12:58:30 [SECTIONED_RFQ_AWAITING_MOD] Section 'date_location' awaiting modification: True
    13:02:27 [SECTIONED_RFQ] Routing to sectioned RFQ handler (... already active: True)
    13:02:27 [SECTIONED_RFQ] Awaiting modification flag is set - processing modification
    13:02:27 sectioned_rfq_format_parser | WARNING | No delivery format fields detected

Issue 3 - seller registration answered "taking longer than expected due to high traffic".
Confirming registration loaded a SentenceTransformer model inline on the event loop, the
worker stopped answering its heartbeat and was killed mid-request:

    11:34:10 auto_categorization_service | INFO | Initializing AutoCategorizationService singleton
    11:34:19 chromadb.config | DEBUG | Starting component FastAPI
    11:35:23 app.main | INFO | [BOOT] Starting App          <- worker respawned
    11:36:23 inactivity_timeout_service | WARNING | [WORKER_TIMEOUT] DETECTED ... flag=1

Issue 2 - "RFQ Creation Failed". `createRFQByClient` answered HTTP 200 carrying
`statusCode 500 / Failed to create RFQ` in 0.171s. The two payloads accepted that day held
plain ASCII remarks; the rejected one carried `10KΩ`, `±5%` and `1000µF`.

Every external collaborator is mocked. Nothing here touches WhatsApp, OpenAI, Redis,
ChromaDB, a database or the network.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.webhook as webhook_mod
import app.procucev_apis.rfq_apis as rfq_apis_mod
import app.services.auto_categorization_service as auto_cat_mod
import app.services.entity_service as entity_mod
import app.services.handlers.purchase_intent_handler as purchase_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.intent_service as intent_mod
from app.services.entity_service import (
    NO_PRODUCTS_SENTINEL,
    EntityService,
    strip_no_products_sentinel,
)
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.intent_service import RFQ_ENTRY_POINT_BUTTON_IDS, IntentService
from app.services.workflow_manager import WorkflowManager

TOOLS_DIR = Path(__file__).resolve().parents[2] / "app" / "tools"

# The exact item remarks from the payload GMT rejected at 10:04:58 UTC.
REJECTED_REMARKS = ["10K\u03a9, 1/4W, \u00b15%, Through Hole", "1000\u00b5F, 25V, Electrolytic"]


def bare(cls, **attrs):
    """Build an instance without running __init__, then attach mocked collaborators."""
    obj = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(obj, name, value)
    return obj


def session(**state):
    return SimpleNamespace(
        session_id="whatsapp_918296753344_20260817",
        external_user_id="918296753344",
        phone_number="+918296753344",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={},
        product_items=[],
        extracted_entities=[],
        outcome=None,
        completed_at=None,
        rfq_ids=[],
        user_type=None,
        rfq_id=None,
        created_at=None,
    )


def user():
    return SimpleNamespace(
        id="b32b4632-a67e-47d7-aa71-9775018eb8cf",
        org_id="9156b6f0-802b-4204-a4db-b7896e424a36",
        phone_number="918296753344",
        role="buyer",
        is_registered=True,
        self_client=True,
    )


def sectioned_handler():
    handler = bare(
        SectionedRFQCreationHandler,
        entity_service=AsyncMock(),
        whatsapp_service=AsyncMock(),
        cancel_service=AsyncMock(),
        session_manager=AsyncMock(),
        confirmation_handler=AsyncMock(),
        attachment_decision_handler=None,
    )
    handler.whatsapp_service.send_configurable_buttons = AsyncMock()
    return handler


def patch_section_state(monkeypatch):
    """Back the section accessors with the plain session dict used by these tests."""
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "get_section_data",
        lambda s, name: s.workflow_state.get(name),
    )
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "update_section_data",
        lambda s, name, value: s.workflow_state.__setitem__(name, value),
    )
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda s: False
    )


# ---------------------------------------------------------------------------
# Issue 1: the "no products mentioned" sentinel must never become an item line
# ---------------------------------------------------------------------------

def test_strip_no_products_sentinel_removes_only_the_signal_entry():
    sentinel = {"description": NO_PRODUCTS_SENTINEL, "quantity": None, "brand": ""}
    real = {"description": "LED Bulb", "quantity": 100}

    assert strip_no_products_sentinel([sentinel]) == []
    assert strip_no_products_sentinel([sentinel, real]) == [real]
    assert strip_no_products_sentinel([real]) == [real]
    # Surrounding whitespace is still the sentinel.
    assert strip_no_products_sentinel([{"description": f"  {NO_PRODUCTS_SENTINEL} "}]) == []
    # A description that merely contains the token is a real (if odd) product.
    keep = {"description": f"{NO_PRODUCTS_SENTINEL} spare part"}
    assert strip_no_products_sentinel([keep]) == [keep]
    # Falsy and non-dict inputs pass through without raising.
    assert strip_no_products_sentinel([]) == []
    assert strip_no_products_sentinel(None) is None
    assert strip_no_products_sentinel(["junk"]) == ["junk"]


@pytest.mark.asyncio
async def test_entity_extraction_drops_the_sentinel_but_keeps_delivery_fields():
    """The production response for "31 Aug 2026, 560037" with no product mentioned."""
    openai = SimpleNamespace(extract_entities=AsyncMock(return_value={
        "products": [{
            "description": NO_PRODUCTS_SENTINEL, "quantity": None,
            "unitofMeasures": "", "brand": "", "remarks": "",
        }],
        "deliveryDate": "31-08-2026",
        "state": "", "city": "", "pincode": "560037",
        "confidence": 92,
    }))
    service = EntityService(openai_service=openai)

    result = await service._handle_standard_extraction("31 Aug 2026, 560037", {}, "buy_something")

    assert result["products"] == []
    # The delivery details the user did give are still returned.
    assert result["deliveryDate"] == "31-08-2026"
    assert result["pincode"] == "560037"


def test_usable_items_drops_the_sentinel_and_contentless_entries():
    usable = SectionedRFQCreationHandler._usable_items
    real = {"description": "LED Bulb", "quantity": 100}

    assert usable([{"description": NO_PRODUCTS_SENTINEL, "quantity": None}]) == []
    # An entry with no description and no quantity holds nothing to confirm or correct.
    assert usable([{"description": "", "quantity": None, "brand": "", "remarks": ""}]) == []
    # A brand or a quantity on its own is still worth asking the user about.
    assert usable([{"description": "", "quantity": 20, "brand": "Dell"}]) == [
        {"description": "", "quantity": 20, "brand": "Dell"}
    ]
    assert usable([real, {"description": NO_PRODUCTS_SENTINEL}]) == [real]
    assert usable(None) == []
    assert usable(["junk"]) == []


@pytest.mark.asyncio
async def test_items_section_asks_for_items_instead_of_echoing_the_sentinel(monkeypatch):
    handler = sectioned_handler()
    patch_section_state(monkeypatch)
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "is_awaiting_section_modification", lambda *_: False
    )
    handler.entity_service.extract_entities.return_value = {
        "products": [{"description": NO_PRODUCTS_SENTINEL, "quantity": None}]
    }
    handler._display_items_missing_fields = AsyncMock(return_value={"status": "items-missing"})
    state = session()

    result = await handler._handle_items_section(user(), state, "31 Aug 2026, 560037", [])

    assert result["status"] == "awaiting_items"
    handler._display_items_missing_fields.assert_not_awaited()
    assert state.workflow_state["items"] == []
    # The sentinel must not reach the user under any header.
    sent = handler.whatsapp_service.send_configurable_buttons.await_args
    assert sent.args[3] == "Items Required"
    assert NO_PRODUCTS_SENTINEL not in sent.args[1]


@pytest.mark.asyncio
async def test_items_section_cleans_a_sentinel_left_in_an_existing_session(monkeypatch):
    """Sessions stored before the fix still hold the sentinel; it must not resurface."""
    handler = sectioned_handler()
    patch_section_state(monkeypatch)
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "is_awaiting_section_modification", lambda *_: False
    )
    handler.entity_service.extract_entities.return_value = {"products": []}
    handler._display_items_missing_fields = AsyncMock(return_value={"status": "items-missing"})
    state = session(items=[{"description": NO_PRODUCTS_SENTINEL, "quantity": None}])

    result = await handler._handle_items_section(user(), state, "", [])

    assert result["status"] == "awaiting_items"
    assert state.workflow_state["items"] == []
    handler._display_items_missing_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_extraction_does_not_store_the_sentinel_as_an_item(monkeypatch):
    """The turn from the incident: pincode plus date accepted, no item invented."""
    handler = sectioned_handler()
    patch_section_state(monkeypatch)
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager, "set_awaiting_section_modification", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format",
        lambda _: {"error": "Missing required field(s): Delivery Date, Delivery Pincode"},
    )
    handler.entity_service.extract_entities.return_value = {
        "deliveryDate": "31-08-2026",
        "pincode": "560037",
        "city": "", "state": "",
        "products": [{"description": NO_PRODUCTS_SENTINEL, "quantity": None}],
    }
    handler._validate_delivery_date = AsyncMock(
        return_value={"is_valid": True, "normalized_date": "31 August 2026"}
    )
    handler._autofill_location_from_pincode = AsyncMock(return_value={
        "is_valid": True,
        "delivery_data": {
            "deliveryDate": "31 August 2026", "pincode": "560037",
            "city": "Bangalore", "state": "Karnataka",
        },
    })
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    state = session()

    result = await handler._process_delivery_modification_direct(user(), state, "31 Aug 2026 560037")

    assert result["status"] == "confirmation"
    assert state.workflow_state["date_location"]["pincode"] == "560037"
    # No items were mentioned, so the items section stays untouched.
    assert "items" not in state.workflow_state


# ---------------------------------------------------------------------------
# Issue 4: "Create new RFQ" starts a new RFQ instead of resuming a stale one
# ---------------------------------------------------------------------------

def test_button_classification_reports_which_button_was_clicked():
    service = bare(IntentService, openai_service=MagicMock(), settings=MagicMock())

    result = service._classify_button_reply({"button_id": "create_rfq"})

    assert result["intent"] == "buy_something"
    assert result["button_id"] == "create_rfq"
    # Unmapped and absent ids still fall through to the model.
    assert service._classify_button_reply({"button_id": "unmapped_test_button"}) is None
    assert service._classify_button_reply({}) is None
    assert service._classify_button_reply(None) is None


def test_rfq_entry_point_click_recognises_only_the_menu_buttons():
    assert RFQ_ENTRY_POINT_BUTTON_IDS == {"create_rfq", "new_rfq", "raise_rfq"}
    for button_id in sorted(RFQ_ENTRY_POINT_BUTTON_IDS):
        assert PurchaseIntentHandler._is_rfq_entry_point_click({"button_id": button_id}) is True
    assert PurchaseIntentHandler._is_rfq_entry_point_click({"button_id": "rfq_status"}) is False
    assert PurchaseIntentHandler._is_rfq_entry_point_click({}) is False
    assert PurchaseIntentHandler._is_rfq_entry_point_click(None) is False


def purchase_handler(sectioned):
    handler = bare(
        PurchaseIntentHandler,
        whatsapp_service=AsyncMock(),
        response_helpers=MagicMock(),
        entity_service=AsyncMock(),
        chat_summary_service=AsyncMock(),
        products_array_handler=AsyncMock(),
        session_manager=AsyncMock(),
        confirmation_handler=AsyncMock(),
        attachment_decision_handler=None,
    )
    handler.sectioned_rfq_handler = sectioned
    return handler


def stale_sectioned_session():
    """A session in the state the 12:58 turn left behind."""
    state = session()
    WorkflowManager.initialize_sectioned_rfq(state, caller="test")
    state.workflow_state["sectioned_rfq"]["active"] = True
    WorkflowManager.set_sectioned_rfq_section(state, "date_location", caller="test")
    WorkflowManager.set_awaiting_section_modification(state, "date_location", True, caller="test")
    WorkflowManager.update_section_data(state, "items", [{"description": "stale"}], caller="test")
    return state


@pytest.mark.asyncio
async def test_create_new_rfq_button_resets_a_stale_sectioned_workflow(monkeypatch):
    monkeypatch.setattr(
        purchase_mod, "get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True), raising=False
    )
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True)
    )
    sectioned = AsyncMock()
    sectioned.handle_sectioned_rfq = AsyncMock(return_value={"status": "awaiting_delivery_details"})
    handler = purchase_handler(sectioned)
    state = stale_sectioned_session()

    result = await handler.handle_purchase_intent(
        user(), state, "Create new RFQ",
        {"intent": "buy_something", "confidence": 100, "button_id": "create_rfq"},
    )

    assert result["status"] == "awaiting_delivery_details"
    # The workflow restarts clean: nothing is awaiting a format reply and no stale items.
    assert WorkflowManager.is_awaiting_section_modification(state, "date_location") is False
    assert WorkflowManager.get_section_data(state, "items") is None
    assert WorkflowManager.get_sectioned_rfq_section(state) == "date_location"
    assert WorkflowManager.is_sectioned_rfq_active(state) is True
    # The button title is not user-supplied delivery data, so it is not passed on.
    assert sectioned.handle_sectioned_rfq.await_args.args[2] == ""
    handler.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_typed_purchase_message_keeps_an_in_progress_sectioned_workflow(monkeypatch):
    """Only an explicit menu click restarts; typed text must resume where the user was."""
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True)
    )
    sectioned = AsyncMock()
    sectioned.handle_sectioned_rfq = AsyncMock(return_value={"status": "missing"})
    handler = purchase_handler(sectioned)
    state = stale_sectioned_session()

    await handler.handle_purchase_intent(
        user(), state, "Delivery Date: 6 Sep 2026", {"intent": "buy_something", "confidence": 92},
    )

    assert WorkflowManager.is_awaiting_section_modification(state, "date_location") is True
    assert WorkflowManager.get_section_data(state, "items") == [{"description": "stale"}]
    assert sectioned.handle_sectioned_rfq.await_args.args[2] == "Delivery Date: 6 Sep 2026"


# ---------------------------------------------------------------------------
# Issue 3: the categorization model must never load on the event loop
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_categorization_singleton(monkeypatch):
    """Reset the module singleton around each test so ordering cannot leak state."""
    # These tests cover the real (vector) service, which is off by default now.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_vector_search", True)
    original = auto_cat_mod._auto_categorization_service_instance
    auto_cat_mod._auto_categorization_service_instance = None
    yield
    auto_cat_mod._auto_categorization_service_instance = original


class FakeCategorizationService:
    """Stands in for the real service, recording the thread it was built on."""

    def __init__(self):
        self.built_on_thread = threading.get_ident()
        self.collection = SimpleNamespace(count=lambda: 1)


@pytest.mark.asyncio
async def test_singleton_is_built_off_the_event_loop(monkeypatch, clean_categorization_singleton):
    monkeypatch.setattr(auto_cat_mod, "AutoCategorizationService", FakeCategorizationService)
    loop_thread = threading.get_ident()

    instance = await auto_cat_mod.get_auto_categorization_service_async()

    assert isinstance(instance, FakeCategorizationService)
    assert instance.built_on_thread != loop_thread, "model load ran on the event loop"


@pytest.mark.asyncio
async def test_cached_singleton_is_returned_without_a_thread_hop(monkeypatch, clean_categorization_singleton):
    cached = object()
    auto_cat_mod._auto_categorization_service_instance = cached
    to_thread = AsyncMock()
    monkeypatch.setattr(auto_cat_mod.asyncio, "to_thread", to_thread)

    assert await auto_cat_mod.get_auto_categorization_service_async(timeout=5) is cached
    to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_slow_model_load_times_out_instead_of_stalling_the_worker(monkeypatch, clean_categorization_singleton):
    class SlowService:
        def __init__(self):
            threading.Event().wait(0.5)
            self.collection = SimpleNamespace(count=lambda: 1)

    monkeypatch.setattr(auto_cat_mod, "AutoCategorizationService", SlowService)

    with pytest.raises(asyncio.TimeoutError):
        await auto_cat_mod.get_auto_categorization_service_async(timeout=0.01)


def test_concurrent_callers_build_the_model_once(monkeypatch, clean_categorization_singleton):
    """Now that the accessor runs on worker threads, it has to be thread-safe."""
    builds = []
    ready = threading.Barrier(4)

    class CountingService:
        def __init__(self):
            builds.append(threading.get_ident())
            threading.Event().wait(0.05)
            self.collection = SimpleNamespace(count=lambda: 1)

    monkeypatch.setattr(auto_cat_mod, "AutoCategorizationService", CountingService)
    results = []

    def resolve():
        ready.wait()
        results.append(auto_cat_mod.get_auto_categorization_service())

    threads = [threading.Thread(target=resolve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(builds) == 1, f"model built {len(builds)} times"
    assert len(results) == 4
    assert all(item is results[0] for item in results)


@pytest.mark.asyncio
async def test_vector_search_runs_off_the_event_loop():
    """The collection embeds query text in-process, so it is CPU work off the loop."""
    service = bare(auto_cat_mod.AutoCategorizationService)
    seen = {}

    def fake_similar(description):
        seen["thread"] = threading.get_ident()
        seen["description"] = description
        return []

    service._get_similar_items = fake_similar
    service._log_categorization = MagicMock()

    result = await service.categorize_item("Chemicals", user_id="918296753344")

    assert result["reason"] == "no_similar_items_found"
    assert seen["description"] == "Chemicals"
    assert seen["thread"] != threading.get_ident(), "vector search ran on the event loop"


# ---------------------------------------------------------------------------
# Issue 2: outbound RFQ text must be ASCII the GMT backend accepts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10K\u03a9, 1/4W, \u00b15%, Through Hole", "10Kohm, 1/4W, +/-5%, Through Hole"),
        ("1000\u00b5F, 25V, Electrolytic", "1000uF, 25V, Electrolytic"),
        ("12W, 6500K, B22", "12W, 6500K, B22"),
        ("25\u00b0C ambient", "25degC ambient"),
        ("2\u2013core cable \u2264 10mm", "2-core cable <= 10mm"),
        ("caf\u00e9", "cafe"),
        ("\u20b9500 per unit", "INR 500 per unit"),
    ],
)
def test_sanitize_gmt_text_transliterates_technical_symbols(raw, expected):
    assert rfq_apis_mod.sanitize_gmt_text(raw) == expected


def test_sanitize_gmt_text_leaves_non_text_values_alone():
    for value in (None, 100, 1.5, True, [], {}):
        assert rfq_apis_mod.sanitize_gmt_text(value) is value


def test_rejected_rfq_payload_becomes_ascii_without_losing_meaning():
    """Rebuilds the payload GMT rejected at 10:04:58 and checks it is now ASCII."""
    rfq_data = {
        "project_desc": "LED Bulb",
        "items": [
            {"description": "LED Bulb", "quantity": 100, "unit_of_measures": "Nos",
             "brand": "Philips", "remarks": "12W, 6500K, B22"},
            {"description": "Resistor", "quantity": 200, "unit_of_measures": "Nos",
             "remarks": REJECTED_REMARKS[0]},
            {"description": "Capacitor", "quantity": 100, "unit_of_measures": "Nos",
             "remarks": REJECTED_REMARKS[1]},
        ],
        "delivery_state": "Karnataka",
        "delivery_city": "Bangalore",
        "delivery_pincode": "560021",
        "remarks": "12W, 6500K, B22",
        "attachments": [],
        "product_name": "LED Bulb",
    }
    service = bare(rfq_apis_mod.RFQAPIService, api_client=MagicMock())

    payload = service._transform_rfq_to_gmt_format(rfq_data, user_id="u1", org_id="o1")

    assert json.dumps(payload).isascii()
    remarks = [item["remarks"] for item in payload["rfqItem"]]
    assert remarks == ["12W, 6500K, B22", "10Kohm, 1/4W, +/-5%, Through Hole",
                       "1000uF, 25V, Electrolytic"]
    # Sanitizing must not disturb anything else about the payload.
    assert payload["rfqItem"][0]["brand"] == "Philips"
    assert payload["rfqItem"][1]["quantity"] == "200"
    assert payload["clientdeliverylocationrfq"] == [
        {"state": "Karnataka", "city": "Bangalore", "pincode": "560021"}
    ]


@pytest.mark.asyncio
async def test_create_rfq_logs_the_upstream_detail_on_rejection(caplog):
    """`message` is always the generic text; the detail lives in errorMsg."""
    api_client = SimpleNamespace(post=AsyncMock(return_value={
        "statusCode": "500",
        "message": "Failed to create RFQ",
        "errorMsg": ["Error occurred while creating RFQ"],
        "status": "Failure",
        "data": {},
        "success": True,
    }))
    service = bare(rfq_apis_mod.RFQAPIService, api_client=api_client)

    with caplog.at_level("ERROR", logger="app.procucev_apis.rfq_apis"):
        result = await service.create_rfq(
            {"items": [{"description": "LED Bulb", "quantity": 100}]}, user_id="u1", org_id="o1"
        )

    assert result == {"success": False, "error": "GMT API error: Failed to create RFQ"}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Error occurred while creating RFQ" in logged
    assert "statusCode=500" in logged


# ---------------------------------------------------------------------------
# Tool definitions and prompt placeholders
# ---------------------------------------------------------------------------

def test_every_tool_definition_uses_the_responses_api_shape():
    """A missing type or a nested "function" key is rejected with HTTP 400 at runtime."""
    tool_files = sorted(TOOLS_DIR.glob("*.json"))
    assert tool_files, "no tool definitions found"

    for path in tool_files:
        tool = json.loads(path.read_text(encoding="utf-8"))
        assert tool.get("type") == "function", f"{path.name} is missing type=function"
        assert tool.get("name"), f"{path.name} is missing a top-level name"
        assert "function" not in tool, f"{path.name} still uses the nested Chat Completions shape"
        assert isinstance(tool.get("parameters"), dict), f"{path.name} has no parameters schema"


def test_user_selection_tool_declares_the_fields_the_caller_reads():
    tool = json.loads((TOOLS_DIR / "user_selection_analysis.json").read_text(encoding="utf-8"))

    assert tool["name"] == "analyze_user_selection"
    properties = tool["parameters"]["properties"]
    # These are exactly the keys UserSelectionTool merges with its rule-based result.
    assert set(properties) == {
        "selected_option", "confidence", "reasoning",
        "alternative_matches", "requires_clarification", "register",
    }
    assert properties["selected_option"]["type"] == ["integer", "null"]
    assert properties["register"]["properties"]["type"]["enum"] == ["buyer", "seller", None]


def test_buyer_rfq_status_prompt_renders_its_followup_link(monkeypatch):
    """A KeyError here used to swap in the seller prompt, so buyers got seller answers."""
    import app.services.openai_service as openai_mod

    service = bare(
        openai_mod.OpenAIService,
        prompts_dir=str(Path(__file__).resolve().parents[2] / "app" / "prompts"),
        settings=SimpleNamespace(
            support_email="support@procucev.com",
            support_contact_info="help@procucev.com",
            PROCUCEV_PORTAL_URL="https://portal.example",
            rfq_followup_note="https://portal.example/login",
        ),
    )

    prompt = service._load_prompt("response_generation", "_get_buyer_rfq_status_response_prompt")

    assert "https://portal.example/login" in prompt
    assert "{followup_note}" not in prompt
    assert "Error loading prompt" not in prompt
    assert "response for the seller" not in prompt


def test_unknown_placeholder_keeps_the_requested_prompt(tmp_path, caplog):
    """An unsatisfied placeholder must not substitute an unrelated set of instructions."""
    import app.services.openai_service as openai_mod

    category = tmp_path / "response_generation"
    category.mkdir()
    (category / "probe_prompt.txt").write_text("Answer as {nobody_supplies_this}.", encoding="utf-8")
    service = bare(
        openai_mod.OpenAIService,
        prompts_dir=str(tmp_path),
        settings=SimpleNamespace(
            support_email="s@example.com", support_contact_info="h@example.com",
            PROCUCEV_PORTAL_URL="https://portal", rfq_followup_note="https://portal/login",
        ),
    )

    with caplog.at_level("ERROR", logger="app.services.openai_service"):
        prompt = service._load_prompt("response_generation", "probe_prompt")

    assert prompt == "Answer as {nobody_supplies_this}."
    assert "nobody_supplies_this" in "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Webhook client disconnects are not application errors
# ---------------------------------------------------------------------------

class DisconnectingRequest:
    """A request whose body never arrives, as starlette reports a hung-up sender."""

    def __init__(self):
        self.headers = {}
        self.query_params = {}
        self.client = SimpleNamespace(host="100.100.1.27")
        self.url = SimpleNamespace(path="/webhook/whatsapp")
        self.method = "POST"

    async def body(self):
        raise webhook_mod.ClientDisconnect()


@pytest.mark.asyncio
async def test_client_disconnect_is_acknowledged_without_an_error(monkeypatch, caplog):
    background = MagicMock()
    parse = AsyncMock()
    monkeypatch.setattr(webhook_mod, "parse_webhook_data", parse)
    cancel = AsyncMock()
    monkeypatch.setattr(webhook_mod, "handle_technical_error_with_cancel", cancel)

    with caplog.at_level("INFO", logger="app.api.webhook"):
        response = await webhook_mod.handle_webhook.__wrapped__(DisconnectingRequest(), background)

    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'
    background.add_task.assert_not_called()
    # No message arrived, so there is nothing to parse and no user to notify.
    parse.assert_not_awaited()
    cancel.assert_not_awaited()
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert any("client disconnected" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_other_webhook_failures_still_name_the_exception_type(monkeypatch, caplog):
    """An exception with an empty str() used to log a line ending in a bare colon."""
    class Silent(Exception):
        def __str__(self):
            return ""

    background = MagicMock()
    monkeypatch.setattr(webhook_mod, "parse_webhook_data", AsyncMock(side_effect=Silent()))
    monkeypatch.setattr(webhook_mod, "handle_technical_error_with_cancel", AsyncMock())
    request = SimpleNamespace(
        body=AsyncMock(return_value=b"replytype=TEXT&customernumber=918296753344"),
        query_params={},
        headers={},
        client=SimpleNamespace(host="100.100.1.27"),
        url=SimpleNamespace(path="/webhook/whatsapp"),
        method="POST",
    )

    with caplog.at_level("ERROR", logger="app.api.webhook"):
        response = await webhook_mod.handle_webhook.__wrapped__(request, background)

    assert response.status_code == 200
    assert any("Silent" in record.getMessage() for record in caplog.records)


# Guard against the sentinel constant drifting away from the prompt contract.
def test_sentinel_matches_the_extraction_prompt_contract():
    prompt = (
        Path(__file__).resolve().parents[2]
        / "app" / "prompts" / "entity_extraction"
        / "_get_entity_system_prompt_rfq_creation.txt"
    ).read_text(encoding="utf-8")
    assert NO_PRODUCTS_SENTINEL in prompt
    assert entity_mod.NO_PRODUCTS_SENTINEL == "NO_PRODUCTS_MENTIONED"
    assert intent_mod.RFQ_ENTRY_POINT_BUTTON_IDS is RFQ_ENTRY_POINT_BUTTON_IDS
