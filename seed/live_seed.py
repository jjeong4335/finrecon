"""Create the scenario plan in your QuickBooks Online and Stripe sandboxes.

    MODE=live python -m seed.live_seed seed       # idempotent: safe to re-run
    MODE=live python -m seed.live_seed capture    # record a snapshot for offline/public use

Prerequisites (see README "Live sandbox mode"):
  * Intuit Developer app + sandbox company: QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REALM_ID,
    and a refresh token in .secrets/qbo_tokens.json  {"refresh_token": "..."}
  * Stripe sandbox secret key: STRIPE_SECRET_KEY=sk_test_...

Idempotency: every object carries its seed_key (Stripe metadata, QBO PrivateNote).
Before creating anything the script lists what already exists and skips those
seed_keys; Stripe writes also send an Idempotency-Key. QBO sandboxes cannot be
reset, so this matters.

Dates: QBO TxnDate is set to the scenario date. Stripe objects cannot be
back-dated, so the business date goes into metadata.event_date, which the
pipeline uses as the payment's effective date.

STATUS: written against the public API docs; it was not executed in the build
environment (no network, no credentials). Run it once, then `capture`, and
review data/registry.json before deploying.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime, timezone

from finrecon.adapters import qbo as qbo_mod
from finrecon.adapters import stripe_api
from finrecon.config import ROOT, get_settings
from finrecon.ingestion.normalize import SEED_KEY
from finrecon.money import to_qbo
from seed.scenarios import build_plan

REGISTRY = ROOT / "data" / "registry.json"


def _num(value) -> float:
    """QBO request bodies take JSON numbers. Amounts have at most 2 decimals, so float is exact enough here."""
    return float(value)


def _q(qbo, statement: str) -> list[dict]:
    """One-page QBO query with a WHERE clause (used for small lookups)."""
    from finrecon.http import request_with_retry

    resp = request_with_retry(qbo.transport, "GET", f"/v3/company/{qbo.realm_id}/query",
                              params={"query": statement, "minorversion": qbo.minor_version},
                              headers={"Accept": "application/json"}, policy=qbo.policy)
    qr = resp.body.get("QueryResponse", {})
    return next((v for v in qr.values() if isinstance(v, list)), [])


def _by_seed(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for r in rows:
        m = SEED_KEY.search(r.get("PrivateNote") or "")
        if m:
            out[m[1]] = r
    return out


def seed() -> None:
    settings = get_settings()
    if settings.mode != "live":
        sys.exit("Set MODE=live")
    plan = build_plan()
    qbo = qbo_mod.live_client(settings)
    stripe = stripe_api.live_client(settings)

    # ---------------------------------------------------------------- QBO reference data
    methods = {m["Name"]: m for m in qbo.query_all("PaymentMethod")[0]}
    for name, kind in (("Credit Card (Stripe)", "CREDIT_CARD"), ("Check", "NON_CREDIT_CARD")):
        if name not in methods:
            methods[name] = qbo.create("PaymentMethod", {"Name": name, "Type": kind})
    items = _q(qbo, "select * from Item where Name = 'Advertising Services'")
    if items:
        item = items[0]
    else:
        income = _q(qbo, "select * from Account where AccountType = 'Income' MAXRESULTS 1")[0]
        item = qbo.create("Item", {"Name": "Advertising Services", "Type": "Service",
                                   "IncomeAccountRef": {"value": income["Id"]}})
    bank = _q(qbo, "select * from Account where AccountType = 'Bank' MAXRESULTS 1")[0]

    customers = {c["DisplayName"]: c for c in qbo.query_all("Customer")[0]}
    parent_id, sub_id = {}, {}
    for c in plan.clients:
        if c.name not in customers:
            customers[c.name] = qbo.create("Customer", {"DisplayName": c.name, "CompanyName": c.name})
        parent_id[c.key] = customers[c.name]["Id"]
        for loc in c.locations:
            dn = f"{c.name} - {loc.name}"
            if dn not in customers:
                customers[dn] = qbo.create("Customer", {"DisplayName": dn, "Job": True, "BillWithParent": False,
                                                        "ParentRef": {"value": parent_id[c.key]}})
            sub_id[(c.key, loc.key)] = customers[dn]["Id"]

    existing_inv = _by_seed(qbo.query_all("Invoice")[0])
    inv_id = {}
    for i in plan.invoices:
        if i.seed_key not in existing_inv:
            existing_inv[i.seed_key] = qbo.create("Invoice", {
                "CustomerRef": {"value": sub_id[(i.client_key, i.location_key)]}, "DocNumber": i.doc_number,
                "TxnDate": i.txn_date.isoformat(), "DueDate": i.due_date.isoformat(),
                "PrivateNote": f"period={i.period}; seed_key={i.seed_key}",
                "Line": [{"Amount": _num(to_qbo(i.amount_cents)), "DetailType": "SalesItemLineDetail",
                          "Description": f"Advertising services {i.period} (ad spend + management fee)",
                          "SalesItemLineDetail": {"ItemRef": {"value": item["Id"]}, "Qty": 1,
                                                  "UnitPrice": _num(to_qbo(i.amount_cents))}}]})
        inv_id[i.seed_key] = existing_inv[i.seed_key]["Id"]

    # ---------------------------------------------------------------- Stripe
    scus = {}
    for cust in stripe.list_all("customers")[0]:
        key = (cust.get("metadata") or {}).get("client_key")
        if key:
            scus[key] = cust["id"]
    for c in plan.clients:
        if c.key not in scus:
            scus[c.key] = stripe.create("customers", {"name": c.name, "metadata": {"client_key": c.key}},
                                        idempotency_key=f"finrecon-cus-{c.key}")["id"]
    existing_pi = {(p.get("metadata") or {}).get("seed_key"): p for p in stripe.list_all("payment_intents")[0]}
    pi_id = {}
    docs = {i.seed_key: i.doc_number for i in plan.invoices}
    for p in plan.stripe_payments:
        if p.seed_key not in existing_pi:
            md = {"seed_key": p.seed_key, "scenario_id": p.seed_key.split(":")[0],
                  "location_key": f"{p.client_key}/{p.location_key}", "event_date": p.paid_on.isoformat()}
            if p.invoice_keys:
                md["qbo_invoice_no"] = ",".join(docs[k] for k in p.invoice_keys)
            existing_pi[p.seed_key] = stripe.create("payment_intents", {
                "amount": p.amount_cents, "currency": "usd", "customer": scus[p.client_key],
                "payment_method": "pm_card_visa", "confirm": True,
                # Current Stripe API: payment methods come from Dashboard settings;
                # allow_redirects=never keeps this a plain card confirmation.
                "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                "description": p.description, "metadata": md}, idempotency_key=f"finrecon-pi-{p.seed_key}")
        pi_id[p.seed_key] = existing_pi[p.seed_key]["id"]
    existing_re = {(r.get("metadata") or {}).get("seed_key"): r for r in stripe.list_all("refunds")[0]}
    re_id = {}
    for r in plan.stripe_refunds:
        if r.seed_key not in existing_re:
            existing_re[r.seed_key] = stripe.create("refunds", {
                "payment_intent": pi_id[r.payment_key], "amount": r.amount_cents,
                "metadata": {"seed_key": r.seed_key, "scenario_id": r.seed_key.split(":")[0],
                             "event_date": r.refunded_on.isoformat()}}, idempotency_key=f"finrecon-re-{r.seed_key}")
        re_id[r.seed_key] = existing_re[r.seed_key]["id"]

    # ---------------------------------------------------------------- QBO payments & refunds
    existing_pay = _by_seed(qbo.query_all("Payment")[0])
    for qp in plan.qbo_payments:
        if qp.seed_key in existing_pay:
            continue
        if qp.stripe_payment_key:
            note = f"Synced from Stripe. stripe_payment_intent={pi_id[qp.stripe_payment_key]}; seed_key={qp.seed_key}"
        elif qp.ghost_stripe_id:
            note = f"Synced from Stripe. stripe_payment_intent={qp.ghost_stripe_id}; seed_key={qp.seed_key}"
        elif qp.method == "check":
            note = f"Check deposited at branch. seed_key={qp.seed_key}"
        else:
            note = f"Card payment entered manually. seed_key={qp.seed_key}"
        method = methods["Check" if qp.method == "check" else "Credit Card (Stripe)"]
        body = {"CustomerRef": {"value": sub_id[(qp.client_key, qp.location_key)] if qp.location_key
                                else parent_id[qp.client_key]},
                "TotalAmt": _num(to_qbo(qp.amount_cents)), "TxnDate": qp.txn_date.isoformat(),
                "PaymentMethodRef": {"value": method["Id"]}, "PrivateNote": note,
                "Line": [{"Amount": _num(to_qbo(a)), "LinkedTxn": [{"TxnId": inv_id[k], "TxnType": "Invoice"}]}
                         for k, a in qp.allocations]}
        if qp.method == "check":
            body["PaymentRefNum"] = "10482"
        qbo.create("Payment", body)
    existing_rr = _by_seed(qbo.query_all("RefundReceipt")[0])
    for r in plan.qbo_refunds:
        if r.seed_key in existing_rr:
            continue
        qbo.create("RefundReceipt", {
            "CustomerRef": {"value": sub_id[(r.client_key, r.location_key)]}, "TxnDate": r.txn_date.isoformat(),
            "DepositToAccountRef": {"value": bank["Id"]},
            "PaymentMethodRef": {"value": methods["Credit Card (Stripe)"]["Id"]},
            "PrivateNote": f"Synced from Stripe. stripe_refund={re_id[r.stripe_refund_key]}; seed_key={r.seed_key}",
            "Line": [{"Amount": _num(to_qbo(r.amount_cents)), "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemRef": {"value": item["Id"]}}}]})

    # ---------------------------------------------------------------- registry
    registry = json.loads((settings.fixture_dir / "registry.json").read_text())
    for c in registry["clients"]:
        c["external"]["qbo_customer_id"] = parent_id[c["client_key"]]
        c["external"]["stripe_customer_id"] = scus[c["client_key"]]
        for loc in c["locations"]:
            loc["external"]["qbo_customer_id"] = sub_id[(c["client_key"], loc["location_key"])]
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(registry, indent=1))
    print(f"Seeded. Registry written to {REGISTRY}.")


def capture(out_dir: str = "fixtures/live_snapshot") -> None:
    """Record live sandbox objects in the offline fixture layout.

    Afterwards: MODE=offline FIXTURE_DIR=fixtures/live_snapshot python -m finrecon.pipeline demo
    serves real sandbox data with no tokens at view time.
    """
    settings = get_settings()
    qbo = qbo_mod.live_client(settings)
    stripe = stripe_api.live_client(settings)
    out = ROOT / out_dir

    def dump(rel, data):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1, default=str))

    for entity in ("Customer", "PaymentMethod", "Invoice", "Payment", "RefundReceipt"):
        dump(f"qbo/{entity}.json", qbo.query_all(entity)[0])
    def unix(d: str) -> int:
        return int(datetime.combine(date.fromisoformat(d), datetime.min.time(), timezone.utc).timestamp()) + 54000

    lists = {res: stripe.list_all(res)[0] for res in
             ("customers", "payment_intents", "charges", "refunds", "payouts", "balance_transactions")}
    # Sandbox objects carry today's `created`; record their business date so
    # the offline transport shows them at the right point in time.
    when: dict[str, int] = {}
    for pi in lists["payment_intents"]:
        ed = (pi.get("metadata") or {}).get("event_date")
        if ed:
            pi["_fixture_created"] = when[pi["id"]] = unix(ed)
            if pi.get("latest_charge"):
                when[pi["latest_charge"]] = when[pi["id"]]
    for ch in lists["charges"]:
        if ch["id"] in when:
            ch["_fixture_created"] = when[ch["id"]]
    for r in lists["refunds"]:
        ed = (r.get("metadata") or {}).get("event_date")
        if ed:
            r["_fixture_created"] = when[r["id"]] = unix(ed)
    for b in lists["balance_transactions"]:
        b["_fixture_created"] = when.get(b.get("source"), 0)
    for po in lists["payouts"]:
        po["_fixture_created"] = 0
        members = {b["id"] for b in stripe.list_all("balance_transactions", {"payout": po["id"]})[0]}
        for b in lists["balance_transactions"]:
            if b["id"] in members:
                b["_fixture_payout"] = po["id"]
    for res, data in lists.items():
        dump(f"stripe/{res}.json", data)
    shutil.copytree(settings.fixture_dir / "ad_platform", out / "ad_platform", dirs_exist_ok=True)
    shutil.copy(REGISTRY, out / "registry.json")
    (out / "CAPTURED_AT.txt").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    print(f"Snapshot written to {out}")


if __name__ == "__main__":
    {"seed": seed, "capture": capture}[sys.argv[1] if len(sys.argv) > 1 else "seed"]()
