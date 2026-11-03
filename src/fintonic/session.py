"""Session model. Storage backends live in `fintonic.stores`."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fintonic.config import TOKEN_EXPIRY_SKEW


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
    """Login state.

    `refresh_token` is kept only to invalidate the session server-side on logout
    (`POST /oidc/logout`). It cannot renew the access token: neither finapi nor the Android
    app has a refresh endpoint (the app re-logs in with the stored PIN, like `--remember`).

    `finia_token` and `user_code` come from the login response headers and are sent on every
    request, as the Android app does. Sessions saved by older versions lack them and still load.
    """

    email: str
    access_token: str | None = field(default=None, repr=False)
    device_uuid: str | None = None
    device_verified: bool = False
    refresh_token: str | None = field(default=None, repr=False)
    finia_token: str | None = field(default=None, repr=False)
    user_code: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        """Tolerate sessions from other versions: missing fields default, unknown ones are dropped."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

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
        return replace(self, access_token=None, refresh_token=None, finia_token=None)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        return cls.from_dict(json.loads(raw))
