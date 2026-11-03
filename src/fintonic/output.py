"""Render rows as a rich table, JSON or CSV."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.table import Table

from fintonic.models import cents_to_decimal


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    csv = "csv"


def format_eur(cents: int) -> str:
    """Spanish formatting: -1.128,04 €"""
    value = cents_to_decimal(cents)
    text = f"{abs(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if value < 0 else ''}{text} €"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def render(rows: Sequence[dict[str, Any]], fmt: OutputFormat, *, title: str | None = None) -> None:
    if fmt is OutputFormat.json:
        print(json.dumps(list(rows), indent=2, ensure_ascii=False, default=_json_default))
        return
    if not rows:
        Console(stderr=True).print("[dim]Sin resultados[/dim]")
        return
    columns = list(rows[0].keys())
    if fmt is OutputFormat.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    table = Table(title=title, show_lines=False)
    for col in columns:
        table.add_column(col, justify="right" if col in {"importe", "saldo", "amount"} else "left")
    for row in rows:
        table.add_row(*("" if row.get(c) is None else str(row.get(c)) for c in columns))
    Console().print(table)


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=_json_default))
