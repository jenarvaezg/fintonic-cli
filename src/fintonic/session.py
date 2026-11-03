"""Session model. Storage backends live in `fintonic.stores`."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, fields, replace
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

    There is no refresh token: finapi exposes no refresh endpoint (verified Nov 2026) and the
    Keycloak issuer is not publicly reachable, so renewal means logging in again.
    """

    email: str
    access_token: str | None = None
    device_uuid: str | None = None
    device_verified: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})  # drops legacy `refresh_token`

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
        return replace(self, access_token=None)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        return cls.from_dict(json.loads(raw))
