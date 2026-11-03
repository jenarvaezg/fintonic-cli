import httpx
import pytest

from fintonic.errors import (
    ApiError,
    NetworkBlockedError,
    SessionExpiredError,
    extract_error_message,
    raise_for_response,
)

URL = "https://api.fintonic.com/finapi/rest/x"


def _resp(status: int, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", URL), **kwargs)


def test_nested_error_envelope() -> None:
    body = {"data": None, "error": {"errorCode": "401", "error": "Invalid Token"}}
    assert extract_error_message(_resp(400, json=body)) == "Invalid Token"


def test_invalid_token_maps_to_session_expired() -> None:
    with pytest.raises(SessionExpiredError):
        raise_for_response(_resp(400, json={"error": {"errorCode": "401", "error": "Invalid Token"}}))


@pytest.mark.parametrize(
    "response",
    [
        _resp(403, text="<html>Attention Required</html>", headers={"content-type": "text/html; charset=UTF-8"}),
        _resp(403, json={}, headers={"cf-mitigated": "challenge"}),
        _resp(503, text="<html></html>", headers={"content-type": "text/html"}),
    ],
)
def test_cloudflare_block(response: httpx.Response) -> None:
    with pytest.raises(NetworkBlockedError, match="Cloudflare"):
        raise_for_response(response)


def test_api_json_403_is_not_cloudflare() -> None:
    with pytest.raises(ApiError) as exc_info:
        raise_for_response(_resp(403, json={"error": "Forbidden"}))
    assert exc_info.value.message == "Forbidden"


def test_non_json_body() -> None:
    assert extract_error_message(_resp(500, text="boom")) == "boom"
