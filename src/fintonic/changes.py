"""The editable state of a transaction, field by field: read it, describe it and write it back.

Every `tx` write command changes exactly one `Field`; its value is plain JSON so it can go to
the change log and be restored later by `tx undo`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Any

from fintonic.client import FintonicClient
from fintonic.models import Category, Transaction
from fintonic.output import format_eur
from fintonic.split import build_split_body


class Field(StrEnum):
    category = "category"  # {category_id: signed cents}; more than one entry when split
    description = "description"  # concept shown in lists
    note = "note"
    read = "read"
    user_date = "user_date"  # YYYY-MM-DD


LABELS: Mapping[str, str] = {
    Field.category: "categoría",
    Field.description: "concepto",
    Field.note: "nota",
    Field.read: "leído",
    Field.user_date: "fecha",
}


def current_value(tx: Transaction, field: Field) -> Any:
    match field:
        case Field.category:
            return tx.category_amounts
        case Field.description:
            return tx.label
        case Field.note:
            return tx.secondary_display or tx.note or ""
        case Field.read:
            return tx.read
        case Field.user_date:
            return tx.user_date.isoformat() if tx.user_date else None


def single_category(tx: Transaction, category_id: str) -> dict[str, int]:
    return {category_id: tx.quantity}


def split_categories(tx: Transaction, parts: list[tuple[str, int]]) -> dict[str, int]:
    """Validates the parts against the transaction total (raises `SplitError`)."""
    body = build_split_body(tx.quantity, parts)
    return dict(zip(body["categoryIds"], body["categoryAmounts"], strict=True))


def write_value(client: FintonicClient, tx: Transaction, field: Field, value: Any) -> None:
    """Make `field` of `tx` equal to `value` with the matching API write."""
    match field:
        case Field.category:
            if len(value) == 1:
                client.set_category(tx.id, next(iter(value)))
            else:
                client.split(tx, [(cid, abs(cents)) for cid, cents in value.items()])
        case Field.description:
            client.set_description(tx.id, value)
        case Field.note:
            client.set_note(tx.id, value)
        case Field.read:
            if value:
                client.mark_read(tx.id)
            else:
                client.mark_unread(tx.id)
        case Field.user_date:
            client.set_user_date(tx.id, date.fromisoformat(value))


def describe_value(field: str, value: Any, categories: Mapping[str, Category] | None = None) -> str:
    """Human-readable value; `field` may be any string read back from the log."""
    if field == Field.category:
        return _describe_categories(value, categories or {})
    if field == Field.read:
        return "leído" if value else "no leído"
    if field in (Field.description, Field.note):
        return f'"{value}"' if value else "(vacío)"
    if field == Field.user_date:
        return value or "(sin fecha)"
    return str(value)


def _describe_categories(value: Any, categories: Mapping[str, Category]) -> str:
    if not isinstance(value, dict) or not value:
        return "(sin categoría)"

    def name(cid: str) -> str:
        return f"{categories[cid].label} ({cid})" if cid in categories else cid

    if len(value) == 1:
        return name(next(iter(value)))
    return " + ".join(f"{name(cid)} {format_eur(cents)}" for cid, cents in value.items())
