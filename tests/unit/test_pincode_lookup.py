from unittest.mock import MagicMock

import pytest
import requests

from app.utils import pincode_lookup as module


def test_get_fallback_location():
    # Valid pincode with known prefix
    res = module.get_fallback_location("110001")
    assert res == {"pincode": "110001", "city": "Delhi", "state": "Delhi"}
    assert module.get_location_from_pincode("110001") == {"pincode": "110001", "city": "Delhi", "state": "Delhi"}

    # Valid pincode with unknown prefix
    assert module.get_fallback_location("999999") is None

    # Invalid pincodes (non-digit or incorrect length)
    assert module.get_fallback_location("abc123") is None
    assert module.get_fallback_location("123") is None
    assert module.get_fallback_location("1234567") is None


def test_pincode_http_retry_and_failure(monkeypatch):
    response = MagicMock()
    response.json.return_value = [{"Status": "Success"}]
    calls = {"n": 0}

    def get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("offline")
        return response

    sleep_calls = []
    monkeypatch.setattr(module.requests, "get", get)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    assert module.get_pincode_details("411005", max_retries=2)[0]["Status"] == "Success"
    assert len(sleep_calls) == 1

    # Timeout retry loop
    timeout_calls = {"n": 0}

    def get_timeout(*args, **kwargs):
        timeout_calls["n"] += 1
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(module.requests, "get", get_timeout)
    assert module.get_pincode_details("411005", max_retries=2) is None
    assert len(sleep_calls) == 2

    monkeypatch.setattr(module.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.RequestException("bad")))
    assert module.get_pincode_details("411005") is None


PUNE_FALLBACK = {"pincode": "411005", "city": "Pune", "state": "Maharashtra"}


def test_api_gets_a_5s_timeout_and_one_retry(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(module.requests, "get", get)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.get_pincode_details("560045") is None
    assert calls == [5, 5]


@pytest.mark.asyncio
async def test_location_validation_and_mapping(monkeypatch):
    assert await module.get_location_from_pincode_async("123") is None

    # The API answered and does not know the pincode: rejected, no fallback
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Error", "PostOffice": []}])
    assert await module.get_location_from_pincode_async("411005") is None
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Success", "PostOffice": []}])
    assert await module.get_location_from_pincode_async("411005") is None

    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}])
    assert await module.get_location_from_pincode_async("411005") == {"pincode": "411005", "city": "Pune", "state": "MH"}


@pytest.mark.asyncio
@pytest.mark.parametrize("api_answer", [None, "not a list", [], ["not a dict"], [{"Status": "Success", "PostOffice": [{}]}]])
async def test_unusable_api_answer_falls_back_to_prefix(monkeypatch, api_answer):
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: api_answer)
    assert await module.get_location_from_pincode_async("411005") == PUNE_FALLBACK


@pytest.mark.asyncio
async def test_api_exception_falls_back_to_prefix(monkeypatch):
    def raise_exc(pin, **kwargs):
        raise ValueError("unexpected failure")

    monkeypatch.setattr(module, "get_pincode_details", raise_exc)
    assert await module.get_location_from_pincode_async("411005") == PUNE_FALLBACK
    assert await module.get_location_from_pincode_async("560045") == {
        "pincode": "560045", "city": "Bengaluru", "state": "Karnataka"
    }
    # Unknown prefix: nothing to fall back to, so the pincode is still rejected
    assert await module.get_location_from_pincode_async("999999") is None



