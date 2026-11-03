from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from fintonic.http import build_http_client
from fintonic.session import Session

API = "https://api.fintonic.com/finapi"
BFF = "https://fintonic-pfm-bff.fintonic.com"
DEVICE = "11111111-2222-3333-4444-555555555555"


def make_jwt(expires_in: timedelta = timedelta(minutes=30)) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    exp = int((datetime.now(tz=UTC) + expires_in).timestamp())
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'userId': '1'})}.sig"


def make_tx(i: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "bankId": "0182",
        "systemBankId": "0182",
        "active": True,
        "id": f"tx{i:04d}",
        "type": "ACCOUNT",
        "productId": "p" * 64,
        "description": f"Comercio {i}",
        "reference": "",
        "note": None,
        "userDescription": None,
        "read": False,
        "quantity": -1234 - i,
        "currency": "EURO",
        "baseQuantity": -1234 - i,
        "baseCurrency": "EURO",
        "valueDate": "2026-10-30",
        "userDate": "2026-10-30",
        "operationDate": "2026-10-29",
        "cleanNote": "",
        "primaryDisplay": f"Comercio {i}",
        "secondaryDisplay": "",
        "categorization": {
            "weightedCategories": {"G0898": 100},
            "classified": True,
            "categorizedByUserRule": False,
            "categorizedByClient": False,
            "categorizedBySystem": True,
            "splited": False,
        },
    }
    return {**base, **overrides}


@pytest.fixture
def session() -> Session:
    return Session(
        email="user@example.com",
        access_token=make_jwt(),
        device_uuid=DEVICE,
        device_verified=True,
    )


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with build_http_client(retries=3, backoff=0) as client:
        yield client
