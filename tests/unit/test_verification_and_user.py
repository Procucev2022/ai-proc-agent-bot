from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.user import User, UserRole, normalize_phone_number
from app.services.verification_check_service import VerificationCheckService


@pytest.fixture
def verification_service():
    return VerificationCheckService(AsyncMock(), AsyncMock(), AsyncMock())


@pytest.mark.asyncio
async def test_verification_access_and_otp_paths(verification_service):
    verification_service.otp_service.send_otp.return_value = {"status": "otp_sent"}
    buyer = {"verificationStatus": "EMAIL_VERIFIED", "selfClient": True, "approved": True, "username": "b@example.com"}
    assert (await verification_service.check_and_enforce_verification("1", buyer, object()))["access_granted"]
    pending = {"verificationStatus": "EMAIL_VERIFIED", "selfClient": True, "approved": False, "username": "b@example.com"}
    assert (await verification_service.check_and_enforce_verification("1", pending, object()))["redirect_to_support"]
    pending["verificationStatus"] = "PENDING_EMAIL_VERIFICATION"
    assert (await verification_service.check_and_enforce_verification("1", pending, object()))["otp_sent"]
    pending["verificationStatus"] = "EMAIL_VERIFICATION_FAILED"
    assert (await verification_service.check_and_enforce_verification("1", pending, object()))["verification_required"]
    seller = {"verification_status": "EMAIL_VERIFIED", "self_client": False, "email": "s@example.com"}
    result = await verification_service.check_and_enforce_verification("1", seller, object())
    assert result["redirect_info"]["reason"] == "seller_authentication"
    unknown_data = {"fullName": "A", "email": "e", "verificationStatus": "SOMETHING_ELSE"}
    unknown = await verification_service.check_and_enforce_verification("1", unknown_data, object())
    assert unknown["redirect_info"]["reason"] == "unknown_status"

    verification_service.otp_service.send_otp.side_effect = RuntimeError("otp")
    assert (await verification_service._send_verification_otp("1", "e", object()))["status"] == "otp_send_failed"
    assert (await verification_service._send_verification_otp("1", None))["reason"] == "invalid_email"
    bad = await verification_service.check_and_enforce_verification("1", object(), object())
    assert bad["verification_required"] and bad["redirect_info"]["reason"] == "verification_check_error"


@pytest.mark.asyncio
async def test_verification_refresh_and_domain_approval(monkeypatch, verification_service):
    verification_service.auth_api_service.authenticate_user.side_effect = [{"success": False}, {"success": True, "data": [1]}]
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    assert (await verification_service.refresh_user_verification_status("1", 2))["success"]
    verification_service.auth_api_service.authenticate_user.return_value = {"success": False}
    assert (await verification_service.refresh_user_verification_status("1", 1))["success"] is False
    verification_service.auth_api_service.authenticate_user.side_effect = RuntimeError("auth")
    assert "auth" in (await verification_service.refresh_user_verification_status("1"))["message"]

    assert (await verification_service._check_domain_approval("u", True))["status"] == "already_approved"
    assert (await verification_service._check_domain_approval("u", False, None))["status"] == "missing_user_data"
    assert (await verification_service._check_domain_approval("u", False, {"email": "x"}))["status"] == "missing_domain_data"
    fake_domain = SimpleNamespace(check_domain_match=AsyncMock(return_value={"approved": True, "method": "ai"}), user_approval_api_call=AsyncMock(return_value={"approved": True}))
    monkeypatch.setattr("app.services.domain_check_service.DomainCheckService", lambda: fake_domain)
    assert (await verification_service._check_domain_approval("u", False, {"email": "x", "companyName": "X"}))["approved"]
    fake_domain.check_domain_match.return_value = {"approved": False, "method": "fallback", "reasoning": "no"}
    assert (await verification_service._check_domain_approval("u", False, {"email": "x", "companyName": "X"}))["status"] == "ai_domain_rejected"
    fake_domain.check_domain_match.side_effect = RuntimeError("domain")
    assert "domain" in (await verification_service._check_domain_approval("u", False, {"email": "x", "companyName": "X"}))["error"]


def test_user_normalization_and_conversion_branches():
    assert normalize_phone_number("") == ""
    assert normalize_phone_number("+919876543210") == "+919876543210"
    assert normalize_phone_number("919876543210") == "+919876543210"
    assert normalize_phone_number("98765 43210") == "+919876543210"
    assert normalize_phone_number("123") == "+91123"
    buyer = User.from_api_response({"userId": 3, "selfClient": True, "username": "x", "phone": "p"})
    assert buyer.id == "3" and buyer.role is UserRole.BUYER and buyer.is_registered
    seller = User.from_api_response({"id": "s", "selfClient": False})
    assert seller.role is UserRole.SELLER and seller.verification_status == "PENDING_EMAIL_VERIFICATION"
    unknown = User.from_api_response({"id": "u"})
    assert unknown.role is UserRole.UNKNOWN
    with pytest.raises(ValueError):
        User.from_api_response({})
    existing = User(id="x")
    assert User.from_mixed_data(existing) is existing
    assert User.from_mixed_data({"name": "N", "email": "e", "id": "x"}).is_registered
    assert User.from_mixed_data({"id": "x", "selfClient": True}).id == "x"
    assert User.invalid_user("1").phone_number == "1"
    with pytest.raises(ValueError):
        User.from_mixed_data(None)
    with pytest.raises(ValueError):
        User.from_mixed_data(3)


def test_user_registration_validation_errors():
    from app.schemas.user import BuyerRegistrationSchema, SellerRegistrationSchema

    with pytest.raises(ValidationError):
        BuyerRegistrationSchema(name="A1", companyName="x", email="bad", zipCode="1")
    valid_buyer = BuyerRegistrationSchema(name="john doe", companyName="acme", email="J@EXAMPLE.COM", zipCode="411005")
    assert valid_buyer.name == "John Doe" and valid_buyer.email == "j@example.com"
    with pytest.raises(ValidationError):
        SellerRegistrationSchema(name="A", companyName="x", email="x@y.com", address1="a", zipCode="411005", gstin="bad", details="d")
