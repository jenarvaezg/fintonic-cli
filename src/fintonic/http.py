"""HTTP client factory with retries on by default.

Only idempotent methods are retried: login (POST) and the device SMS/verification
calls (PUT) must never be replayed, or we would burn PIN attempts and SMS quota.
"""

from __future__ import annotations

import httpx
from httpx_retries import Retry, RetryTransport

from fintonic.config import DEFAULT_HEADERS

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DEFAULT_RETRIES = 4
DEFAULT_BACKOFF = 0.5  # 0.5s, 1s, 2s, 4s (+ jitter)
DEFAULT_TIMEOUT = 30.0


def build_retry(retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> Retry:
    return Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=RETRY_METHODS,
        respect_retry_after_header=True,
        max_backoff_wait=20.0,
    )


def build_http_client(
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Client:
    transport = RetryTransport(transport=httpx.HTTPTransport(), retry=build_retry(retries, backoff))
    return httpx.Client(transport=transport, timeout=timeout, headers=DEFAULT_HEADERS)
