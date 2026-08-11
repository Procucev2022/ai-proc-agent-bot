from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.otp_service import OTPService


def make_otp():
    register = AsyncMock()
    whatsapp = AsyncMock()
    support = AsyncMock()
    service = OTPService(register, whatsapp, support)
    return service, register, whatsapp, support


@pytest.mark.asyncio
async def test_otp_send_validate_and_message_paths():
    service, register, whatsapp, support = make_otp()
    session = SimpleNamespace(workflow_state={})
    register.send_otp.return_value = {"statusCode": "200"}
    assert (await service.send_otp("1", "e", session))["status"] == "otp_sent"
    assert session.workflow_state["otp_email"] == "e"
    register.send_otp.return_value = {"status": "Success"}
    assert (await service.send_otp("1", "e", session, is_daily_verification=True, send_notification=False))["status"] == "otp_sent"
    register.send_otp.return_value = {"statusCode": "400", "message": "email not exists"}
    assert (await service.send_otp("1", "e", session))["reason"] == "email_not_exists"
    register.send_otp.return_value = {"statusCode": "400", "message": "bad"}
    assert (await service.send_otp("1", "e", session))["reason"] == "otp_send_failed"
    register.send_otp.side_effect = RuntimeError("send")
    assert (await service.send_otp("1", "e", session))["status"] == "redirect_to_support"

    session.workflow_state = {}
    assert (await service.validate_otp("1", session, "1"))["status"] == "restart_authentication"
    session.workflow_state = {"otp_email": "e", "otp_retry_count": 1}
    register.validate_otp.return_value = {"statusCode": "200"}
    assert (await service.validate_otp("1", session, "1234"))["status"] == "otp_valid"
    session.workflow_state = {"otp_email": "e", "otp_retry_count": 0}
    register.validate_otp.return_value = {"statusCode": "400"}
    assert (await service.validate_otp("1", session, "1234"))["status"] == "otp_invalid"
    session.workflow_state["otp_retry_count"] = 2
    assert (await service.validate_otp("1", session, "1234"))["status"] == "max_otp_exceeded"
    register.validate_otp.side_effect = RuntimeError("validate")
    assert (await service.validate_otp("1", session, "1234"))["status"] == "redirect_to_support"

    service.send_otp = AsyncMock(return_value={"status": "otp_sent"})
    session.workflow_state = {"otp_email": "e", "otp_retry_count": 0}
    assert (await service.handle_user_message("1", " RESEND ", session))["status"] == "otp_sent"
    assert service._extract_otp("code 1234") == "1234"
    assert service._extract_otp("no digits") == ""
    assert (await service.handle_user_message("1", "no digits", session))["status"] == "otp_format_invalid"
    session.workflow_state["otp_retry_count"] = 2
    assert (await service.handle_user_message("1", "bad", session))["status"] == "max_otp_exceeded"
