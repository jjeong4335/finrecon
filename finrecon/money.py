"""The one place where money changes representation.

Internally every amount is an integer number of minor units (cents) plus an
ISO currency code. Integers are exact, compare cheaply, and behave the same in
SQLite and PostgreSQL (BIGINT). This mirrors how Stripe itself stores money.

* Stripe sends integer minor units already -> `from_stripe`
* QuickBooks sends decimal major units (e.g. 1250.5) -> `from_qbo`
  (JSON must be parsed with `parse_float=Decimal`, never as float)
* The mock ad API sends integer cents -> no conversion needed
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Currencies with no minor unit would need a different exponent. The demo is
# USD-only, and anything else is rejected rather than guessed.
_EXPONENT = {"USD": 2}


class MoneyError(ValueError):
    pass


def normalize_currency(code: str | None) -> str:
    if not code:
        raise MoneyError("missing currency")
    code = code.strip().upper()
    if code not in _EXPONENT:
        raise MoneyError(f"unsupported currency {code!r} (no conversion policy defined)")
    return code


def from_stripe(amount: int, currency: str) -> tuple[int, str]:
    """Stripe amounts are already integer minor units."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise MoneyError(f"Stripe amount must be an integer number of minor units, got {amount!r}")
    return amount, normalize_currency(currency)


def from_qbo(amount, currency: str | None) -> tuple[int, str]:
    """QuickBooks amounts are decimal major units, e.g. Decimal('1250.50')."""
    if isinstance(amount, float):
        raise MoneyError("QuickBooks amount arrived as float; parse JSON with parse_float=Decimal")
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"invalid QuickBooks amount {amount!r}") from exc
    cur = normalize_currency(currency or "USD")
    exponent = _EXPONENT[cur]
    scaled = value.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"QuickBooks amount {amount!r} has more than {exponent} decimal places")
    return int(scaled), cur


def to_qbo(cents: int, currency: str = "USD") -> Decimal:
    """Integer cents -> Decimal major units for QuickBooks request bodies."""
    return Decimal(cents).scaleb(-_EXPONENT[normalize_currency(currency)])


def pct_of(cents: int, basis_points: int) -> int:
    """Percentage of an amount, rounded half-up to the cent (bps: 1250 = 12.5%)."""
    return int((Decimal(cents) * Decimal(basis_points) / Decimal(10000)).quantize(Decimal(1), ROUND_HALF_UP))


def fmt(cents: int | None, currency: str = "USD") -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{sign}{symbol}{cents // 100:,}.{cents % 100:02d}"
