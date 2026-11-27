"""Test 13: money normalization (Stripe cents <-> QBO decimals)."""
from decimal import Decimal

import pytest

from finrecon import money
from finrecon.http import decode_json


def test_stripe_minor_units_pass_through():
    assert money.from_stripe(123456, "usd") == (123456, "USD")


@pytest.mark.parametrize("bad", [12.5, "100", True, None])
def test_stripe_rejects_non_integer_amounts(bad):
    with pytest.raises(money.MoneyError):
        money.from_stripe(bad, "usd")


@pytest.mark.parametrize("raw, cents", [
    (Decimal("1250.5"), 125050), (Decimal("2512.35"), 251235), (Decimal("0.01"), 1),
    (Decimal("0"), 0), (Decimal("100"), 10000), (Decimal("-45.00"), -4500), ("19.99", 1999),
])
def test_qbo_decimal_to_cents(raw, cents):
    assert money.from_qbo(raw, "USD") == (cents, "USD")


def test_qbo_rejects_float_and_sub_cent_precision():
    with pytest.raises(money.MoneyError):
        money.from_qbo(0.1, "USD")          # a float means JSON was parsed unsafely
    with pytest.raises(money.MoneyError):
        money.from_qbo(Decimal("1.005"), "USD")


def test_unknown_currency_is_rejected_not_guessed():
    with pytest.raises(money.MoneyError):
        money.from_stripe(100, "eur")
    with pytest.raises(money.MoneyError):
        money.from_qbo(Decimal("1"), "XYZ")


def test_round_trip_cents_qbo():
    for cents in (0, 1, 99, 100, 101, 123456789, 251235):
        assert money.from_qbo(money.to_qbo(cents), "USD")[0] == cents


def test_json_decoding_keeps_decimals_exact():
    body = decode_json('{"TotalAmt": 0.1, "Balance": 2512.35}')
    assert body["TotalAmt"] == Decimal("0.1")
    assert money.from_qbo(body["Balance"], "USD")[0] == 251235


@pytest.mark.parametrize("cents, bps, expected", [
    (5, 5000, 3),        # 2.5c rounds half-up to 3c
    (15, 1000, 2),       # 1.5c -> 2c
    (14, 1000, 1),       # 1.4c -> 1c
    (222015, 1500, 33302),
    (0, 1200, 0),
])
def test_percentage_rounding_half_up(cents, bps, expected):
    assert money.pct_of(cents, bps) == expected


def test_format():
    assert money.fmt(123456) == "$1,234.56"
    assert money.fmt(-4500) == "-$45.00"
    assert money.fmt(None) == "—"
