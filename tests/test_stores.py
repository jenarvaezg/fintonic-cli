import json
import stat
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from fintonic import stores
from fintonic.session import Session
from fintonic.stores import FileSessionStore, KeyringSessionStore, StoreKind, resolve_store


class FakeKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.data[(service, username)]


@pytest.fixture
def fake_keyring():
    previous = keyring.get_keyring()
    backend = FakeKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_file_store_roundtrip_and_permissions(tmp_path: Path, session: Session) -> None:
    path = tmp_path / "cfg" / "fintonic" / "session.json"
    store = FileSessionStore(path)

    store.save(session)
    store.save_pin(session.email, "1234")

    assert store.load() == session
    assert store.load_pin(session.email) == "1234"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_file_store_clear_and_delete_pin(tmp_path: Path, session: Session) -> None:
    store = FileSessionStore(tmp_path / "s.json")
    store.save(session)
    store.save_pin(session.email, "1234")

    store.delete_pin(session.email)
    store.clear()

    assert store.load() is None
    assert store.load_pin(session.email) is None


def test_file_store_empty(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "missing.json")
    assert store.load() is None
    assert store.load_pin("x") is None


def test_legacy_refresh_token_is_ignored(tmp_path: Path, session: Session) -> None:
    path = tmp_path / "s.json"
    legacy = {"email": session.email, "access_token": session.access_token, "refresh_token": "old"}
    path.write_text(json.dumps({"session": legacy}))
    assert FileSessionStore(path).load() == Session(session.email, session.access_token)


def test_keyring_store(fake_keyring: FakeKeyring, session: Session) -> None:
    store = KeyringSessionStore()
    store.save(session)
    store.save_pin(session.email, "1234")
    assert store.load() == session
    assert store.load_pin(session.email) == "1234"

    store.delete_pin(session.email)
    store.clear()
    store.clear()  # idempotent
    assert store.load() is None
    assert fake_keyring.data == {}


def test_auto_uses_file_when_no_keyring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = resolve_store(StoreKind.auto)
    assert isinstance(store, FileSessionStore)
    assert store.path == tmp_path / "fintonic" / "session.json"


def test_auto_prefers_keyring(fake_keyring: FakeKeyring) -> None:
    assert stores.keyring_available()
    assert isinstance(resolve_store(StoreKind.auto), KeyringSessionStore)


def test_explicit_file_path(tmp_path: Path) -> None:
    store = resolve_store(StoreKind.file, tmp_path / "x.json")
    assert isinstance(store, FileSessionStore)
    assert store.path == tmp_path / "x.json"
