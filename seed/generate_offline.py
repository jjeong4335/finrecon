"""Render the scenario plan into offline fixtures shaped like real API objects.

QuickBooks entities follow the Accounting API JSON (Id, TxnDate, TotalAmt,
CustomerRef, Line[].LinkedTxn ...); Stripe objects follow the v1 API
(payment_intent, charge, refund, balance_transaction, payout). The fixture
transports serve them through the *same* pagination code the live adapters use.

Keys starting with "_fixture_" are bookkeeping for the fake transport and are
stripped before an object is served.

Usage:  python -m seed.generate_offline
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from finrecon.config import ROOT
from finrecon.money import pct_of, to_qbo
from seed.scenarios import RECON_AS_OF, Plan, build_plan, ground_truth

OUT = ROOT / "fixtures" / "offline"
GROUND_TRUTH = ROOT / "tests" / "fixtures" / "expected_reconciliation.json"

STRIPE_FEE_BPS = 290       # illustrative card pricing: 2.9% + 30c
STRIPE_FEE_FIXED = 30


def _h(*parts: str, n: int = 24) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:n]


def _unix(d: date, hour: int = 15) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


def _qbo_time(d: date) -> str:
    return f"{d.isoformat()}T09:00:00-08:00"


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)   # serialized as JSON number; read back with parse_float=Decimal
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, cls=_Encoder, sort_keys=False)
    path.write_text(text + "\n")


def render(plan: Plan) -> dict[str, object]:
    clients = {c.key: c for c in plan.clients}
    loc_name = {(c.key, l.key): l.name for c in plan.clients for l in c.locations}

    # ------------------------------------------------------------ IDs
    qbo_parent = {c.key: str(i + 1) for i, c in enumerate(plan.clients)}
    qbo_sub, n = {}, 10
    for c in plan.clients:
        for loc in c.locations:
            n += 1
            qbo_sub[(c.key, loc.key)] = str(n)
    stripe_cus = {c.key: "cus_" + _h("cus", c.key, n=14) for c in plan.clients}

    registry = {"clients": []}
    for c in plan.clients:
        registry["clients"].append({
            "client_key": c.key, "name": c.name,
            "billing_rule": {"mgmt_fee_bps": c.fee_bps, "min_fee_cents": c.min_fee_cents,
                             "discount_bps": c.discount_bps, "tax_bps": c.tax_bps,
                             "currency": "USD", "description": c.rule_description},
            "external": {"qbo_customer_id": qbo_parent[c.key], "stripe_customer_id": stripe_cus[c.key],
                         "ad_client_id": c.ad_client_id},
            "locations": [{"location_key": l.key, "name": l.name, "opened_on": l.opened_on.isoformat(),
                           "external": {"qbo_customer_id": qbo_sub[(c.key, l.key)],
                                        "ad_location_id": l.ad_location_id}} for l in c.locations],
        })

    # ------------------------------------------------------------ QBO customers & methods
    customers = []
    for c in plan.clients:
        customers.append({"Id": qbo_parent[c.key], "DisplayName": c.name, "CompanyName": c.name,
                          "Job": False, "Active": True, "CurrencyRef": {"value": "USD"},
                          "MetaData": {"CreateTime": _qbo_time(date(2026, 6, 1))}})
        for loc in c.locations:
            customers.append({"Id": qbo_sub[(c.key, loc.key)], "DisplayName": f"{c.name} - {loc.name}",
                              "FullyQualifiedName": f"{c.name}:{loc.name}", "Job": True,
                              "ParentRef": {"value": qbo_parent[c.key]}, "BillWithParent": False,
                              "Active": True, "CurrencyRef": {"value": "USD"},
                              "MetaData": {"CreateTime": _qbo_time(date(2026, 6, 1))}})
    methods = [
        {"Id": "1", "Name": "Credit Card (Stripe)", "Type": "CREDIT_CARD", "Active": True},
        {"Id": "2", "Name": "Check", "Type": "NON_CREDIT_CARD", "Active": True},
        {"Id": "3", "Name": "ACH", "Type": "NON_CREDIT_CARD", "Active": True},
    ]

    # ------------------------------------------------------------ QBO invoices
    inv_by_key = {i.seed_key: i for i in plan.invoices}
    qbo_inv_id = {i.seed_key: str(1001 + n) for n, i in enumerate(plan.invoices)}
    applied: dict[str, int] = {k: 0 for k in inv_by_key}
    for qp in plan.qbo_payments:
        for ik, amt in qp.allocations:
            applied[ik] += amt

    invoices = []
    for i in plan.invoices:
        invoices.append({
            "Id": qbo_inv_id[i.seed_key], "SyncToken": "0", "DocNumber": i.doc_number,
            "TxnDate": i.txn_date, "DueDate": i.due_date,
            "CustomerRef": {"value": qbo_sub[(i.client_key, i.location_key)],
                            "name": f"{clients[i.client_key].name} - {loc_name[(i.client_key, i.location_key)]}"},
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
            "TotalAmt": to_qbo(i.amount_cents), "Balance": to_qbo(i.amount_cents - applied[i.seed_key]),
            "PrivateNote": f"period={i.period}; seed_key={i.seed_key}",
            "Line": [
                {"Id": "1", "LineNum": 1, "Amount": to_qbo(i.amount_cents), "DetailType": "SalesItemLineDetail",
                 "Description": f"Advertising services {i.period} (ad spend + management fee)",
                 "SalesItemLineDetail": {"ItemRef": {"value": "1", "name": "Advertising Services"}}},
                {"Amount": to_qbo(i.amount_cents), "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}},
            ],
            "MetaData": {"CreateTime": _qbo_time(i.txn_date), "LastUpdatedTime": _qbo_time(i.txn_date)},
        })

    # ------------------------------------------------------------ Stripe payments
    pi_id = {p.seed_key: "pi_" + _h("pi", p.seed_key) for p in plan.stripe_payments}
    payment_intents, charges, bts, refunds = [], [], [], []
    charge_of: dict[str, dict] = {}
    for p in plan.stripe_payments:
        ch = "ch_" + _h("ch", p.seed_key)
        txn = "txn_" + _h("txn", p.seed_key)
        created = _unix(p.paid_on)
        fee = pct_of(p.amount_cents, STRIPE_FEE_BPS) + STRIPE_FEE_FIXED
        docs = ",".join(inv_by_key[k].doc_number for k in p.invoice_keys)
        metadata = {"seed_key": p.seed_key, "scenario_id": p.seed_key.split(":")[0],
                    "location_key": f"{p.client_key}/{p.location_key}", "event_date": p.paid_on.isoformat()}
        if docs:
            metadata["qbo_invoice_no"] = docs
        payment_intents.append({
            "id": pi_id[p.seed_key], "object": "payment_intent", "amount": p.amount_cents,
            "amount_received": p.amount_cents, "currency": "usd", "customer": stripe_cus[p.client_key],
            "status": "succeeded", "created": created, "description": p.description,
            "latest_charge": ch, "livemode": False, "metadata": metadata,
        })
        charge = {"id": ch, "object": "charge", "amount": p.amount_cents, "amount_refunded": 0,
                  "balance_transaction": txn, "payment_intent": pi_id[p.seed_key],
                  "customer": stripe_cus[p.client_key], "created": created, "currency": "usd",
                  "status": "succeeded", "paid": True, "refunded": False, "livemode": False,
                  "metadata": {}}
        charges.append(charge)
        charge_of[p.seed_key] = charge
        bts.append({"id": txn, "object": "balance_transaction", "type": "charge", "source": ch,
                    "amount": p.amount_cents, "fee": fee, "net": p.amount_cents - fee, "currency": "usd",
                    "created": created, "available_on": _unix(p.paid_on + timedelta(days=2), 0),
                    "status": "available", "fee_details": [
                        {"amount": fee, "currency": "usd", "type": "stripe_fee", "description": "Stripe processing fees"}]})

    re_id = {r.seed_key: "re_" + _h("re", r.seed_key) for r in plan.stripe_refunds}
    for r in plan.stripe_refunds:
        ch = charge_of[r.payment_key]
        ch["amount_refunded"] += r.amount_cents
        txn = "txn_" + _h("txn", r.seed_key)
        created = _unix(r.refunded_on)
        refunds.append({"id": re_id[r.seed_key], "object": "refund", "amount": r.amount_cents,
                        "charge": ch["id"], "payment_intent": ch["payment_intent"], "currency": "usd",
                        "created": created, "status": "succeeded", "balance_transaction": txn,
                        "metadata": {"seed_key": r.seed_key, "scenario_id": r.seed_key.split(":")[0]}})
        bts.append({"id": txn, "object": "balance_transaction", "type": "refund", "source": re_id[r.seed_key],
                    "amount": -r.amount_cents, "fee": 0, "net": -r.amount_cents, "currency": "usd",
                    "created": created, "available_on": created, "status": "available", "fee_details": []})

    # Simulated automatic weekly payouts (Fridays): sweep everything available.
    payouts = []
    paid_out: set[str] = set()
    friday = date(2026, 7, 3)
    while friday <= RECON_AS_OF:
        cutoff = _unix(friday, 23)
        batch = [b for b in bts if b["type"] in ("charge", "refund") and b["id"] not in paid_out
                 and b["available_on"] <= cutoff]
        amount = sum(b["net"] for b in batch)
        if batch and amount > 0:
            po = "po_" + _h("po", friday.isoformat())
            for b in batch:
                paid_out.add(b["id"])
                b["_fixture_payout"] = po
            created = _unix(friday, 12)
            payouts.append({"id": po, "object": "payout", "amount": amount, "currency": "usd",
                            "arrival_date": _unix(friday + timedelta(days=2), 0), "created": created,
                            "status": "paid", "automatic": True, "method": "standard",
                            "metadata": {"simulated": "true"}})
            bts.append({"id": "txn_" + _h("txn-po", po), "object": "balance_transaction", "type": "payout",
                        "source": po, "amount": -amount, "fee": 0, "net": -amount, "currency": "usd",
                        "created": created, "available_on": created, "status": "available",
                        "fee_details": [], "_fixture_payout": po})
        friday += timedelta(days=7)

    stripe_customers = [{"id": stripe_cus[c.key], "object": "customer", "name": c.name,
                         "created": _unix(date(2026, 6, 1)), "livemode": False,
                         "metadata": {"client_key": c.key}} for c in plan.clients]

    # ------------------------------------------------------------ QBO payments & refunds
    qbo_payments = []
    for n, qp in enumerate(plan.qbo_payments):
        if qp.stripe_payment_key:
            note = f"Synced from Stripe. stripe_payment_intent={pi_id[qp.stripe_payment_key]}; seed_key={qp.seed_key}"
            ref = ""
        elif qp.ghost_stripe_id:
            note = f"Synced from Stripe. stripe_payment_intent={qp.ghost_stripe_id}; seed_key={qp.seed_key}"
            ref = ""
        elif qp.method == "check":
            note = f"Check deposited at branch. seed_key={qp.seed_key}"
            ref = "10482"
        else:
            note = f"Card payment entered manually. seed_key={qp.seed_key}"
            ref = ""
        customer = qbo_sub[(qp.client_key, qp.location_key)] if qp.location_key else qbo_parent[qp.client_key]
        allocated = sum(a for _, a in qp.allocations)
        qbo_payments.append({
            "Id": str(5001 + n), "SyncToken": "0", "TxnDate": qp.txn_date,
            "CustomerRef": {"value": customer}, "CurrencyRef": {"value": "USD"},
            "TotalAmt": to_qbo(qp.amount_cents), "UnappliedAmt": to_qbo(qp.amount_cents - allocated),
            "PaymentMethodRef": {"value": "2" if qp.method == "check" else "1"},
            "PaymentRefNum": ref, "PrivateNote": note,
            "Line": [{"Amount": to_qbo(a), "LinkedTxn": [{"TxnId": qbo_inv_id[ik], "TxnType": "Invoice"}]}
                     for ik, a in qp.allocations],
            "MetaData": {"CreateTime": _qbo_time(qp.txn_date), "LastUpdatedTime": _qbo_time(qp.txn_date)},
        })

    qbo_refunds = []
    for n, r in enumerate(plan.qbo_refunds):
        qbo_refunds.append({
            "Id": str(7001 + n), "SyncToken": "0", "TxnDate": r.txn_date,
            "CustomerRef": {"value": qbo_sub[(r.client_key, r.location_key)]}, "CurrencyRef": {"value": "USD"},
            "TotalAmt": to_qbo(r.amount_cents), "PaymentMethodRef": {"value": "1"},
            "DepositToAccountRef": {"value": "35", "name": "Checking"},
            "PrivateNote": f"Synced from Stripe. stripe_refund={re_id[r.stripe_refund_key]}; seed_key={r.seed_key}",
            "Line": [{"Amount": to_qbo(r.amount_cents), "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemRef": {"value": "1", "name": "Advertising Services"}}}],
            "MetaData": {"CreateTime": _qbo_time(r.txn_date), "LastUpdatedTime": _qbo_time(r.txn_date)},
        })

    # ------------------------------------------------------------ ad platform
    ad = {
        "clients": [{"id": c.ad_client_id, "name": c.name} for c in plan.clients],
        "locations": [{"id": l.ad_location_id, "client_id": c.ad_client_id, "name": l.name,
                       "status": "onboarding" if l.opened_on > RECON_AS_OF else "active"}
                      for c in plan.clients for l in c.locations],
        "spend": [{"id": s.source_spend_id,
                   "location_id": next(l.ad_location_id for c in plan.clients if c.key == s.client_key
                                       for l in c.locations if l.key == s.location_key),
                   "date": s.spend_date, "campaign": s.campaign, "amount_cents": s.amount_cents,
                   "currency": "USD"} for s in plan.spend],
    }
    failures = {k: asdict(v) for k, v in plan.ad_failures.items()}

    return {
        "registry.json": registry,
        "qbo/Customer.json": customers, "qbo/PaymentMethod.json": methods, "qbo/Invoice.json": invoices,
        "qbo/Payment.json": qbo_payments, "qbo/RefundReceipt.json": qbo_refunds,
        "stripe/customers.json": stripe_customers, "stripe/payment_intents.json": payment_intents,
        "stripe/charges.json": charges, "stripe/refunds.json": refunds,
        "stripe/balance_transactions.json": bts, "stripe/payouts.json": payouts,
        "ad_platform/dataset.json": ad, "ad_platform/failures.json": failures,
    }


def main() -> None:
    plan = build_plan()
    for rel, data in render(plan).items():
        _dump(OUT / rel, data)
    _dump(GROUND_TRUTH, ground_truth(plan))
    print(f"wrote fixtures to {OUT} and ground truth to {GROUND_TRUTH}")


if __name__ == "__main__":
    main()
