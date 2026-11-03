from dataclasses import replace

from fintonic.headers import auth_headers
from fintonic.session import Session
from tests.conftest import DEVICE


def test_verified_session_sends_everything_it_has(session: Session) -> None:
    full = replace(session, finia_token="ft", user_code="uc")
    assert auth_headers(full) == {
        "authorization": f"Bearer {session.access_token}",
        "finia_token": "ft",
        "x-fintonic-user-code": "uc",
        "device-uuid": DEVICE,
    }


def test_legacy_session_without_finia_token_or_user_code(session: Session) -> None:
    assert auth_headers(session) == {"authorization": f"Bearer {session.access_token}", "device-uuid": DEVICE}


def test_unverified_device_is_not_sent(session: Session) -> None:
    assert "device-uuid" not in auth_headers(replace(session, device_verified=False))


def test_device_verification_calls_force_the_device(session: Session) -> None:
    assert auth_headers(replace(session, device_verified=False), force_device=True)["device-uuid"] == DEVICE


def test_nothing_without_tokens() -> None:
    assert auth_headers(Session(email="user@example.com")) == {}
