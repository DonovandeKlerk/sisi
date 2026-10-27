"""Retry helper: transport errors and 5xx retry, 4xx fails fast."""

import httpx
import pytest

from app.net import call_with_retries


def _status_error(status):
    req = httpx.Request("POST", "https://x.test")
    resp = httpx.Response(status, text="boom", request=req)
    return httpx.HTTPStatusError("e", request=req, response=resp)


def test_retries_then_succeeds():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return "ok"

    assert call_with_retries(flaky, base_delay=0) == "ok"
    assert len(calls) == 3


def test_client_error_fails_fast():
    calls = []

    def bad():
        calls.append(1)
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        call_with_retries(bad, base_delay=0)
    assert len(calls) == 1


def test_server_error_gives_up_after_three():
    calls = []

    def down():
        calls.append(1)
        raise _status_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        call_with_retries(down, base_delay=0)
    assert len(calls) == 3
