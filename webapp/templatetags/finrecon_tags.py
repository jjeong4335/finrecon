from django import template

from finrecon.money import fmt

register = template.Library()

GOOD = {"MATCHED", "VALIDATED", "BILLING_MATCHED", "ZERO_TRANSACTIONS", "NO_INVOICE_EXPECTED", "NON_STRIPE_PAYMENT",
        "succeeded", "both", "PASS", "correct", "LINKED", "VALID_SALE", "completed"}
NEUTRAL = {"OPEN_AR", "PARTIAL", "PENDING_SYNC", "info", "CANCELLATION", "NON_PRODUCT"}
WARN = {"STALE", "INCOMPLETE", "NEEDS_REVIEW", "BILLING_DATA_UNAVAILABLE", "incomplete", "warning", "only_in_stripe",
        "only_in_qbo", "NO_PRIOR_SALE", "MISSING_CUSTOMER", "STOCK_ADJUSTMENT", "ADJUSTMENT"}


@register.filter
def money(cents, currency="USD"):
    return fmt(cents, currency or "USD")


@register.filter
def gbp(cents):
    return fmt(cents, "GBP")


@register.filter
def pill(status):
    s = "" if status is None else str(status)
    cls = "ok" if s in GOOD else "neu" if s in NEUTRAL else "warn" if s in WARN else "bad"
    return cls


@register.filter
def pct(value, digits=1):
    return "—" if value is None else f"{value * 100:.{int(digits)}f}%"


@register.filter
def intcomma(value):
    return "—" if value is None else f"{int(value):,}"
