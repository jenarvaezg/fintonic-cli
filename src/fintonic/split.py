"""Build and validate the body for `POST rest/transaction/{id}/split`.

The web app sends `{"categoryIds": [...], "categoryAmounts": [...]}` where each amount is
in cents carrying the transaction's sign, and the parts must add up to the total. Its UI
allows at most three parts.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from fintonic.errors import InputError

MAX_PARTS = 3


class SplitError(InputError):
    pass


def parse_part(spec: str) -> tuple[str, int]:
    """'G0898:85,50' -> ('G0898', 8550). Amounts are positive euros."""
    category_id, sep, amount = spec.partition(":")
    if not sep or not category_id or not amount:
        raise SplitError(f"Parte inválida {spec!r}: usa CATEGORIA:IMPORTE, p.ej. G0898:85,50")
    try:
        euros = Decimal(amount.replace(",", "."))
    except InvalidOperation as exc:
        raise SplitError(f"Importe inválido en {spec!r}") from exc
    cents = euros * 100
    if cents != cents.to_integral_value() or cents <= 0:
        raise SplitError(f"Importe inválido en {spec!r}: positivo y con 2 decimales como mucho.")
    return category_id.strip().upper(), int(cents)


def build_split_body(total_cents: int, parts: Sequence[tuple[str, int]]) -> dict[str, list]:
    if not 2 <= len(parts) <= MAX_PARTS:
        raise SplitError(f"Un movimiento se divide en 2 o {MAX_PARTS} partes (la web no admite más).")
    ids = [cid for cid, _ in parts]
    if len(set(ids)) != len(ids):
        raise SplitError("Hay categorías repetidas.")
    if any(cents <= 0 for _, cents in parts):
        raise SplitError("Cada parte debe ser un importe positivo.")
    expected = abs(total_cents)
    got = sum(cents for _, cents in parts)
    if got != expected:
        raise SplitError(f"Las partes suman {got / 100:.2f} € y el movimiento es de {expected / 100:.2f} €.")
    sign = -1 if total_cents < 0 else 1
    return {"categoryIds": ids, "categoryAmounts": [sign * cents for _, cents in parts]}
