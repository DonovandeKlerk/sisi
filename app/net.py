"""Outbound HTTP resilience: bounded retries with exponential backoff.

Retries transport errors and 5xx responses only. Client errors (4xx) fail fast:
retrying them cannot help and can double-send.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


def call_with_retries(func: Callable[[], T], *, attempts: int = 3,
                      base_delay: float = 0.5) -> T:
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return func()
        except httpx.HTTPStatusError as e:
            last = e
            status = e.response.status_code if e.response is not None else 0
            if status < 500:
                raise
        except httpx.TransportError as e:
            last = e
        if i < attempts - 1:
            time.sleep(base_delay * (2 ** i))
    assert last is not None
    raise last
