from unittest.mock import MagicMock

import pytest
import requests

from app.utils import pincode_lookup as module


def test_pincode_http_retry_and_failure(monkeypatch):
    response = MagicMock()
    response.json.return_value = [{"Status": "Success"}]
    calls = {"n": 0}

    def get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("offline")
        return response

    monkeypatch.setattr(module.requests, "get", get)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.get_pincode_details("411005", max_retries=2)[0]["Status"] == "Success"
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout()))
    assert module.get_pincode_details("411005", max_retries=1) is None
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.RequestException("bad")))
    assert module.get_pincode_details("411005") is None


@pytest.mark.asyncio
async def test_location_validation_and_mapping(monkeypatch):
    assert await module.get_location_from_pincode_async("123") is None
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: None)
    assert await module.get_location_from_pincode_async("411005") is None
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Error", "PostOffice": []}])
    assert await module.get_location_from_pincode_async("411005") is None
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}])
    assert await module.get_location_from_pincode_async("411005") == {"pincode": "411005", "city": "Pune", "state": "MH"}
    monkeypatch.setattr(module, "get_pincode_details", lambda pin, **kwargs: [{"Status": "Success", "PostOffice": [{}]}])
    assert await module.get_location_from_pincode_async("411005") is None


