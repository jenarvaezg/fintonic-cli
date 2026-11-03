import csv
import io
import json
from dataclasses import replace

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.output import format_eur
from fintonic.session import Session
from fintonic.stores import FileSessionStore, MemorySessionStore
from tests.conftest import API, DEVICE, make_jwt, make_tx

runner = CliRunner()
TX_LIST = f"{API}/rest/transaction/list/pageable"
CATS = {"categoryTree": {"G0898": {"id": "G0898", "name": "Restaurante", "classified": True}}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


def test_format_eur() -> None:
    assert format_eur(-112804) == "-1.128,04 €"
    assert format_eur(5) == "0,05 €"
    assert format_eur(0) == "0,00 €"


ACCOUNTS = {"accounts": {"0182": [{"id": "acc1", "bankId": "0182", "alias": "Nómina", "ccc": "p" * 64}]}}


def test_transactions_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    split = make_tx(
        2, quantity=-1000, categorization={"weightedCategories": {"G0898": -700, "G0101": -300}, "splited": True}
    )
    mock_api.get(TX_LIST).mock(
        return_value=httpx.Response(200, json={"content": [make_tx(1, quantity=-1050), split], "last": True})
    )
    result = runner.invoke(cli.app, ["transactions", "-f", "json"])
    assert result.exit_code == 0, result.output
    first, second = json.loads(result.stdout)
    assert first["amount"] == "-10.50"
    assert first["category"] == "Restaurante"
    assert first["product"] == "Nómina"
    assert first["operation_date"] == "2026-10-29"
    assert second["split"] is True
    assert second["categories"] == {"G0898": "-7.00", "G0101": "-3.00"}


def test_transactions_table_marks_splits(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    split = make_tx(2, quantity=-1000, categorization={"weightedCategories": {"G0898": -700, "G0101": -300}})
    mock_api.get(TX_LIST).mock(return_value=httpx.Response(200, json={"content": [split], "last": True}))
    result = runner.invoke(cli.app, ["transactions", "-f", "csv"])
    row = next(csv.DictReader(io.StringIO(result.stdout)))
    assert row["categoría"] == "Restaurante +1"
    assert row["producto"] == "Nómina"
    assert row["id"] == "tx0002"


def test_summary_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    tree = {
        "categoryTree": {
            "G0898": {"id": "G0898", "name": "Restaurante", "ancestors": ["root", "G0000", "G0800"]},
            "I0101": {"id": "I0101", "name": "Nómina", "ancestors": ["root", "I0000", "I0100"]},
            "N0102": {"id": "N0102", "name": "Traspaso", "computable": False, "ancestors": ["root", "N0000", "N0100"]},
        }
    }
    txs = [
        make_tx(1, quantity=200000, bankId="0182", categorization={"weightedCategories": {"I0101": 200000}}),
        make_tx(2, quantity=-3000, bankId="0182", categorization={"weightedCategories": {"G0898": -3000}}),
        make_tx(3, quantity=-50000, bankId="0182", categorization={"weightedCategories": {"N0102": -50000}}),
        make_tx(4, quantity=-999, bankId="2091", categorization={"weightedCategories": {"G0898": -999}}),
    ]
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=tree))
    route = mock_api.get(TX_LIST).mock(return_value=httpx.Response(200, json={"content": txs, "last": True}))

    result = runner.invoke(cli.app, ["summary", "--month", "2026-10", "--exclude-bank", "2091", "-f", "json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert (body["income"], body["expenses"], body["net"]) == ("2000.00", "-30.00", "1970.00")
    assert body["excluded_non_computable"] == "-500.00"
    params = route.calls.last.request.url.params
    assert (params["beginDate"], params["endDate"]) == ("2026-10-01", "2026-10-31")


def test_transactions_csv(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    mock_api.get(TX_LIST).mock(
        return_value=httpx.Response(200, json={"content": [make_tx(1), make_tx(2)], "last": True})
    )
    result = runner.invoke(cli.app, ["transactions", "-f", "csv", "--from", "2026-10-01"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert len(rows) == 2
    assert rows[0]["fecha"] == "2026-10-30"


def test_login_with_sms(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    memory = MemorySessionStore()
    monkeypatch.setattr(cli, "_store", lambda: memory)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    body = {"data": {"access_token": make_jwt(), "refresh_token": make_jwt(), "error": None}}
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json=body, headers={"device-uuid": DEVICE})
    )
    sms = mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(202))
    mock_api.put(f"{API}/rest/device/verification").mock(return_value=httpx.Response(202))

    result = runner.invoke(cli.app, ["login", "--email", "user@example.com", "--remember"], input="1234\n9876\n")

    assert result.exit_code == 0, result.output
    assert sms.called
    assert memory.session and memory.session.device_verified
    assert memory.session.refresh_token
    assert memory.load_pin("user@example.com") == "1234"


def test_products_table_hides_canceled(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    body = {
        "creditCards": {
            "0182": [
                {"id": "c1", "bankId": "0182", "balance": -5000, "alias": "Visa viva", "displayCardNumber": "**** 1"},
                {"id": "c2", "bankId": "0182", "balance": 0, "alias": "Visa muerta", "canceled": True},
            ]
        }
    }
    mock_api.get(f"{API}/rest/creditcard/listCreditCards").mock(return_value=httpx.Response(200, json=body))

    result = runner.invoke(cli.app, ["products", "credit_card", "-f", "csv"])

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [r["nombre"] for r in rows] == ["Visa viva"]
    assert rows[0]["saldo"] == "-5000"


def test_headless_login_sends_sms_then_verify(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    memory = MemorySessionStore()
    monkeypatch.setattr(cli, "_store", lambda: memory)
    body = {"data": {"access_token": make_jwt(), "error": None}}
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json=body, headers={"device-uuid": DEVICE})
    )
    sms = mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(202))
    verify = mock_api.put(f"{API}/rest/device/verification").mock(return_value=httpx.Response(202))

    first = runner.invoke(cli.app, ["login", "--email", "user@example.com", "--pin-stdin"], input="1234\n")
    assert first.exit_code == 0, first.output
    assert sms.called and not verify.called
    assert memory.session and not memory.session.device_verified
    assert "fintonic verify" in first.stderr

    blocked = runner.invoke(cli.app, ["position"])
    assert "verify" in str(blocked.exception)

    second = runner.invoke(cli.app, ["verify", "9876"])
    assert second.exit_code == 0, second.output
    assert memory.session.device_verified


def test_pin_from_env(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    monkeypatch.setenv("FINTONIC_PIN", "4321")
    route = mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json={"data": {"access_token": make_jwt()}}, headers={"device-uuid": DEVICE})
    )
    result = runner.invoke(cli.app, ["login"])
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content)["password"] == "4321"


def test_file_store_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path, session: Session) -> None:
    path = tmp_path / "s.json"
    FileSessionStore(path).save(session)
    result = runner.invoke(cli.app, ["status"], env={"FINTONIC_STORE": "file", "FINTONIC_STORE_PATH": str(path)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["store"] == "FileSessionStore"


def test_logout_keeps_device(store: MemorySessionStore, session: Session) -> None:
    store.save_pin(session.email, "1234")
    assert runner.invoke(cli.app, ["logout"]).exit_code == 0
    assert store.session is not None
    assert store.session.access_token is None
    assert store.session.device_uuid == DEVICE
    assert store.load_pin(session.email) is None


def test_logout_invalidates_session_on_server(
    mock_api: respx.MockRouter, store: MemorySessionStore, session: Session
) -> None:
    store.save(replace(session, refresh_token="rt", finia_token="ft"))
    route = mock_api.post(f"{API}/oidc/logout").mock(return_value=httpx.Response(200, json={"data": None}))

    result = runner.invoke(cli.app, ["logout"])

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {"refresh_token": "rt"}
    assert store.session is not None
    assert (store.session.access_token, store.session.refresh_token, store.session.finia_token) == (None, None, None)
    assert "rt" not in result.output


@pytest.mark.parametrize(
    ("response", "warning"),
    [
        (httpx.Response(500, json={"error": "boom"}), "No se ha podido cerrar la sesión en Fintonic"),
        (httpx.ConnectError("offline"), "error de red: ConnectError"),
        (httpx.Response(401, json={"error": "Invalid Token"}), "ha rechazado el cierre"),
    ],
)
def test_logout_server_failure_still_clears_local(
    mock_api: respx.MockRouter,
    store: MemorySessionStore,
    session: Session,
    response: httpx.Response | Exception,
    warning: str,
) -> None:
    store.save(replace(session, refresh_token="rt"))
    store.save_pin(session.email, "1234")
    route = mock_api.post(f"{API}/oidc/logout")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)

    result = runner.invoke(cli.app, ["logout", "--forget-device"])

    assert result.exit_code == 0, result.output
    assert warning in " ".join(result.stderr.split())
    assert "Sesión cerrada." in result.stderr
    assert store.session is None
    assert store.load_pin(session.email) is None


def test_logout_legacy_session_warns_local_only(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["logout"])  # session fixture has no refresh_token; no route mocked
    assert result.exit_code == 0, result.output
    assert "solo se cierra en local" in " ".join(result.stderr.split())
    assert store.session is not None and store.session.access_token is None


def test_status(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["logged_in"] is True


def test_main_prints_friendly_error_when_logged_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore())
    monkeypatch.setattr("sys.argv", ["fintonic", "position"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "fintonic login" in capsys.readouterr().err
