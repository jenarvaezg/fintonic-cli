import pytest

from fintonic.amounts import amount_bounds, parse_amount, parse_euros
from fintonic.errors import InputError


@pytest.mark.parametrize(
    ("text", "cents"),
    [("12,50", 1250), ("12.50", 1250), ("-12,5", -1250), (" 100 ", 10000), ("-0,01", -1), ("0", 0)],
)
def test_parse_euros(text: str, cents: int) -> None:
    assert parse_euros(text) == cents


@pytest.mark.parametrize("text", ["", "doce", "1,234", "1.234,56", "nan", "inf"])
def test_parse_euros_rejects_garbage(text: str) -> None:
    with pytest.raises(InputError):
        parse_euros(text)


@pytest.mark.parametrize(
    ("spec", "bounds"),
    [
        ("-12,50", (-1250, -1250)),
        ("-100..-50", (-10000, -5000)),
        ("-50..-100", (-10000, -5000)),
        ("12.50..20", (1250, 2000)),
        ("100..", (10000, None)),
        ("..-20", (None, -2000)),
    ],
)
def test_parse_amount(spec: str, bounds: tuple[int | None, int | None]) -> None:
    assert parse_amount(spec) == bounds


@pytest.mark.parametrize("spec", ["..", "a..b", "1..2..3"])
def test_parse_amount_rejects_garbage(spec: str) -> None:
    with pytest.raises(InputError):
        parse_amount(spec)


def test_amount_bounds_combines_options() -> None:
    assert amount_bounds(None, None, None) == (None, None)
    assert amount_bounds("-100", "-12,50", None) == (-10000, -1250)
    assert amount_bounds(None, None, "-5") == (-500, -500)


def test_amount_bounds_rejects_conflicts() -> None:
    with pytest.raises(InputError, match="--amount"):
        amount_bounds("1", None, "5")
    with pytest.raises(InputError, match="--min"):
        amount_bounds("10", "-10", None)
