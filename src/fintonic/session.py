"""Session model and its storage (macOS Keychain via `keyring`)."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

import keyring

from fintonic.config import KEYRING_SERVICE, TOKEN_EXPIRY_SKEW


def jwt_expiry(token: str) -> datetime | None:
    """Read the `exp` claim without verifying the signature (we only need it for scheduling)."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(padded))["exp"]
        return datetime.fromtimestamp(int(exp), tz=UTC)
    except (IndexError, KeyError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Session:
    email: str
    access_token: str | None = None
    refresh_token: str | None = None
    device_uuid: str | None = None
    device_verified: bool = False

    @property
    def expires_at(self) -> datetime | None:
        return jwt_expiry(self.access_token) if self.access_token else None

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.access_token:
            return True
        expires_at = self.expires_at
        if expires_at is None:
            return False
        now = now or datetime.now(tz=UTC)
        return now >= expires_at - timedelta(seconds=TOKEN_EXPIRY_SKEW)

    def without_tokens(self) -> Session:
        """Logout keeps the device so the next login can skip SMS verification."""
        return replace(self, access_token=None, refresh_token=None)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        return cls(**json.loads(raw))


class SessionStore(Protocol):
    def load(self) -> Session | None: ...
    def save(self, session: Session) -> None: ...
    def clear(self) -> None: ...
    def load_pin(self, email: str) -> str | None: ...
    def save_pin(self, email: str, pin: str) -> None: ...
    def delete_pin(self, email: str) -> None: ...


class KeyringSessionStore:
    _SESSION_KEY = "session"

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def load(self) -> Session | None:
        raw = keyring.get_password(self._service, self._SESSION_KEY)
        return Session.from_json(raw) if raw else None

    def save(self, session: Session) -> None:
        keyring.set_password(self._service, self._SESSION_KEY, session.to_json())

    def clear(self) -> None:
        if keyring.get_password(self._service, self._SESSION_KEY) is not None:
            keyring.delete_password(self._service, self._SESSION_KEY)

    def load_pin(self, email: str) -> str | None:
        return keyring.get_password(self._service, f"pin:{email}")

    def save_pin(self, email: str, pin: str) -> None:
        keyring.set_password(self._service, f"pin:{email}", pin)

    def delete_pin(self, email: str) -> None:
        if self.load_pin(email) is not None:
            keyring.delete_password(self._service, f"pin:{email}")


class MemorySessionStore:
    """In-memory store for tests and library users who manage persistence themselves."""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self.pins: dict[str, str] = {}

    def load(self) -> Session | None:
        return self.session

    def save(self, session: Session) -> None:
        self.session = session

    def clear(self) -> None:
        self.session = None

    def load_pin(self, email: str) -> str | None:
        return self.pins.get(email)

    def save_pin(self, email: str, pin: str) -> None:
        self.pins[email] = pin

    def delete_pin(self, email: str) -> None:
        self.pins.pop(email, None)
