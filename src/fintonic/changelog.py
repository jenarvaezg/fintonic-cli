"""Append-only local log of every write made through the CLI, so it can be reviewed and undone.

One JSON object per line in `$XDG_STATE_HOME/fintonic/changes.jsonl` (default
`~/.local/state/fintonic/`), directory 0700 and file 0600: it holds amounts and concepts.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fintonic.errors import FintonicError


class CorruptChangeLogError(FintonicError):
    def __init__(self, path: Path, line: int) -> None:
        super().__init__(f"El historial de cambios {path} está corrupto (línea {line}). Revísalo a mano.")


@dataclass(frozen=True)
class ChangeRecord:
    """One write on one transaction: enough of the previous state to put it back.

    `field` is one of `fintonic.changes.Field`; `before`/`after` are that field's JSON value.
    """

    command: str
    tx_id: str
    field: str
    before: Any
    after: Any
    timestamp: str = dataclass_field(default_factory=lambda: datetime.now(tz=UTC).isoformat(timespec="seconds"))
    rule: bool = False  # set-category --similar also created a rule for other movements
    scope: str | None = None  # bulk mark-read target (bank:<id>, product:<id>, all)
    undoes: str | None = None  # timestamp of the record an `undo` reverted

    @property
    def moment(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)

    @classmethod
    def from_dict(cls, raw: Any) -> ChangeRecord:
        if not isinstance(raw, dict):
            raise TypeError("record must be an object")
        record = cls(**raw)
        if not all(isinstance(v, str) for v in (record.command, record.tx_id, record.field, record.timestamp)):
            raise TypeError("command, tx_id, field and timestamp must be strings")
        datetime.fromisoformat(record.timestamp)  # raises ValueError when malformed
        return record


def default_changelog_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state_home) / "fintonic" / "changes.jsonl"


def _ends_mid_line(fd: int) -> bool:
    size = os.fstat(fd).st_size
    return size > 0 and os.pread(fd, 1, size - 1) != b"\n"


class ChangeLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_changelog_path()

    def append(self, record: ChangeRecord) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = (json.dumps(asdict(record), ensure_ascii=False) + "\n").encode()
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # one whole line per write, even with concurrent CLIs
            os.fchmod(fd, 0o600)  # also tighten a file created by hand or by an older version
            if _ends_mid_line(fd):  # never glue a record onto a line cut short by a crash
                data = b"\n" + data
            while data:
                data = data[os.write(fd, data) :]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def records(self) -> list[ChangeRecord]:
        """All records, oldest first."""
        if not self.path.exists():
            return []
        result = []
        with self.path.open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    result.append(ChangeRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise CorruptChangeLogError(self.path, number) from exc
        return result

    def history(self, tx_id: str | None = None, limit: int | None = None) -> list[ChangeRecord]:
        """Most recent first, optionally for one transaction."""
        found = [r for r in reversed(self.records()) if tx_id is None or r.tx_id == tx_id]
        return found[:limit] if limit else found

    def last_for(self, tx_id: str) -> ChangeRecord | None:
        found = self.history(tx_id, limit=1)
        return found[0] if found else None
