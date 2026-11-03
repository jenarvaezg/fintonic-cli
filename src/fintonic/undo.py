"""Decide whether the last logged change of a transaction can be reverted, and to what."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fintonic.changelog import ChangeRecord
from fintonic.changes import LABELS, Field, current_value, describe_value
from fintonic.errors import InputError
from fintonic.models import Category, Transaction


class UndoRefused(InputError):
    """The change cannot be reverted safely; the message says why."""


@dataclass(frozen=True)
class UndoPlan:
    record: ChangeRecord
    field: Field
    current: Any
    target: Any

    @property
    def noop(self) -> bool:
        return self.current == self.target


def no_history(tx_id: str) -> UndoRefused:
    return UndoRefused(f"No hay cambios registrados del movimiento {tx_id} en este equipo (`fintonic tx history`).")


def plan_undo(record: ChangeRecord, tx: Transaction, categories: Mapping[str, Category] | None = None) -> UndoPlan:
    """Revert `record` on `tx` (its current state), or raise `UndoRefused`."""
    if record.rule:
        raise UndoRefused(
            "Ese cambio se hizo con `set-category --similar` y creó una regla que afecta a otros movimientos: "
            "no se puede deshacer desde aquí. Revisa la regla en la app de Fintonic."
        )
    try:
        field = Field(record.field)
    except ValueError as exc:
        raise UndoRefused(f"No sé deshacer cambios de tipo {record.field!r}.") from exc

    current = current_value(tx, field)
    if current != record.after:
        raise UndoRefused(
            f"El movimiento ha cambiado desde el último cambio registrado ({record.timestamp}): "
            f"su {LABELS[field]} es {describe_value(field, current, categories)} y se dejó en "
            f"{describe_value(field, record.after, categories)}. No lo deshago para no pisar ese otro cambio."
        )

    target = record.before
    if field is Field.category:
        _check_category_target(target, current, tx)
    elif field is Field.user_date and not target:
        raise UndoRefused("No se conoce la fecha anterior del movimiento.")
    return UndoPlan(record=record, field=field, current=current, target=target)


def _check_category_target(target: Any, current: dict[str, int], tx: Transaction) -> None:
    if not isinstance(target, dict) or not target:
        raise UndoRefused("No se conoce la categoría anterior del movimiento.")
    if len(target) == 1 and len(current) > 1:
        raise UndoRefused(
            "Habría que volver a unir un movimiento dividido y aún no se sabe si la API lo permite (#3). "
            "Hazlo desde la app de Fintonic."
        )
    if len(target) > 1 and sum(target.values()) != tx.quantity:
        raise UndoRefused("Las partes anteriores ya no suman el importe del movimiento: no se puede volver a dividir.")
