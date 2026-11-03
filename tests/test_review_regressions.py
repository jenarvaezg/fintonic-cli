"""Regression tests for the code review of issue #1."""

import json
import stat
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli, stores
from fintonic.auth import Authenticator, stored_pin_relogin
from fintonic.client import FintonicClient
from fintonic.errors import ApiError, CorruptStoreError, InvalidCredentialsError, SessionExpiredError
from fintonic.session import Session
from fintonic.stores import FileSessionStore, MemorySessionStore
from tests.conftest import API, DEVICE, make_jwt

runner = CliRunner()


def test_typer_never_renders_locals() -> None:
    assert cli.app.pretty_exceptions_show_locals is False


@pytest.mark.parametrize("status", [400, 403, 422])
def test_any_4xx_from_token_endpoint_is_invalid_credentials(
    mock_api: respx.MockRouter, http: httpx.Client, status: int
) -> None:
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(status, json={"error": {"errorCode": "BAD_REQUEST", "error": "invalid_grant"}})
    )
    with pytest.raises(InvalidCredentialsError):
        Authenticator(http).login("user@example.com", "0000")


def test_400_rejection_deletes_stored_pin(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(400, json={"error": {"error": "invalid_grant"}})
    )
    store = MemorySessionStore(session)
    store.save_pin(session.email, "1234")
    relogin = stored_pin_relogin(Authenticator(http), store)
    with pytest.raises(Exception, match="He borrado el PIN"):
        relogin(session)
    assert store.load_pin(session.email) is None
    assert route.call_count == 1


@pytest.mark.parametrize("status", [429, 500])
def test_transient_token_errors_keep_pin(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session, status: int
) -> None:
    mock_api.post(f"{API}/oidc/token").mock(return_value=httpx.Response(status, json={"error": "busy"}))
    store = MemorySessionStore(session)
    store.save_pin(session.email, "1234")
    with pytest.raises(ApiError):
        stored_pin_relogin(Authenticator(http), store)(session)
    assert store.load_pin(session.email) == "1234"


def test_at_most_one_relogin_per_request(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    expired = replace(session, access_token=make_jwt(timedelta(minutes=-5)))
    mock_api.get(f"{API}/rest/user/profile").mock(return_value=httpx.Response(401))
    calls: list[Session] = []

    def relogin(current: Session) -> Session:
        calls.append(current)
        return replace(current, access_token=make_jwt())

    with pytest.raises(SessionExpiredError):
        FintonicClient(expired, http=http, relogin=relogin).profile()
    assert len(calls) == 1


@pytest.mark.parametrize("content", ['{"session": {"email": "x"', "[1, 2]", '{"session": {"nope": 1}}'])
def test_corrupt_file_store(tmp_path: Path, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(CorruptStoreError, match="Bórralo"):
        FileSessionStore(path).load()


def test_cli_reports_corrupt_store_without_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"pins": {"x": "9999"')
    monkeypatch.setenv("FINTONIC_STORE", "file")
    monkeypatch.setenv("FINTONIC_STORE_PATH", str(path))
    monkeypatch.setattr("sys.argv", ["fintonic", "status"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    err = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "corrupto" in err
    assert "9999" not in err


def test_write_leaves_no_temp_files_and_keeps_0600(tmp_path: Path, session: Session) -> None:
    store = FileSessionStore(tmp_path / "s.json")
    store.save(session)
    store.save_pin(session.email, "1234")
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert stat.S_IMODE((tmp_path / "s.json").stat().st_mode) == 0o600


def test_failed_write_removes_temp_file(tmp_path: Path, session: Session, monkeypatch) -> None:
    store = FileSessionStore(tmp_path / "s.json")

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(stores.json, "dump", boom)
    with pytest.raises(RuntimeError):
        store.save(session)
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert not (tmp_path / "s.json").exists()


def test_pin_stdin_wins_over_env(mock_api: respx.MockRouter, monkeypatch, session: Session) -> None:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    monkeypatch.setenv("FINTONIC_PIN", "stale")
    route = mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json={"data": {"access_token": make_jwt()}}, headers={"device-uuid": DEVICE})
    )
    result = runner.invoke(cli.app, ["login", "--pin-stdin"], input="fresh\n")
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content)["password"] == "fresh"


def test_switching_account_forgets_previous_pin(mock_api: respx.MockRouter, monkeypatch, session: Session) -> None:
    memory = MemorySessionStore(session)
    memory.save_pin(session.email, "1234")
    monkeypatch.setattr(cli, "_store", lambda: memory)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json={"data": {"access_token": make_jwt()}}, headers={"device-uuid": "d2"})
    )
    mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(202))
    result = runner.invoke(cli.app, ["login", "--email", "other@example.com", "--pin-stdin"], input="5678\n")
    assert result.exit_code == 0, result.output
    assert memory.load_pin(session.email) is None


def test_main_handles_network_errors(mock_api: respx.MockRouter, monkeypatch, capsys, session: Session) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore(session))
    mock_api.get(f"{API}/rest/overview/position").mock(side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr("fintonic.cli.build_http_client", lambda: httpx.Client())
    monkeypatch.setattr("sys.argv", ["fintonic", "position"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "Error de red" in capsys.readouterr().err


def test_bad_date_is_friendly(mock_api: respx.MockRouter, monkeypatch, capsys, session: Session) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore(session))
    monkeypatch.setattr("sys.argv", ["fintonic", "transactions", "--from", "ayer"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert not mock_api.calls  # rejected before touching the network
