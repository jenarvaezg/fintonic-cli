"""Login flow: OIDC password grant proxied by finapi, plus SMS device verification.

Observed sequence in the web app:
  1. POST /oidc/token {username, password, deviceUUID}  -> tokens in body, `device-uuid` header
  2. PUT  /rest/device/provisions {deviceUuid}          -> 202, sends SMS
  3. PUT  /rest/device/verification {deviceUuid, verificationCode} -> 202

`username` is the account email (the web form labels it "Email de usuario").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

import httpx

from fintonic.config import API_BASE
from fintonic.errors import (
    AuthError,
    DeviceVerificationRequired,
    InvalidCredentialsError,
    SessionExpiredError,
    raise_for_response,
)
from fintonic.session import Session
from fintonic.stores import SessionStore


@dataclass(frozen=True)
class LoginResult:
    session: Session
    needs_device_verification: bool
    # The server ignored our verified device and issued a new one, so SMS is needed again.
    device_changed: bool = False


class Authenticator:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def login(self, email: str, pin: str, previous: Session | None = None) -> LoginResult:
        known_device = previous.device_uuid if previous and previous.email == email else None
        response = self._http.post(
            f"{API_BASE}/oidc/token",
            json={"username": email, "password": pin, "deviceUUID": known_device},
        )
        try:
            raise_for_response(response)
        except SessionExpiredError as exc:
            raise InvalidCredentialsError() from exc

        data = (response.json() or {}).get("data") or {}
        if data.get("error"):
            raise InvalidCredentialsError(str(data.get("error_description") or data["error"]))

        device_uuid = response.headers.get("device-uuid") or known_device
        access_token = data.get("access_token")
        if not access_token or not device_uuid:
            raise AuthError("Respuesta de login inesperada: faltan token o device-uuid.")

        was_verified = bool(previous and previous.device_verified and known_device)
        verified = was_verified and device_uuid == known_device
        session = Session(
            email=email,
            access_token=access_token,
            device_uuid=device_uuid,
            device_verified=verified,
        )
        return LoginResult(
            session=session,
            needs_device_verification=not verified,
            device_changed=was_verified and not verified,
        )

    def request_verification_code(self, session: Session) -> None:
        """Ask Fintonic to send the SMS code. Not retried: each call sends an SMS."""
        response = self._http.put(
            f"{API_BASE}/rest/device/provisions",
            json={"deviceUuid": session.device_uuid},
            headers=_auth_headers(session),
        )
        raise_for_response(response)

    def verify_device(self, session: Session, code: str) -> Session:
        response = self._http.put(
            f"{API_BASE}/rest/device/verification",
            json={"deviceUuid": session.device_uuid, "verificationCode": code.strip()},
            headers=_auth_headers(session),
        )
        raise_for_response(response)
        return replace(session, device_verified=True)


def _auth_headers(session: Session) -> dict[str, str]:
    return {"authorization": f"Bearer {session.access_token}", "device-uuid": session.device_uuid or ""}


def stored_pin_relogin(auth: Authenticator, store: SessionStore) -> Callable[[Session], Session]:
    """Callback that silently logs in again with a PIN saved via `login --remember`.

    A rejected PIN is deleted on the spot: retrying a stale PIN on every command would
    get the account blocked.
    """

    def relogin(current: Session) -> Session:
        pin = store.load_pin(current.email)
        if not pin:
            raise SessionExpiredError()
        try:
            result = auth.login(current.email, pin, previous=current)
        except AuthError as exc:
            store.delete_pin(current.email)
            raise AuthError(
                f"El re-login automático ha fallado ({exc}). He borrado el PIN guardado para no bloquear "
                "la cuenta; ejecuta `fintonic login --remember`."
            ) from exc
        if result.needs_device_verification:
            raise DeviceVerificationRequired()
        store.save(result.session)
        return result.session

    return relogin
