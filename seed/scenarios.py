"""The scenario plan: one deterministic, source-system-neutral description of
everything the demo contains.

The same plan is rendered three ways:
  * seed/generate_offline.py -> API-shaped offline fixtures (QBO, Stripe, ad API)
  * seed/live_seed.py        -> real objects in the QBO and Stripe sandboxes
  * the ground truth         -> tests/fixtures/expected_reconciliation.json

Expected statuses are written here from the *intent* of each scenario. They are
never produced by running the reconciliation engine, so the evaluator is a real
check rather than a tautology.

This module is the "billing system + payment sync" of a fictional agency. It
deliberately does not import anything from finrecon.recon.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from finrecon.money import pct_of

RANDOM_SEED = 20261125
WINDOW_START = date(2026, 7, 1)
WINDOW_END = date(2026, 11, 30)
RECON_AS_OF = date(2026, 11, 30)
FIRST_RUN_AS_OF = date(2026, 11, 25)
BILLED_PERIODS = ["2026-07", "2026-08", "2026-09", "2026-10"]

# Scenario catalog: code -> (title, what it demonstrates, interview question)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "S00": ("Baseline", "Invoice paid in Stripe and synced to QBO with the Stripe ID in the memo.", "Q4 matching"),
    "S01": ("Partial payment", "Customer paid 40% of the invoice; balance remains open.", "Q3 data model"),
    "S02": ("Split payment", "Two Stripe payments together settle one invoice.", "Q3 data model"),
    "S03": ("One payment, two invoices", "A single payment is allocated across two invoices (LinkedTxn).", "Q3 data model"),
    "S04": ("Amount mismatch", "Sync recorded $45 less in QBO than Stripe actually collected.", "Q4 missing/mismatched"),
    "S05": ("Missing in QBO", "Stripe collected the money; the sync never created the QBO payment.", "Q4 missing/mismatched"),
    "S06": ("Non-Stripe payment", "Customer paid by check. No Stripe record is expected.", "Q4 missing/mismatched"),
    "S07": ("Duplicate import", "The sync imported the same Stripe payment twice; the copy sits unapplied.", "Q2 accuracy"),
    "S08": ("Unrecorded refund", "Stripe refund was never reflected in QBO.", "Q4 missing/mismatched"),
    "S09": ("Recorded refund", "Stripe refund recorded in QBO as a refund receipt.", "Q4 matching"),
    "S10": ("Candidate match", "QBO card payment entered by hand without the Stripe ID; one unique candidate.", "Q4 matching"),
    "S11": ("Ambiguous match", "Two identical Stripe deposits, one QBO entry. Needs a human.", "Q4 matching"),
    "S12": ("Missing in Stripe", "QBO card payment references a Stripe payment that does not exist.", "Q4 missing/mismatched"),
    "S13": ("Billing mismatch", "Invoice charged the management fee twice.", "Q5 ad platform -> billing"),
    "S14": ("Pending sync", "Paid in Stripe yesterday; still inside the 2-day sync window.", "Q2 accuracy"),
    "S15": ("Not invoiced", "Ad spend ran in October but no invoice was issued.", "Q5 ad platform -> billing"),
    "S16": ("Open AR", "October invoices not yet due. Normal, not an exception.", "Q3 data model"),
    "D01": ("Genuine zero", "New location, not live yet: zero spend is a real, complete answer.", "Q2 completeness"),
    "D02": ("No data", "Ad API returns 503 for this location every time.", "Q2 completeness"),
    "D03": ("Stale data", "Latest pull failed; last validated data is kept and flagged stale.", "Q2 completeness"),
    "D04": ("Missing page", "Latest pull silently dropped a page.", "Q1 automated pulls"),
    "D05": ("Totals disagree", "Latest pull's detail rows don't add up to the source summary.", "Q2 accuracy"),
    "D06": ("Rate limited", "First request gets HTTP 429; retry succeeds.", "Q1 automated pulls"),
    "D07": ("Duplicate rows", "Ad API repeats rows across pages; de-duplicated by source ID.", "Q2 accuracy"),
}

EXCEPTION_STATUSES = {
    "PENDING_SYNC": False, "MATCHED": False, "PARTIAL": False, "OPEN_AR": False,
    "NON_STRIPE_PAYMENT": False, "MISSING_IN_QBO": True, "MISSING_IN_STRIPE": True,
    "AMOUNT_MISMATCH": True, "DUPLICATE": True, "UNRECORDED_REFUND": True, "NEEDS_REVIEW": True,
}


@dataclass
class Location:
    key: str
    name: str
    ad_location_id: str
    opened_on: date = WINDOW_START


@dataclass
class Client:
    key: str
    name: str
    doc_prefix: str
    ad_client_id: str
    fee_bps: int
    min_fee_cents: int
    discount_bps: int
    tax_bps: int
    rule_description: str
    locations: list[Location]


@dataclass
class SpendRow:
    source_spend_id: str
    client_key: str
    location_key: str
    spend_date: date
    campaign: str
    amount_cents: int


@dataclass
class Invoice:
    seed_key: str
    client_key: str
    location_key: str
    period: str
    doc_number: str
    txn_date: date
    due_date: date
    amount_cents: int
    expected_status: str


@dataclass
class StripePayment:
    seed_key: str
    client_key: str
    location_key: str
    amount_cents: int
    paid_on: date
    description: str
    invoice_keys: list[str]
    expected_status: str


@dataclass
class QboPayment:
    seed_key: str
    client_key: str
    location_key: str | None          # None = parent (client-level) customer
    amount_cents: int
    txn_date: date
    method: str                       # card | check
    stripe_payment_key: str | None    # seed_key of the Stripe payment named in the memo
    ghost_stripe_id: str | None       # memo names an ID that does not exist in Stripe
    allocations: list[tuple[str, int]]
    expected_status: str


@dataclass
class StripeRefund:
    seed_key: str
    payment_key: str
    amount_cents: int
    refunded_on: date
    expected_status: str


@dataclass
class QboRefund:
    seed_key: str
    stripe_refund_key: str
    client_key: str
    location_key: str
    amount_cents: int
    txn_date: date
    expected_status: str


@dataclass
class AdFailure:
    rate_limit_first_request: bool = False
    always_fail: bool = False
    fail_on_runs: list[int] = field(default_factory=list)
    missing_page_on_runs: list[int] = field(default_factory=list)
    summary_mismatch_on_runs: list[int] = field(default_factory=list)
    duplicate_rows: bool = False


@dataclass
class Plan:
    clients: list[Client]
    spend: list[SpendRow]
    invoices: list[Invoice]
    stripe_payments: list[StripePayment]
    qbo_payments: list[QboPayment]
    stripe_refunds: list[StripeRefund]
    qbo_refunds: list[QboRefund]
    ad_failures: dict[str, AdFailure]            # ad_location_id -> failure config
    expected_data_status: dict[str, tuple[str, str]]   # "client/location" -> (scenario, status)
    expected_billing: dict[str, tuple[str, str]]       # "client/location/period" -> (scenario, status)


CLIENTS = [
    Client(
        "harbor", "Harbor Fitness Co.", "HF", "adv-101", 1200, 15000, 0, 0,
        "Ad spend + 12% management fee (minimum $150.00 per location-month)",
        [
            Location("hoboken", "Hoboken", "adv-101-01"),
            Location("jcheights", "Jersey City Heights", "adv-101-02"),
            Location("astoria", "Astoria", "adv-101-03"),
            Location("parkslope", "Park Slope", "adv-101-04"),
            Location("williamsburg", "Williamsburg", "adv-101-05"),
        ],
    ),
    Client(
        "maple", "Maple Leaf Dental Group", "ML", "adv-102", 1000, 20000, 500, 400,
        "Ad spend + 10% management fee (minimum $200.00), 5% loyalty discount on the fee, "
        "illustrative 4% tax on the net fee",
        [
            Location("stamford", "Stamford", "adv-102-01"),
            Location("whiteplains", "White Plains", "adv-102-02"),
            Location("yonkers", "Yonkers", "adv-102-03"),
            Location("newrochelle", "New Rochelle", "adv-102-04"),
        ],
    ),
    Client(
        "sunbeam", "Sunbeam Pizza Franchise", "SP", "adv-103", 1500, 10000, 0, 0,
        "Ad spend + 15% management fee (minimum $100.00 per location-month)",
        [
            Location("newark", "Newark", "adv-103-01"),
            Location("paterson", "Paterson", "adv-103-02"),
            Location("edison", "Edison", "adv-103-03"),
            Location("hackensack", "Hackensack", "adv-103-04"),
            Location("montclair", "Montclair", "adv-103-05"),
            Location("morristown", "Morristown", "adv-103-06", opened_on=date(2026, 12, 1)),
        ],
    ),
]


def billing_components(spend_cents: int, client: Client) -> dict[str, int]:
    """The fictional agency's billing system (independent of the checker)."""
    if spend_cents == 0:
        return {"spend": 0, "fee": 0, "discount": 0, "tax": 0, "total": 0}
    fee = max(pct_of(spend_cents, client.fee_bps), client.min_fee_cents)
    discount = pct_of(fee, client.discount_bps)
    tax = pct_of(fee - discount, client.tax_bps)
    return {"spend": spend_cents, "fee": fee, "discount": discount, "tax": tax,
            "total": spend_cents + fee - discount + tax}


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def build_plan() -> Plan:
    rng = random.Random(RANDOM_SEED)

    # ------------------------------------------------------------ ad spend
    spend: list[SpendRow] = []
    for c in CLIENTS:
        for loc in c.locations:
            for d in _daterange(WINDOW_START, WINDOW_END):
                for campaign in ("search", "social"):
                    amount = rng.randint(1500, 6000)
                    if d < loc.opened_on:
                        continue
                    spend.append(SpendRow(f"sp-{loc.ad_location_id}-{d.isoformat()}-{campaign}",
                                          c.key, loc.key, d, campaign, amount))

    def period_spend(ck: str, lk: str, period: str) -> int:
        return sum(s.amount_cents for s in spend
                   if s.client_key == ck and s.location_key == lk and s.spend_date.isoformat()[:7] == period)

    # ------------------------------------------------------------ invoices
    invoices: dict[str, Invoice] = {}
    expected_billing: dict[str, tuple[str, str]] = {}
    for c in CLIENTS:
        for loc in c.locations:
            for period in BILLED_PERIODS:
                bkey = f"{c.key}/{loc.key}/{period}"
                spent = period_spend(c.key, loc.key, period)
                if spent == 0:
                    expected_billing[bkey] = ("D01", "NO_INVOICE_EXPECTED")
                    continue
                if (c.key, loc.key, period) == ("maple", "newrochelle", "2026-10"):
                    expected_billing[bkey] = ("S15", "NOT_INVOICED")
                    continue
                amount = billing_components(spent, c)["total"]
                scenario = "S00"
                if (c.key, loc.key, period) == ("sunbeam", "hackensack", "2026-09"):
                    amount += billing_components(spent, c)["fee"]      # fee billed twice
                    scenario = "S13"
                expected_billing[bkey] = (scenario, "BILLING_MATCHED" if scenario == "S00" else "BILLING_MISMATCH")
                year, month = int(period[:4]), int(period[5:])
                txn = date(year + (month == 12), month % 12 + 1, 1)
                inv = Invoice(
                    seed_key=f"{'S16' if period == '2026-10' else scenario}:inv:{c.key}:{loc.key}:{period}",
                    client_key=c.key, location_key=loc.key, period=period,
                    doc_number=f"{c.doc_prefix}-{loc.key[:4].upper()}-{period[2:4]}{period[5:]}",
                    txn_date=txn, due_date=txn + timedelta(days=30), amount_cents=amount,
                    expected_status="OPEN_AR" if period == "2026-10" else "MATCHED",
                )
                invoices[f"{c.key}/{loc.key}/{period}"] = inv

    def inv(ck, lk, period) -> Invoice:
        return invoices[f"{ck}/{lk}/{period}"]

    def retag(invoice: Invoice, code: str, status: str) -> None:
        invoice.seed_key = code + invoice.seed_key[3:]
        invoice.expected_status = status

    sps: list[StripePayment] = []
    qps: list[QboPayment] = []
    s_refunds: list[StripeRefund] = []
    q_refunds: list[QboRefund] = []

    def stripe(code, ck, lk, amount, paid_on, invoice_list, status, desc=None, suffix="sp") -> StripePayment:
        sp = StripePayment(
            seed_key=f"{code}:{suffix}:{ck}:{lk}:{paid_on.isoformat()}", client_key=ck, location_key=lk,
            amount_cents=amount, paid_on=paid_on,
            description=desc or "Invoice payment " + ", ".join(i.doc_number for i in invoice_list),
            invoice_keys=[i.seed_key for i in invoice_list], expected_status=status,
        )
        sps.append(sp)
        return sp

    def sync(code, sp: StripePayment | None, ck, lk, amount, txn_date, allocations, status,
             method="card", ghost=None, suffix="qp") -> QboPayment:
        qp = QboPayment(
            seed_key=f"{code}:{suffix}:{ck}:{lk or 'client'}:{txn_date.isoformat()}", client_key=ck,
            location_key=lk, amount_cents=amount, txn_date=txn_date, method=method,
            stripe_payment_key=sp.seed_key if sp else None, ghost_stripe_id=ghost,
            allocations=[(i.seed_key, a) for i, a in allocations], expected_status=status,
        )
        qps.append(qp)
        return qp

    special = {
        ("harbor", "hoboken", "2026-09"), ("harbor", "jcheights", "2026-09"),
        ("harbor", "astoria", "2026-09"), ("harbor", "astoria", "2026-10"),
        ("harbor", "williamsburg", "2026-09"), ("harbor", "parkslope", "2026-10"),
        ("maple", "stamford", "2026-09"), ("maple", "whiteplains", "2026-09"),
        ("maple", "yonkers", "2026-09"), ("maple", "newrochelle", "2026-09"),
        ("sunbeam", "newark", "2026-09"), ("sunbeam", "paterson", "2026-09"),
        ("sunbeam", "montclair", "2026-09"),
    }

    # ------------------------------------------------------------ baseline (S00)
    for key, i in invoices.items():
        ck, lk, period = key.split("/")
        if period == "2026-10" or (ck, lk, period) in special:
            continue
        paid_on = i.txn_date + timedelta(days=rng.randint(6, 26))
        sp = stripe("S00", ck, lk, i.amount_cents, paid_on, [i], "MATCHED")
        sync("S00", sp, ck, lk, i.amount_cents, paid_on + timedelta(days=1), [(i, i.amount_cents)], "MATCHED")

    # S01 partial payment
    i = inv("harbor", "hoboken", "2026-09")
    retag(i, "S01", "PARTIAL")
    part = pct_of(i.amount_cents, 4000)
    sp = stripe("S01", "harbor", "hoboken", part, date(2026, 10, 15), [i], "MATCHED")
    sync("S01", sp, "harbor", "hoboken", part, date(2026, 10, 16), [(i, part)], "MATCHED")

    # S02 split payment
    i = inv("harbor", "jcheights", "2026-09")
    retag(i, "S02", "MATCHED")
    first = i.amount_cents // 2
    second = i.amount_cents - first
    for paid_on, amt in ((date(2026, 10, 12), first), (date(2026, 10, 26), second)):
        sp = stripe("S02", "harbor", "jcheights", amt, paid_on, [i], "MATCHED")
        sync("S02", sp, "harbor", "jcheights", amt, paid_on + timedelta(days=1), [(i, amt)], "MATCHED")

    # S03 one payment, two invoices (same location: QBO applies payments per customer)
    i1, i2 = inv("harbor", "astoria", "2026-09"), inv("harbor", "astoria", "2026-10")
    retag(i1, "S03", "MATCHED")
    retag(i2, "S03", "MATCHED")
    total = i1.amount_cents + i2.amount_cents
    sp = stripe("S03", "harbor", "astoria", total, date(2026, 11, 12), [i1, i2], "MATCHED")
    sync("S03", sp, "harbor", "astoria", total, date(2026, 11, 13),
         [(i1, i1.amount_cents), (i2, i2.amount_cents)], "MATCHED")

    # S04 amount mismatch: QBO recorded $45.00 less than Stripe collected
    i = inv("harbor", "williamsburg", "2026-09")
    retag(i, "S04", "PARTIAL")
    sp = stripe("S04", "harbor", "williamsburg", i.amount_cents, date(2026, 10, 18), [i], "AMOUNT_MISMATCH")
    short = i.amount_cents - 4500
    sync("S04", sp, "harbor", "williamsburg", short, date(2026, 10, 19), [(i, short)], "AMOUNT_MISMATCH")

    # S05 missing in QBO
    i = inv("maple", "stamford", "2026-09")
    retag(i, "S05", "OPEN_AR")
    stripe("S05", "maple", "stamford", i.amount_cents, date(2026, 10, 20), [i], "MISSING_IN_QBO")

    # S06 check payment, no Stripe record expected
    i = inv("maple", "whiteplains", "2026-09")
    retag(i, "S06", "MATCHED")
    sync("S06", None, "maple", "whiteplains", i.amount_cents, date(2026, 10, 18),
         [(i, i.amount_cents)], "NON_STRIPE_PAYMENT", method="check")

    # S07 duplicate import: second copy is unapplied
    i = inv("maple", "yonkers", "2026-09")
    retag(i, "S07", "MATCHED")
    sp = stripe("S07", "maple", "yonkers", i.amount_cents, date(2026, 10, 14), [i], "MATCHED")
    sync("S07", sp, "maple", "yonkers", i.amount_cents, date(2026, 10, 15), [(i, i.amount_cents)], "MATCHED")
    sync("S07", sp, "maple", "yonkers", i.amount_cents, date(2026, 10, 15), [], "DUPLICATE", suffix="qp-dup")

    # S08 refund in Stripe, never recorded in QBO
    i = inv("maple", "newrochelle", "2026-09")
    retag(i, "S08", "MATCHED")
    sp = stripe("S08", "maple", "newrochelle", i.amount_cents, date(2026, 10, 10), [i], "MATCHED")
    sync("S08", sp, "maple", "newrochelle", i.amount_cents, date(2026, 10, 11), [(i, i.amount_cents)], "MATCHED")
    s_refunds.append(StripeRefund("S08:re:maple:newrochelle", sp.seed_key, 20000, date(2026, 10, 25),
                                  "UNRECORDED_REFUND"))

    # S09 refund recorded in both systems
    i = inv("sunbeam", "newark", "2026-09")
    retag(i, "S09", "MATCHED")
    sp = stripe("S09", "sunbeam", "newark", i.amount_cents, date(2026, 10, 9), [i], "MATCHED")
    sync("S09", sp, "sunbeam", "newark", i.amount_cents, date(2026, 10, 10), [(i, i.amount_cents)], "MATCHED")
    s_refunds.append(StripeRefund("S09:re:sunbeam:newark", sp.seed_key, 15000, date(2026, 10, 22), "MATCHED"))
    q_refunds.append(QboRefund("S09:qre:sunbeam:newark", "S09:re:sunbeam:newark", "sunbeam", "newark",
                               15000, date(2026, 10, 23), "MATCHED"))

    # S10 hand-entered QBO card payment without the Stripe ID -> unique candidate
    i = inv("sunbeam", "paterson", "2026-09")
    retag(i, "S10", "MATCHED")
    stripe("S10", "sunbeam", "paterson", i.amount_cents, date(2026, 10, 16), [i], "MATCHED")
    sync("S10", None, "sunbeam", "paterson", i.amount_cents, date(2026, 10, 17), [(i, i.amount_cents)], "MATCHED")

    # S11 two identical Stripe deposits, one QBO entry -> ambiguous
    stripe("S11", "sunbeam", "edison", 50000, date(2026, 10, 21), [], "NEEDS_REVIEW",
           desc="Creative production deposit")
    stripe("S11", "sunbeam", "hackensack", 50000, date(2026, 10, 22), [], "NEEDS_REVIEW",
           desc="Creative production deposit")
    sync("S11", None, "sunbeam", None, 50000, date(2026, 10, 22), [], "NEEDS_REVIEW")

    # S12 QBO card payment naming a Stripe payment that does not exist
    i = inv("harbor", "parkslope", "2026-10")
    retag(i, "S12", "MATCHED")
    sync("S12", None, "harbor", "parkslope", i.amount_cents, date(2026, 11, 10), [(i, i.amount_cents)],
         "MISSING_IN_STRIPE", ghost="pi_3Qx9GhostNotInStripe0000")

    # S14 paid in Stripe the day before as-of; not yet synced
    i = inv("sunbeam", "montclair", "2026-09")
    retag(i, "S14", "OPEN_AR")
    stripe("S14", "sunbeam", "montclair", i.amount_cents, date(2026, 11, 29), [i], "PENDING_SYNC")

    # ------------------------------------------------------------ ad API failures
    failures = {
        "adv-101-01": AdFailure(rate_limit_first_request=True),       # D06 Hoboken
        "adv-103-02": AdFailure(always_fail=True),                    # D02 Paterson
        "adv-101-03": AdFailure(fail_on_runs=[2]),                    # D03 Astoria
        "adv-102-03": AdFailure(missing_page_on_runs=[2]),            # D04 Yonkers
        "adv-102-02": AdFailure(summary_mismatch_on_runs=[2]),        # D05 White Plains
        "adv-103-01": AdFailure(duplicate_rows=True),                 # D07 Newark
    }
    data_status_overrides = {
        "sunbeam/morristown": ("D01", "ZERO_TRANSACTIONS"),
        "sunbeam/paterson": ("D02", "NO_DATA"),
        "harbor/astoria": ("D03", "STALE"),
        "maple/yonkers": ("D04", "INCOMPLETE"),
        "maple/whiteplains": ("D05", "INCOMPLETE"),
        "harbor/hoboken": ("D06", "VALIDATED"),
        "sunbeam/newark": ("D07", "VALIDATED"),
    }
    expected_data_status = {}
    for c in CLIENTS:
        for loc in c.locations:
            k = f"{c.key}/{loc.key}"
            expected_data_status[k] = data_status_overrides.get(k, ("S00", "VALIDATED"))

    # Billing checks for a location whose spend never arrived cannot be evaluated.
    for period in BILLED_PERIODS:
        expected_billing[f"sunbeam/paterson/{period}"] = ("D02", "BILLING_DATA_UNAVAILABLE")

    return Plan(CLIENTS, spend, list(invoices.values()), sps, qps, s_refunds, q_refunds,
                failures, expected_data_status, expected_billing)


def ground_truth(plan: Plan) -> dict:
    tx = []
    for i in plan.invoices:
        tx.append({"record_type": "invoice", "seed_key": i.seed_key, "expected": i.expected_status})
    for p in plan.stripe_payments:
        tx.append({"record_type": "stripe_payment", "seed_key": p.seed_key, "expected": p.expected_status})
    for p in plan.qbo_payments:
        tx.append({"record_type": "qbo_payment", "seed_key": p.seed_key, "expected": p.expected_status})
    for r in plan.stripe_refunds:
        tx.append({"record_type": "stripe_refund", "seed_key": r.seed_key, "expected": r.expected_status})
    for r in plan.qbo_refunds:
        tx.append({"record_type": "qbo_refund", "seed_key": r.seed_key, "expected": r.expected_status})
    for t in tx:
        t["scenario"] = t["seed_key"].split(":")[0]
    return {
        "as_of": RECON_AS_OF.isoformat(),
        "note": "Written from scenario intent in seed/scenarios.py, not from engine output.",
        "scenarios": {k: {"title": v[0], "description": v[1], "interview_question": v[2]}
                      for k, v in SCENARIOS.items()},
        "transactions": sorted(tx, key=lambda t: (t["record_type"], t["seed_key"])),
        "data_status": [{"scope": k, "scenario": v[0], "expected": v[1]}
                        for k, v in sorted(plan.expected_data_status.items())],
        "billing": [{"check_key": k, "scenario": v[0], "expected": v[1]}
                    for k, v in sorted(plan.expected_billing.items())],
    }
