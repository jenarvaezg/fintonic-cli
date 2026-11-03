"""`tx` writes: --dry-run, change log, undo/history and the Android-only endpoints (issues #4, #10)."""

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.changelog import ChangeLog, ChangeRecord
from fintonic.client import FintonicClient
from fintonic.errors import InputError
from fintonic.models import Product
from fintonic.session import Session
from fintonic.split import SplitError
from fintonic.stores import MemorySessionStore
from fintonic.undo import UndoRefused
from fintonic.user_date import user_date_millis
from tests.conftest import API, make_tx

runner = CliRunner()
CATS = {
    "categoryTree": {
        "G0898": {"id": "G0898", "name": "Restaurante"},
        "G0101": {"id": "G0101", "name": "Farmacia"},
    }
}
ACCOUNTS = {"accounts": {"0182": [{"id": "acc1", "bankId": "0182", "alias": "Nómina", "ccc": "c" * 64}]}}
CARDS = {
    "creditCards": {"2100": [{"id": "card1", "bankId": "2100", "cardNumber": "1234", "displayCardNumber": "1234"}]}
}
EMPTY = {"accounts": {}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def log() -> ChangeLog:
    return ChangeLog()  # under the per-test XDG_STATE_HOME


def _serve_txs(mock_api: respx.MockRouter, *txs: dict[str, Any]) -> None:
    by_id = {t["id"]: t for t in txs}

    def lookup(request: httpx.Request) -> httpx.Response:
        found = by_id.get(request.url.params["transactionIds"])
        return httpx.Response(200, json={"count": int(bool(found)), "resultList": [found] if found else []})

    mock_api.get(f"{API}/rest/transaction/listByIds").mock(side_effect=lookup)
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))


def _body(route: respx.Route) -> object:
    content = route.calls.last.request.content
    return json.loads(content) if content else None


def _cats(amounts: dict[str, int]) -> dict[str, Any]:
    return {"weightedCategories": amounts, "splited": len(amounts) > 1}


def _no_writes(mock_api: respx.MockRouter) -> list[respx.Route]:
    return [
        mock_api.post(url__regex=rf"{API}/rest/transaction/.*").mock(return_value=httpx.Response(200)),
        mock_api.delete(url__regex=rf"{API}/rest/transaction/.*").mock(return_value=httpx.Response(200)),
    ]


# ---- --dry-run -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["set-category", "tx0001", "g0101"], "categoría: Restaurante (G0898) → Farmacia (G0101)"),
        (["split", "tx0001", "G0898:6", "G0101:4"], "Restaurante (G0898) -6,00 € + Farmacia (G0101) -4,00 €"),
        (["describe", "tx0001", "Cena [cumple]"], 'concepto: "Comercio 1" → "Cena [cumple]"'),
        (["note", "tx0001", "pagó Ana"], 'nota: (vacío) → "pagó Ana"'),
        (["mark-read", "tx0001"], "leído: no leído → leído"),
        (["mark-unread", "tx0001"], "leído: no leído → no leído"),
        (["set-date", "tx0001", "2026-10-01"], "fecha: 2026-10-30 → 2026-10-01"),
    ],
)
def test_dry_run_shows_change_without_writing(
    mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog, args: list[str], expected: str
) -> None:
    _serve_txs(mock_api, make_tx(1, quantity=-1000, categorization=_cats({"G0898": -1000})))
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", *args, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert expected in result.stderr
    assert "no se ha escrito nada" in result.stderr
    assert not any(route.called for route in writes)
    assert log.records() == []


def test_split_dry_run_validates_total(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    _serve_txs(mock_api, make_tx(1, quantity=-1000))
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", "split", "tx0001", "G0898:6", "G0101:5", "--dry-run"])

    assert isinstance(result.exception, SplitError)
    assert not any(route.called for route in writes)


def test_bulk_mark_read_dry_run_lists_unread(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    listing = mock_api.get(f"{API}/rest/transaction/list").mock(
        return_value=httpx.Response(200, json={"count": 2, "resultList": [make_tx(1), make_tx(2)]})
    )
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", "mark-read", "--all", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert listing.calls.last.request.url.params["read"] == "false"
    assert "tx0002 · leído: no leído → leído" in result.stderr
    assert "2 movimientos sin leer" in result.stderr
    assert not any(route.called for route in writes)


# ---- change log ------------------------------------------------------------------


def test_write_is_logged_after_success(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    _serve_txs(mock_api, make_tx(1, quantity=-1000, categorization=_cats({"G0898": -1000})))
    mock_api.post(f"{API}/rest/transaction/tx0001/category").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "set-category", "tx0001", "G0101"])

    assert result.exit_code == 0, result.output
    [record] = log.records()
    assert (record.command, record.tx_id, record.field) == ("set-category", "tx0001", "category")
    assert (record.before, record.after, record.rule) == ({"G0898": -1000}, {"G0101": -1000}, False)


def test_failed_write_is_not_logged(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    _serve_txs(mock_api, make_tx(1))
    mock_api.post(f"{API}/rest/transaction/tx0001/secondaryDisplay").mock(return_value=httpx.Response(500))

    result = runner.invoke(cli.app, ["tx", "note", "tx0001", "hola"])

    assert result.exit_code != 0
    assert log.records() == []


def test_set_category_similar_is_logged_as_rule(
    mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog
) -> None:
    _serve_txs(mock_api, make_tx(1))
    rule = mock_api.post(f"{API}/rest/transaction/tx0001/applyUserRule").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "set-category", "tx0001", "G0101", "--similar"])

    assert result.exit_code == 0, result.output
    assert rule.called
    assert log.records()[0].rule


def test_bulk_mark_read_logs_each_unread(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    mock_api.get(f"{API}/rest/transaction/listByBank/0182").mock(
        return_value=httpx.Response(200, json={"count": 2, "resultList": [make_tx(1), make_tx(2)]})
    )
    bank = mock_api.post(f"{API}/rest/transaction/markAsReadByBank/0182").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "mark-read", "--bank", "0182"])

    assert result.exit_code == 0, result.output
    assert bank.called
    assert [(r.tx_id, r.before, r.after, r.scope) for r in log.records()] == [
        ("tx0001", False, True, "bank:0182"),
        ("tx0002", False, True, "bank:0182"),
    ]


def test_history_command(store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(
        ChangeRecord(
            command="set-category",
            tx_id="tx1",
            field="category",
            before={"G0898": -1},
            after={"G0101": -1},
            timestamp="2026-11-01T10:00:00+00:00",
        )
    )
    log.append(ChangeRecord(command="note", tx_id="tx2", field="note", before="", after="hola"))

    as_json = runner.invoke(cli.app, ["tx", "history", "tx1", "-f", "json"])
    as_csv = runner.invoke(cli.app, ["tx", "history", "-n", "1", "-f", "csv"])

    assert as_json.exit_code == 0, as_json.output
    [entry] = json.loads(as_json.stdout)
    assert entry["command"] == "set-category" and entry["before"] == {"G0898": -1}
    assert as_csv.exit_code == 0, as_csv.output
    lines = as_csv.stdout.strip().splitlines()
    assert len(lines) == 2
    assert 'tx2,nota,(vacío),"""hola"""' in lines[1]


# ---- undo ------------------------------------------------------------------------


def test_undo_set_category_restores_previous_split(
    mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog
) -> None:
    log.append(
        ChangeRecord(
            command="set-category",
            tx_id="tx0001",
            field="category",
            before={"G0898": -600, "G0101": -400},
            after={"G0101": -1000},
        )
    )
    _serve_txs(mock_api, make_tx(1, quantity=-1000, categorization=_cats({"G0101": -1000})))
    split = mock_api.post(f"{API}/rest/transaction/tx0001/split").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])

    assert result.exit_code == 0, result.output
    assert _body(split) == {"categoryIds": ["G0898", "G0101"], "categoryAmounts": [-600, -400]}
    undo = log.records()[-1]
    assert undo.command == "undo"
    assert (undo.before, undo.after) == ({"G0101": -1000}, {"G0898": -600, "G0101": -400})
    assert undo.undoes == log.records()[0].timestamp


@pytest.mark.parametrize(
    ("before", "after", "tx", "method", "path", "body"),
    [
        ("Comercio", "Cena", {"primaryDisplay": "Cena"}, "POST", "primaryDisplay", {"textFieldValue": "Comercio"}),
        ("", "hola", {"secondaryDisplay": "hola"}, "POST", "secondaryDisplay", {"textFieldValue": ""}),
        (False, True, {"read": True}, "DELETE", "read", None),
        (True, False, {"read": False}, "POST", "markAsRead", None),
        ({"G0898": -1000}, {"G0101": -1000}, {"categorization": _cats({"G0101": -1000})}, "POST", "category", None),
    ],
)
def test_undo_each_field(
    mock_api: respx.MockRouter,
    store: MemorySessionStore,
    log: ChangeLog,
    before: Any,
    after: Any,
    tx: dict[str, Any],
    method: str,
    path: str,
    body: Any,
) -> None:
    field = {str: "description" if before == "Comercio" else "note", bool: "read", dict: "category"}[type(before)]
    log.append(ChangeRecord(command="x", tx_id="tx0001", field=field, before=before, after=after))
    _serve_txs(mock_api, make_tx(1, quantity=-1000, **tx))
    route = mock_api.route(method=method, url=f"{API}/rest/transaction/tx0001/{path}").mock(
        return_value=httpx.Response(200)
    )

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])

    assert result.exit_code == 0, result.output
    assert route.called
    if body is not None:
        assert _body(route) == body
    assert log.records()[-1].after == before


def test_undo_of_undo_redoes(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(ChangeRecord(command="note", tx_id="tx0001", field="note", before="", after="hola"))
    log.append(ChangeRecord(command="undo", tx_id="tx0001", field="note", before="hola", after=""))
    _serve_txs(mock_api, make_tx(1))
    note = mock_api.post(f"{API}/rest/transaction/tx0001/secondaryDisplay").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])

    assert result.exit_code == 0, result.output
    assert _body(note) == {"textFieldValue": "hola"}


def test_undo_dry_run(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(ChangeRecord(command="mark-read", tx_id="tx0001", field="read", before=False, after=True))
    _serve_txs(mock_api, make_tx(1, read=True))
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "leído: leído → no leído" in result.stderr
    assert not any(route.called for route in writes)
    assert len(log.records()) == 1


def test_undo_without_history_needs_no_network(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])
    assert isinstance(result.exception, UndoRefused)


def test_undo_refused_writes_nothing(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(ChangeRecord(command="note", tx_id="tx0001", field="note", before="", after="hola"))
    _serve_txs(mock_api, make_tx(1, secondaryDisplay="cambiada en la app"))
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])

    assert isinstance(result.exception, UndoRefused)
    assert not any(route.called for route in writes)


def test_undo_noop(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(ChangeRecord(command="mark-read", tx_id="tx0001", field="read", before=True, after=True))
    _serve_txs(mock_api, make_tx(1, read=True))
    writes = _no_writes(mock_api)

    result = runner.invoke(cli.app, ["tx", "undo", "tx0001"])

    assert result.exit_code == 0, result.output
    assert "Nada que deshacer" in result.stderr
    assert not any(route.called for route in writes)


# ---- Android-only endpoints (#4) --------------------------------------------------


def test_mark_unread_uses_delete(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    _serve_txs(mock_api, make_tx(1, read=True), make_tx(2, read=True))
    route = mock_api.delete(url__regex=rf"{API}/rest/transaction/tx000[12]/read").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "mark-unread", "tx0001", "tx0002"])

    assert result.exit_code == 0, result.output
    assert route.call_count == 2
    assert [(r.command, r.before, r.after) for r in log.records()] == [("mark-unread", True, False)] * 2


@pytest.mark.parametrize(
    ("product_id", "endpoint"),
    [("acc1", "markAsReadByAccount/0182/" + "c" * 64), ("card1", "markAsReadByCreditCard/2100/1234")],
)
def test_mark_read_by_product(
    mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog, product_id: str, endpoint: str
) -> None:
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    mock_api.get(f"{API}/rest/creditcard/listCreditCards").mock(return_value=httpx.Response(200, json=CARDS))
    mock_api.get(url__regex=rf"{API}/rest/(deposit|fund|loan|share|pensionplan)/.*").mock(
        return_value=httpx.Response(200, json=EMPTY)
    )
    key = "c" * 64 if product_id == "acc1" else "1234"
    bank = "0182" if product_id == "acc1" else "2100"
    mock_api.get(f"{API}/rest/transaction/listByBank/{bank}").mock(
        return_value=httpx.Response(
            200, json={"count": 2, "resultList": [make_tx(1, productId=key), make_tx(2, productId="other")]}
        )
    )
    route = mock_api.post(f"{API}/rest/transaction/{endpoint}").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "mark-read", "--product", product_id])

    assert result.exit_code == 0, result.output
    assert route.called
    assert [(r.tx_id, r.scope) for r in log.records()] == [("tx0001", f"product:{product_id}")]


def test_mark_read_product_excludes_other_targets(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["tx", "mark-read", "--product", "acc1", "--all"])
    assert result.exit_code != 0


def test_mark_product_read_rejects_other_kinds(session: Session, http: httpx.Client) -> None:
    loan = Product(kind="loan", id="loan1", bank_id="0182")
    with pytest.raises(InputError, match="cuentas y tarjetas"):
        FintonicClient(session, http=http).mark_product_read(loan)


def test_set_date_posts_epoch_millis(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    _serve_txs(mock_api, make_tx(1))
    route = mock_api.post(f"{API}/rest/transaction/tx0001/userDate").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "set-date", "tx0001", "2026-10-01"])

    assert result.exit_code == 0, result.output
    assert _body(route) == {"userDate": str(user_date_millis(date(2026, 10, 1)))}
    assert "Experimental" in result.stderr
    [record] = log.records()
    assert (record.before, record.after) == ("2026-10-30", "2026-10-01")


def test_set_date_rejects_bad_date(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["tx", "set-date", "tx0001", "01/10/2026"])
    assert isinstance(result.exception, InputError)


@pytest.mark.parametrize(
    ("day", "utc"),
    [
        (date(2026, 10, 30), datetime(2026, 10, 30, 11, 0, tzinfo=UTC)),  # CET, UTC+1
        (date(2026, 7, 1), datetime(2026, 7, 1, 10, 0, tzinfo=UTC)),  # CEST, UTC+2
    ],
)
def test_user_date_millis_is_noon_in_madrid(day: date, utc: datetime) -> None:
    assert user_date_millis(day) == int(utc.timestamp() * 1000)


# ---- review regressions ------------------------------------------------------------


def test_noop_write_is_not_logged(mock_api: respx.MockRouter, store: MemorySessionStore, log: ChangeLog) -> None:
    _serve_txs(mock_api, make_tx(1, read=True))
    mock_api.post(f"{API}/rest/transaction/tx0001/markAsRead").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "mark-read", "tx0001"])

    assert result.exit_code == 0, result.output
    assert log.records() == []


def test_log_failure_after_write_warns(
    mock_api: respx.MockRouter, store: MemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_txs(mock_api, make_tx(1))
    note = mock_api.post(f"{API}/rest/transaction/tx0001/secondaryDisplay").mock(return_value=httpx.Response(200))

    def broken(self: ChangeLog, record: ChangeRecord) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(ChangeLog, "append", broken)
    result = runner.invoke(cli.app, ["tx", "note", "tx0001", "hola"])

    assert result.exit_code == 0, result.output
    assert note.called
    assert "no se ha podido registrar" in result.stderr


def test_mark_read_product_dry_run_rejects_loans(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    loans = {"loans": {"0182": [{"id": "loan1", "bankId": "0182"}]}}
    mock_api.get(f"{API}/rest/loan/listLoans").mock(return_value=httpx.Response(200, json=loans))
    mock_api.get(url__regex=rf"{API}/rest/(account|creditcard|deposit|fund|share|pensionplan)/.*").mock(
        return_value=httpx.Response(200, json=EMPTY)
    )

    result = runner.invoke(cli.app, ["tx", "mark-read", "--product", "loan1", "--dry-run"])

    assert isinstance(result.exception, InputError)


def test_error_messages_are_not_rich_markup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], store: MemorySessionStore
) -> None:
    monkeypatch.setattr("sys.argv", ["fintonic", "tx", "undo", "[/x]"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "[/x]" in capsys.readouterr().err


def test_history_table_shows_brackets_literally(store: MemorySessionStore, log: ChangeLog) -> None:
    log.append(ChangeRecord(command="note", tx_id="tx1", field="note", before="", after="[b]x"))
    result = runner.invoke(cli.app, ["tx", "history"])
    assert result.exit_code == 0, result.output
    assert "[b]x" in result.stdout


def test_history_rejects_negative_limit(store: MemorySessionStore) -> None:
    assert runner.invoke(cli.app, ["tx", "history", "-n", "-1"]).exit_code != 0
