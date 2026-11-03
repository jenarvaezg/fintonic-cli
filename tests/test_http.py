import httpx
import respx

from fintonic.http import build_http_client
from tests.conftest import API


def test_get_is_retried_on_transient_errors(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}/rest/category/tree").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": True})]
    )
    response = http.get(f"{API}/rest/category/tree")
    assert response.status_code == 200
    assert route.call_count == 3


def test_get_is_retried_on_connection_errors(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}/rest/user/profile").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )
    assert http.get(f"{API}/rest/user/profile").status_code == 200
    assert route.call_count == 2


def test_get_gives_up_after_max_retries(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}/rest/x").mock(return_value=httpx.Response(500))
    assert http.get(f"{API}/rest/x").status_code == 500
    assert route.call_count == 4  # 1 + 3 retries


def test_login_post_is_never_retried(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(return_value=httpx.Response(503))
    assert http.post(f"{API}/oidc/token", json={}).status_code == 503
    assert route.call_count == 1


def test_sms_put_is_never_retried(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(503))
    assert http.put(f"{API}/rest/device/provisions", json={}).status_code == 503
    assert route.call_count == 1


def test_client_errors_are_not_retried(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}/rest/x").mock(return_value=httpx.Response(400))
    http.get(f"{API}/rest/x")
    assert route.call_count == 1


def test_default_headers_mimic_web_app(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.get(f"{API}/rest/x").mock(return_value=httpx.Response(200))
    http.get(f"{API}/rest/x")
    sent = route.calls.last.request.headers
    assert sent["accept"] == "application/vnd.fintonic-v7+json"
    assert sent["psd2_support"] == "true"
    assert "Chrome" in sent["user-agent"]


def test_default_timeout_matches_android_app() -> None:
    with build_http_client() as client:
        assert client.timeout == httpx.Timeout(60.0)
