"""Local change log (issue #10) and plan_undo rules."""

import json
import stat
from pathlib import Path

import pytest

from fintonic.changelog import ChangeLog, ChangeRecord, CorruptChangeLogError, default_changelog_path
from fintonic.changes import Field, describe_value
from fintonic.models import Category, Transaction
from fintonic.undo import UndoRefused, plan_undo
from tests.conftest import make_tx


def _tx(**overrides) -> Transaction:
    return Transaction.model_validate(make_tx(1, **overrides))


def _split(amounts: dict[str, int]) -> dict:
    return {"weightedCategories": amounts, "splited": len(amounts) > 1}


# ---- storage -------------------------------------------------------------------


def test_default_path_follows_xdg_state_home(_isolated_state: Path) -> None:
    assert default_changelog_path() == _isolated_state / "fintonic" / "changes.jsonl"


def test_default_path_without_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_changelog_path() == tmp_path / ".local" / "state" / "fintonic" / "changes.jsonl"


def test_append_creates_private_dir_and_file(tmp_path: Path) -> None:
    log = ChangeLog(tmp_path / "fintonic" / "changes.jsonl")
    log.append(ChangeRecord(command="note", tx_id="tx1", field=Field.note, before="", after="hola"))

    assert stat.S_IMODE(log.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    assert log.records()[0].field == "note"


def test_history_is_newest_first_and_filters(tmp_path: Path) -> None:
    log = ChangeLog(tmp_path / "changes.jsonl")
    for i, tx_id in enumerate(["tx1", "tx2", "tx1"]):
        log.append(ChangeRecord(command="note", tx_id=tx_id, field="note", before=str(i), after=str(i + 1)))

    assert [r.before for r in log.history()] == ["2", "1", "0"]
    assert [r.before for r in log.history("tx1")] == ["2", "0"]
    assert len(log.history(limit=1)) == 1
    last = log.last_for("tx1")
    assert last is not None and last.before == "2"
    assert log.last_for("nope") is None


def test_append_never_glues_onto_a_truncated_line(tmp_path: Path) -> None:
    path = tmp_path / "changes.jsonl"
    path.write_text('{"command": "no')  # crash mid-write
    path.chmod(0o644)
    ChangeLog(path).append(ChangeRecord(command="note", tx_id="tx1", field="note", before="", after="a"))

    lines = path.read_text().splitlines()
    assert lines[0] == '{"command": "no'
    assert ChangeRecord.from_dict(json.loads(lines[1])).tx_id == "tx1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_log_is_empty(tmp_path: Path) -> None:
    assert ChangeLog(tmp_path / "none.jsonl").history() == []


@pytest.mark.parametrize("line", ["{not json", '{"command": "x"}', "[]", '{"command": 1, "tx_id": "a"}'])
def test_corrupt_line_is_reported(tmp_path: Path, line: str) -> None:
    path = tmp_path / "changes.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(CorruptChangeLogError, match="línea 1"):
        ChangeLog(path).records()


# ---- describe ------------------------------------------------------------------


def test_describe_values() -> None:
    cats = {"G0898": Category(id="G0898", name="Restaurante")}
    assert describe_value(Field.category, {"G0898": -100}, cats) == "Restaurante (G0898)"
    assert describe_value(Field.category, {"G0898": -100, "G0101": -50}, cats) == (
        "Restaurante (G0898) -1,00 € + G0101 -0,50 €"
    )
    assert describe_value(Field.category, {}) == "(sin categoría)"
    assert describe_value(Field.read, False) == "no leído"
    assert describe_value(Field.note, "") == "(vacío)"
    assert describe_value(Field.description, "Cena") == '"Cena"'
    assert describe_value("future", 3) == "3"


# ---- plan_undo -----------------------------------------------------------------


def _record(field: Field, before, after, **extra) -> ChangeRecord:
    return ChangeRecord(command="cmd", tx_id="tx0001", field=field, before=before, after=after, **extra)


def test_undo_restores_previous_split() -> None:
    tx = _tx(quantity=-1000, categorization=_split({"G0101": -1000}))
    record = _record(Field.category, {"G0898": -600, "G0101": -400}, {"G0101": -1000})
    plan = plan_undo(record, tx)
    assert plan.target == {"G0898": -600, "G0101": -400}
    assert not plan.noop


def test_undo_of_split_joins_back_to_one_category() -> None:
    """Setting one category on a split movement joins it (verified against the real API in #3)."""
    tx = _tx(quantity=-1000, categorization=_split({"G0898": -600, "G0101": -400}))
    record = _record(Field.category, {"G0898": -1000}, {"G0898": -600, "G0101": -400})
    plan = plan_undo(record, tx)
    assert plan.target == {"G0898": -1000}


def test_undo_refuses_rules() -> None:
    tx = _tx(quantity=-1000, categorization=_split({"G0101": -1000}))
    with pytest.raises(UndoRefused, match="regla"):
        plan_undo(_record(Field.category, {"G0898": -1000}, {"G0101": -1000}, rule=True), tx)


def test_undo_refuses_when_changed_since() -> None:
    tx = _tx(secondaryDisplay="otra cosa")
    with pytest.raises(UndoRefused, match="ha cambiado"):
        plan_undo(_record(Field.note, "", "hola"), tx)


def test_undo_refuses_parts_that_no_longer_add_up() -> None:
    tx = _tx(quantity=-1000, categorization=_split({"G0101": -1000}))
    with pytest.raises(UndoRefused, match="no suman"):
        plan_undo(_record(Field.category, {"G0898": -600, "G0101": -500}, {"G0101": -1000}), tx)


@pytest.mark.parametrize("before", [{}, None])
def test_undo_refuses_unknown_previous_category(before) -> None:
    tx = _tx(quantity=-1000, categorization=_split({"G0101": -1000}))
    with pytest.raises(UndoRefused, match="categoría anterior"):
        plan_undo(_record(Field.category, before, {"G0101": -1000}), tx)


def test_undo_refuses_unknown_field() -> None:
    with pytest.raises(UndoRefused, match="No sé"):
        plan_undo(_record("mystery", 1, 2), _tx())  # type: ignore[arg-type]


def test_undo_of_noop_change() -> None:
    plan = plan_undo(_record(Field.read, True, True), _tx(read=True))
    assert plan.noop
