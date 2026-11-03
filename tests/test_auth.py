import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import respx

from fintonic.auth import Authenticator, stored_pin_relogin
from fintonic.errors import (
    AuthError,
    BlockedUserError,
    DeviceVerificationRequired,
    InvalidCredentialsError,
    NetworkBlockedError,
    SessionExpiredError,
    SmsLimitExceeded,
)
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, DEVICE, make_jwt


def _token_response(device: str = DEVICE) -> httpx.Response:
    body = {
        "data": {
            "access_token": make_jwt(),
            "refresh_token": make_jwt(),
            "token_type": "Bearer",
            "expires_in": 1800,
            "error": None,
            "error_description": None,
        },
        "error": None,
    }
    return httpx.Response(200, json=body, headers={"device-uuid": device})


def test_first_login_requires_device_verification(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response())

    result = Authenticator(http).login("user@example.com", "1234")

    assert json.loads(route.calls.last.request.content) == {
        "username": "user@example.com",
        "password": "1234",
        "deviceUUID": None,
    }
    assert result.needs_device_verification
    assert result.session.device_uuid == DEVICE
    assert result.session.access_token


def test_login_with_verified_device_skips_sms(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response())

    result = Authenticator(http).login(session.email, "1234", previous=session)

    assert json.loads(route.calls.last.request.content)["deviceUUID"] == DEVICE
    assert not result.needs_device_verification
    assert result.session.device_verified


def test_device_of_another_email_is_not_reused(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response("other-device"))

    result = Authenticator(http).login("other@example.com", "1234", previous=session)

    assert json.loads(route.calls.last.request.content)["deviceUUID"] is None
    assert result.needs_device_verification


def test_wrong_pin_raises_auth_error(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(AuthError, match="incorrectos"):
        Authenticator(http).login("user@example.com", "0000")


def test_error_inside_data_raises(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    body = {"data": {"error": "invalid_grant", "error_description": "Invalid user credentials"}}
    mock_api.post(f"{API}/oidc/token").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AuthError, match="Invalid user credentials"):
        Authenticator(http).login("user@example.com", "0000")


def test_blocked_user(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=httpx.Response(403, json={"error": "Blocked user"}))
    with pytest.raises(BlockedUserError, match="bloqueado"):
        Authenticator(http).login("user@example.com", "1234")


def test_sms_flow(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    unverified = Session(session.email, session.access_token, DEVICE, False)
    provision = mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(202))
    verify = mock_api.put(f"{API}/rest/device/verification").mock(return_value=httpx.Response(202))
    auth = Authenticator(http)

    auth.request_verification_code(unverified)
    verified = auth.verify_device(unverified, " 1234 ")

    assert json.loads(provision.calls.last.request.content) == {"deviceUuid": DEVICE}
    assert provision.calls.last.request.headers["authorization"] == f"Bearer {session.access_token}"
    assert json.loads(verify.calls.last.request.content) == {"deviceUuid": DEVICE, "verificationCode": "1234"}
    assert verified.device_verified


def test_sms_limit(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.put(f"{API}/rest/device/provisions").mock(
        return_value=httpx.Response(429, json={"error": {"code": "PROVISION_CALLS_EXCEEDED"}})
    )
    with pytest.raises(SmsLimitExceeded):
        Authenticator(http).request_verification_code(session)


def test_relogin_uses_stored_pin(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response())
    stale = replace(session, access_token=make_jwt(timedelta(minutes=-1)))
    store = MemorySessionStore(stale)
    store.save_pin(session.email, "1234")

    renewed = stored_pin_relogin(Authenticator(http), store)(stale)

    assert renewed.access_token != stale.access_token
    assert store.load() == renewed


def test_relogin_without_pin_reports_expired(http: httpx.Client, session: Session) -> None:
    relogin = stored_pin_relogin(Authenticator(http), MemorySessionStore(session))
    with pytest.raises(SessionExpiredError):
        relogin(session)


def test_relogin_needing_sms_cannot_be_silent(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response("new-device"))
    store = MemorySessionStore(session)
    store.save_pin(session.email, "1234")
    with pytest.raises(DeviceVerificationRequired):
        stored_pin_relogin(Authenticator(http), store)(session)


def test_server_reassigning_device_is_reported(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response("brand-new-device"))
    result = Authenticator(http).login(session.email, "1234", previous=session)
    assert result.needs_device_verification
    assert result.device_changed
    assert result.session.device_uuid == "brand-new-device"


def test_first_login_is_not_reported_as_device_change(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=_token_response())
    assert not Authenticator(http).login("user@example.com", "1234").device_changed


def test_wrong_pin_is_invalid_credentials(mock_api: respx.MockRouter, http: httpx.Client) -> None:
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(401, json={"data": None, "error": {"errorCode": "401", "error": "Unauthorized"}})
    )
    with pytest.raises(InvalidCredentialsError):
        Authenticator(http).login("user@example.com", "0000")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"errorCode": "401", "error": "Unauthorized"}}),
        httpx.Response(403, json={"error": "Blocked user"}),
    ],
)
def test_relogin_failure_deletes_stored_pin_and_does_not_retry(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session, response: httpx.Response
) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(return_value=response)
    store = MemorySessionStore(session)
    store.save_pin(session.email, "1234")
    relogin = stored_pin_relogin(Authenticator(http), store)

    with pytest.raises(AuthError, match="He borrado el PIN"):
        relogin(session)
    with pytest.raises(SessionExpiredError):
        relogin(session)  # second command: no PIN left, no new login attempt

    assert store.load_pin(session.email) is None
    assert route.call_count == 1


def test_relogin_network_block_keeps_pin(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(403, text="<html>blocked</html>", headers={"content-type": "text/html"})
    )
    store = MemorySessionStore(session)
    store.save_pin(session.email, "1234")
    with pytest.raises(NetworkBlockedError):
        stored_pin_relogin(Authenticator(http), store)(session)
    assert store.load_pin(session.email) == "1234"
