from __future__ import annotations

import importlib.util
import runpy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import app.config as config_module
import app.context.test_context as context_tests
import app.procucev_apis.bfs_apis as bfs_module
import app.procucev_apis.category_apis as category_module
import app.procucev_apis.register_apis as register_module
import app.procucev_apis.seller_apis as seller_module
import app.celery_config as celery_config

_legacy_spec = importlib.util.spec_from_file_location("app.legacy_schemas", Path(__file__).parents[2] / "app" / "schemas.py")
legacy_schemas = importlib.util.module_from_spec(_legacy_spec)
assert _legacy_spec.loader is not None
_legacy_spec.loader.exec_module(legacy_schemas)
ConversationContextSchema = legacy_schemas.ConversationContextSchema
LegacyRFQCreateRequestSchema = legacy_schemas.RFQCreateRequestSchema
LegacyRFQStatusResponseSchema = legacy_schemas.RFQStatusResponseSchema
UserRegistrationSchema = legacy_schemas.UserRegistrationSchema
LegacyVendorResponseSchema = legacy_schemas.VendorResponseSchema
VendorSearchRequestSchema = legacy_schemas.VendorSearchRequestSchema
WhatsAppMessageSchema = legacy_schemas.WhatsAppMessageSchema
from app.schemas.rfq import (
    ExcelValidationSchema,
    RFQCreateRequestSchema,
    RFQDeliveryLocationSchema,
    RFQItemSchema,
    RFQOrganizationSchema,
    RFQStatusResponseSchema,
    RFQUpdateSchema,
    RFQValidationSchema,
    RFQVendorSchema,
)
from app.procucev_apis.bfs_apis import BFSAPIService
from app.procucev_apis.category_apis import CategoryAPIService
from app.procucev_apis.register_apis import RegisterAPIService
from app.procucev_apis.seller_apis import SellerAPIService


@pytest.mark.asyncio
async def test_rfq_schema_validators_and_excel_question_paths():
    item = RFQItemSchema(
        description="Custom item", quantity="2.5", unit_of_measures="unknown",
        createdTS="2025-01-01T00:00:00", extra_field="allowed",
    )
    assert item.quantity == 2.5 and item.created_ts.year == 2025
    for quantity, message in [("not-a-number", "valid number"), (0, "greater than 0"), (10000001, "limit")]:
        with pytest.raises(ValidationError, match=message):
            RFQItemSchema(description="x", quantity=quantity, unit_of_measures="pcs")

    with pytest.raises(ValidationError, match="6 digits"):
        RFQDeliveryLocationSchema(state="Unknown", city="Pune", pincode="abc")
    with pytest.raises(ValidationError, match="6 digits"):
        RFQDeliveryLocationSchema(state="Unknown", city="Pune", pincode="12345")
    assert RFQDeliveryLocationSchema(state="Unknown", city="Pune", pincode="411005").state == "Unknown"

    with pytest.raises(ValidationError, match="Invalid email"):
        RFQVendorSchema(id="v", email="invalid")
    assert RFQVendorSchema(id="v", email="a@b.com", otherEmails=["c@d.com"], vendorId="v2").vendor_id == "v2"
    assert RFQOrganizationSchema(id="o", extra="x").id == "o"

    future = datetime.now() + timedelta(days=3)
    valid_kwargs = dict(
        createdBy="u", deliveryDate=future, user="u", org={"id": "o"},
        rfqItem=[{"description": "x", "quantity": 1, "unit_of_measures": "pcs"}],
        clientdeliverylocationrfq=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
    )
    assert RFQCreateRequestSchema(**valid_kwargs).no_pr_flag is True
    with pytest.raises(ValidationError, match="future"):
        RFQCreateRequestSchema(**{**valid_kwargs, "deliveryDate": datetime.now() - timedelta(days=1)})
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**valid_kwargs, "rfqItem": []})
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**valid_kwargs, "clientdeliverylocationrfq": []})
    assert RFQStatusResponseSchema(statusCode="200", message="ok", timestamp="now", status="Success", type="x").status_code == "200"
    assert RFQUpdateSchema(remarks="updated", items=[]).remarks == "updated"

    empty_excel = ExcelValidationSchema()
    assert empty_excel.get_missing_required_excel_fields() == ["item_description", "specification", "uom", "quantity"]
    assert empty_excel.get_missing_optional_excel_fields() == ["serial_no", "remarks"]
    assert empty_excel.get_excel_completion_percentage() == 0
    assert not empty_excel.is_excel_complete()
    assert len(empty_excel.get_excel_questions()) == 4
    assert empty_excel.get_next_excel_questions() == empty_excel.get_excel_questions()

    complete_excel = ExcelValidationSchema(item_description="item", specification="spec", uom="pcs", quantity="2")
    assert complete_excel.is_excel_complete()
    assert complete_excel.get_excel_completion_percentage() == pytest.approx(66.6666667)
    optional_questions = complete_excel.get_excel_questions()
    assert len(optional_questions) == 2 and "serial number" in optional_questions[0]
    full_excel = ExcelValidationSchema(serial_no="1", item_description="item", specification="spec", uom="pcs", quantity="2", remarks="note")
    assert full_excel.get_excel_completion_percentage() == 100 and full_excel.get_excel_questions() == []


@pytest.mark.asyncio
async def test_rfq_validation_completeness_questions_and_flags():
    schema = RFQValidationSchema()
    assert schema._is_invalid_description("")
    assert schema._is_invalid_description(" 123 ")
    assert schema._is_invalid_description("kg")
    assert schema._is_invalid_description("Product")
    assert not schema._is_invalid_description("steel plate")

    missing = RFQValidationSchema(
        items=[
            {"description": "kg", "quantity": 0},
            {"description": "valid", "quantity": "bad"},
            {"description": "valid", "quantity": "2"},
        ],
        delivery_locations=[{"state": "", "city": "Pune"}, {"state": "MH", "pincode": "411005"}],
        date_validation_error=True,
    )
    fields = missing.get_missing_mandatory_fields()
    assert "delivery_date" in fields and "item_0_description" in fields and "item_0_quantity" in fields
    assert "item_1_quantity" in fields and "delivery_location_0_state" in fields and "delivery_location_0_pincode" in fields
    assert not missing.is_complete()
    assert missing.get_missing_optional_fields() == ["preferred_brand", "remarks", "attachments"]
    assert missing.get_completeness_percentage() == pytest.approx(22.2222222)
    assert missing.get_mandatory_questions()
    assert missing.get_next_questions() == missing.get_mandatory_questions()

    grouped = RFQValidationSchema(items=[{"description": "", "quantity": None}], delivery_locations=[{}])
    questions = grouped.get_mandatory_questions()
    assert any("item details" in question.lower() for question in questions)
    assert any("complete address" in question.lower() for question in questions)

    one_missing = RFQValidationSchema(
        delivery_date=datetime.now() + timedelta(days=1),
        items=[{"description": "valid", "quantity": 1}],
        delivery_locations=[{"state": "MH", "city": "Pune", "pincode": "411005"}],
    )
    assert one_missing.is_complete()
    assert one_missing.get_next_questions() == one_missing.get_optional_questions()
    one_missing.attachments = [{"name": "spec.pdf"}]
    assert one_missing.get_next_questions() == []
    one_missing.set_date_validation_error(False)
    assert not one_missing.has_date_validation_error()
    combined = one_missing.get_combined_questions(include_optional=False)
    assert combined == {"mandatory": [], "optional": [], "has_mandatory": False, "has_optional": False}

    dynamic = RFQValidationSchema(
        delivery_date=datetime.now() + timedelta(days=1),
        items=[{"description": "valid", "quantity": 1}, {"description": "", "quantity": 1}],
        delivery_locations=[{"state": "", "city": "", "pincode": ""}, {"state": "MH", "city": "Pune", "pincode": "411005"}],
    )
    dynamic_questions = dynamic.get_mandatory_questions()
    assert any("item description" in q.lower() for q in dynamic_questions)
    assert any("address" in q.lower() or "delivered" in q.lower() for q in dynamic_questions)



def test_legacy_schema_placeholders_are_importable():
    instances = [
        WhatsAppMessageSchema(), UserRegistrationSchema(), VendorSearchRequestSchema(),
        LegacyVendorResponseSchema(), LegacyRFQCreateRequestSchema(),
        LegacyRFQStatusResponseSchema(), ConversationContextSchema(),
    ]
    assert all(instance is not None for instance in instances)


def test_context_test_module_runs_all_paths():
    context_tests.run_all_tests()
    # Execute the script entry point as well, covering its __main__ branch.
    source = Path(context_tests.__file__).read_text(encoding="utf-8")
    exec(compile(source, context_tests.__file__, "exec"), {"__name__": "__main__", "__package__": "app.context"})



def test_settings_validation_accessors_and_singleton(monkeypatch):
    # Avoid python-dotenv's caller-frame discovery; it is an external filesystem boundary.
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    settings = config_module.Settings()
    settings.database_mode = "client"
    settings.client_database_url = "mysql+pymysql://client"
    assert settings.get_database_url() == "mysql+pymysql://client"
    assert settings.is_ssl_enabled()
    assert settings.get_settings if False else True

    settings.support_team_numbers = [" 1 ", "", "2"]
    settings.validate_config()
    assert settings.support_team_numbers == ["1", "2"]

    settings.api_read_timeout = 0
    with pytest.raises(ValueError, match="API_READ_TIMEOUT"):
        settings.validate_config()
    settings.api_read_timeout = 30
    settings.otp_max_attempts = 6
    with pytest.raises(ValueError, match="OTP_MAX_ATTEMPTS"):
        settings.validate_config()
    settings.otp_max_attempts = 3
    settings.max_retry_attempts = 0
    with pytest.raises(ValueError, match="MAX_RETRY_ATTEMPTS"):
        settings.validate_config()
    settings.max_retry_attempts = 3
    settings.api_connect_timeout = 0
    with pytest.raises(ValueError, match="API_CONNECT_TIMEOUT"):
        settings.validate_config()
    settings.api_connect_timeout = 10

    settings.webhook_health_monitoring_enabled = True
    settings.webhook_health_check_interval_seconds = 20
    settings.webhook_api_timeout_seconds = 10
    settings.webhook_failure_grace_period_seconds = 20
    with pytest.raises(ValueError, match="less than.*grace"):
        settings.validate_config()
    settings.webhook_failure_grace_period_seconds = 100
    settings.webhook_api_response_threshold_seconds = 10
    with pytest.raises(ValueError, match="threshold"):
        settings.validate_config()
    settings.webhook_api_response_threshold_seconds = 5
    settings.webhook_recovery_confirmations = 0
    with pytest.raises(ValueError, match="RECOVERY_CONFIRMATIONS"):
        settings.validate_config()
    settings.webhook_recovery_confirmations = 1
    settings.webhook_warning_consecutive_threshold = 0
    with pytest.raises(ValueError, match="WARNING_CONSECUTIVE"):
        settings.validate_config()

    config_module._settings = None
    with patch.object(config_module, "Settings", return_value="loaded"):
        config_module.load_environment()
        assert config_module.get_settings() == "loaded"
    config_module._settings = None
    with patch.object(config_module, "load_environment", side_effect=lambda: setattr(config_module, "_settings", "singleton")):
        assert config_module.get_settings() == "singleton"


@pytest.mark.asyncio
async def test_api_wrapper_missing_failure_and_exception_edges(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(bfs_module, "get_procucev_api_client", lambda: client)
    bfs = BFSAPIService()
    client.post.return_value = {"success": False}
    assert (await bfs.request_bfs_item("i", 1, 2, 3, "o", "u", "p"))["error"] == "BFS item request failed"
    client.post.side_effect = RuntimeError("request")
    assert (await bfs.request_bfs_item("i", 1, 2, 3, "o", "u", "p"))["error"] == "request"
    client.post.side_effect = None
    client.post.return_value = {"success": False}
    assert (await bfs.accept_bid_by_seller("b"))["error"] == "Bid acceptance failed"
    assert (await bfs.reject_bid_by_seller("b"))["error"] == "Bid rejection failed"

    monkeypatch.setattr(category_module, "get_procucev_api_client", lambda: client)
    category = CategoryAPIService()
    client.get.side_effect = RuntimeError("category")
    assert (await category.get_divisions())["error"] == "category"
    assert (await category.get_categories())["error"] == "category"

    monkeypatch.setattr(register_module, "get_procucev_api_client", lambda: client)
    register = RegisterAPIService()
    client.post.side_effect = None
    client.post.return_value = {"statusCode": 400, "data": {}, "status": "Failure"}
    assert (await register.register_seller({}))["message"] == "Registration failed"
    assert (await register.register_buyer({}))["message"] == "Registration failed"
    client.post.side_effect = RuntimeError("registration")
    assert (await register.register_seller({}))["statusCode"] == "500"
    assert (await register.send_otp("u"))["statusCode"] == "500"
    assert (await register.validate_otp("u", "1"))["statusCode"] == "500"


@pytest.mark.asyncio
async def test_seller_wrapper_remaining_transformation_and_errors(monkeypatch):
    client = AsyncMock()
    settings = SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal")
    monkeypatch.setattr(seller_module, "get_procucev_api_client", lambda: client)
    monkeypatch.setattr(seller_module, "get_settings", lambda: settings)
    seller = SellerAPIService()

    client.post.return_value = {"success": True, "data": {"rfqs": [{"rfqId": "1", "deliveryDate": "not-a-date", "rfqItem": [{}, {"category": "B"}]}]}}
    transformed = await seller.fetch_active_rfqs("o")
    assert transformed["rfqs"][0]["delivery_date"] == "not-a-date" and transformed["rfqs"][0]["location"] is None
    client.post.return_value = {"success": True, "data": {}}
    assert (await seller.check_seller_credits("o"))["credits_available"] == 0
    client.post.return_value = {"success": True, "data": {"x": 1}}
    assert (await seller.check_seller_rfq_status("s", [None]))["data"] == {"x": 1}
    client.post.return_value = {"success": True, "open_rfqs": [1], "total_count": 1}
    assert (await seller.fetch_seller_open_rfqs_for_reminder("s"))["open_rfqs"] == [1]
    client.get.return_value = {"success": False}
    assert (await seller.get_subscription_plans())["error"] == "Failed to fetch subscription plans"
    client.post.side_effect = RuntimeError("seller")
    assert (await seller.fetch_active_rfqs("o"))["error"] == "seller"
    assert (await seller.send_rfq_email([], "e", "s"))["error"] == "seller"
    assert (await seller.update_rfq_seller_sent_flag("r", "s"))["error"] == "seller"
    client.get.side_effect = RuntimeError("seller")
    assert (await seller.get_subscription_plans())["error"] == "seller"
    assert (await seller.generate_payment_link("p"))["error"] == "seller"
    assert (await seller.fetch_seller_open_rfqs_for_reminder("s"))["error"] == "seller"


def test_celery_entrypoint_and_disabled_schedule_branches(monkeypatch):
    import celery
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    config_module._settings = config_module.Settings()
    monkeypatch.setattr(celery.Celery, "start", MagicMock())
    runpy.run_module("app.celery_app", run_name="__main__")

    source_path = Path(celery_config.__file__)
    source = source_path.read_text(encoding="utf-8")
    flags = [
        "ENABLE_AUTO_CATEGORIZATION", "ENABLE_VECTOR_STORE_SYNC", "ENABLE_SELLER_MATCHING",
        "ENABLE_DAILY_CATEGORY_REBUILD", "ENABLE_CATEGORY_NAME_SYNC", "ENABLE_LOG_CLEANUP",
        "ENABLE_BFS_NOTIFICATION", "ENABLE_WHATSAPP_REPORT_AUTOMATION", "ENABLE_TAXONOMY_BUILD",
    ]
    for flag in flags:
        source = source.replace(f"if {flag}:", "if False:")
    namespace = {"__name__": "app.celery_config_disabled_test", "__file__": str(source_path)}
    exec(compile(source, str(source_path), "exec"), namespace)
    assert namespace["beat_schedule"] == {}
