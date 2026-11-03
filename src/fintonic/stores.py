"""Where the session (and optionally the PIN) lives: OS keyring, a 0600 file, or memory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import keyring

from fintonic.config import KEYRING_SERVICE
from fintonic.session import Session

_NO_KEYRING_MODULES = {"keyring.backends.fail", "keyring.backends.null"}


class StoreKind(StrEnum):
    auto = "auto"
    keyring = "keyring"
    file = "file"


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


class FileSessionStore:
    """JSON file readable only by the owner (dir 0700, file 0600), for headless Linux/CI."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def load(self) -> Session | None:
        raw = self._read().get("session")
        return Session.from_dict(raw) if raw else None

    def save(self, session: Session) -> None:
        self._write({**self._read(), "session": asdict(session)})

    def clear(self) -> None:
        data = self._read()
        data.pop("session", None)
        self._write(data)

    def load_pin(self, email: str) -> str | None:
        return self._read().get("pins", {}).get(email)

    def save_pin(self, email: str, pin: str) -> None:
        data = self._read()
        self._write({**data, "pins": {**data.get("pins", {}), email: pin}})

    def delete_pin(self, email: str) -> None:
        data = self._read()
        pins = {k: v for k, v in data.get("pins", {}).items() if k != email}
        self._write({**data, "pins": pins})


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


def default_store_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "fintonic" / "session.json"


def keyring_available() -> bool:
    return type(keyring.get_keyring()).__module__ not in _NO_KEYRING_MODULES


def resolve_store(kind: StoreKind = StoreKind.auto, path: Path | None = None) -> SessionStore:
    if kind is StoreKind.auto:
        kind = StoreKind.keyring if keyring_available() else StoreKind.file
    if kind is StoreKind.keyring:
        return KeyringSessionStore()
    return FileSessionStore(path or default_store_path())
