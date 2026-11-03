"""Parse the signed euro amounts used to filter movements into API cents.

Amounts keep the movement's sign: expenses are negative, income positive. So
"expenses over 100 €" is `max=-100`, and an exact `-12,50` finds a 12,50 € charge.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fintonic.errors import InputError

RANGE_SEP = ".."

AmountRange = tuple[int | None, int | None]


def parse_euros(text: str) -> int:
    """'-12,50' or '-12.50' -> -1250 (signed cents, at most two decimals)."""
    cleaned = text.strip().replace(",", ".")
    try:
        euros = Decimal(cleaned)
    except InvalidOperation as exc:
        raise InputError(f"Importe inválido {text!r}: usa euros como 12,50 o -12.50.") from exc
    cents = euros * 100
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise InputError(f"Importe inválido {text!r}: como mucho 2 decimales.")
    return int(cents)


def parse_amount(spec: str) -> AmountRange:
    """'X' -> exactly X; 'X..Y' -> between X and Y (either side may be left open)."""
    low, sep, high = spec.partition(RANGE_SEP)
    if not sep:
        exact = parse_euros(spec)
        return exact, exact
    if not low.strip() and not high.strip():
        raise InputError(f"Rango inválido {spec!r}: usa X..Y, X.. o ..Y.")
    bounds = (parse_euros(low) if low.strip() else None, parse_euros(high) if high.strip() else None)
    if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
        return bounds[1], bounds[0]  # '-50..-100' reads naturally; order it for the user
    return bounds


def amount_bounds(minimum: str | None, maximum: str | None, amount: str | None) -> AmountRange:
    """Combine the CLI's --min/--max/--amount options into (min, max) cents."""
    if amount is not None:
        if minimum is not None or maximum is not None:
            raise InputError("Usa --amount o --min/--max, pero no ambos.")
        return parse_amount(amount)
    low = parse_euros(minimum) if minimum is not None else None
    high = parse_euros(maximum) if maximum is not None else None
    if low is not None and high is not None and low > high:
        raise InputError("--min no puede ser mayor que --max (los gastos son negativos).")
    return low, high
