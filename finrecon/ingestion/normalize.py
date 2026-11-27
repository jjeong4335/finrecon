"""External objects -> internal rows.

Each function returns (row, issues). Issues are data-quality findings that the
runner records; a record with a blocking issue is still stored when possible,
so it can be traced and shown as needing attention rather than silently lost.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .. import money
from ..ids import client_id, location_id, record_id

SEED_KEY = re.compile(r"seed_key=([^;\s]+)")
PERIOD = re.compile(r"period=(\d{4}-\d{2})")
DESC_PERIOD = re.compile(r"\b(\d{4}-\d{2})\b")
STRIPE_PI = re.compile(r"stripe_payment_intent=(pi_\w+)")
STRIPE_RE = re.compile(r"stripe_refund=(re_\w+)")


@dataclass
class Issue:
    check: str
    detail: str
    severity: str = "error"


@dataclass
class IdMap:
    """Lookup of (source, entity_type, external_id) -> (internal_type, internal_id)."""
    entries: dict[tuple[str, str, str], tuple[str, str]] = field(default_factory=dict)
    location_client: dict[str, str] = field(default_factory=dict)

    def resolve(self, source: str, entity_type: str, external_id: str | None) -> tuple[str | None, str | None]:
        """Return (client_id, location_id) for an external reference."""
        if not external_id:
            return None, None
        hit = self.entries.get((source, entity_type, str(external_id)))
        if not hit:
            return None, None
        kind, internal = hit
        if kind == "location":
            return self.location_client[internal], internal
        return internal, None


def registry_rows(registry: dict, now: str):
    clients, locations, mappings, rules = [], [], [], []
    for c in registry["clients"]:
        cid = client_id(c["client_key"])
        clients.append({"client_id": cid, "client_key": c["client_key"], "name": c["name"], "created_at": now})
        r = c["billing_rule"]
        rules.append({"client_id": cid, "mgmt_fee_bps": r["mgmt_fee_bps"], "min_fee_cents": r["min_fee_cents"],
                      "discount_bps": r["discount_bps"], "tax_bps": r["tax_bps"],
                      "currency": money.normalize_currency(r["currency"]), "description": r["description"]})
        ext = c["external"]
        mappings += [
            {"source": "qbo", "entity_type": "customer", "external_id": ext["qbo_customer_id"],
             "internal_type": "client", "internal_id": cid},
            {"source": "stripe", "entity_type": "customer", "external_id": ext["stripe_customer_id"],
             "internal_type": "client", "internal_id": cid},
            {"source": "ad_platform", "entity_type": "client", "external_id": ext["ad_client_id"],
             "internal_type": "client", "internal_id": cid},
        ]
        for loc in c["locations"]:
            lid = location_id(c["client_key"], loc["location_key"])
            locations.append({"location_id": lid, "client_id": cid, "location_key": loc["location_key"],
                              "name": loc["name"], "opened_on": loc.get("opened_on"), "created_at": now})
            le = loc["external"]
            mappings += [
                {"source": "qbo", "entity_type": "customer", "external_id": le["qbo_customer_id"],
                 "internal_type": "location", "internal_id": lid},
                {"source": "ad_platform", "entity_type": "location", "external_id": le["ad_location_id"],
                 "internal_type": "location", "internal_id": lid},
                # Stripe has no location object: the checkout writes metadata.location_key.
                {"source": "stripe", "entity_type": "metadata.location_key",
                 "external_id": f"{c['client_key']}/{loc['location_key']}",
                 "internal_type": "location", "internal_id": lid},
            ]
    return clients, locations, mappings, rules


def _note(obj: dict) -> str:
    return obj.get("PrivateNote") or ""


def _seed(text: str) -> str | None:
    m = SEED_KEY.search(text or "")
    return m[1] if m else None


def _iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _date_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def _require(obj: dict, fields: list[str], label: str) -> list[Issue]:
    return [Issue("required_fields", f"{label} missing {f}") for f in fields if obj.get(f) in (None, "")]


# ------------------------------------------------------------------ QuickBooks
def qbo_invoice(obj: dict, ids: IdMap, run_id: str, now: str):
    issues = _require(obj, ["Id", "TxnDate", "TotalAmt", "CustomerRef"], f"QBO Invoice {obj.get('Id')}")
    cust = (obj.get("CustomerRef") or {}).get("value")
    cid, lid = ids.resolve("qbo", "customer", cust)
    if cid is None:
        issues.append(Issue("unmapped_external_id", f"QBO Invoice {obj.get('Id')}: customer {cust} not in source_id_map"))
    total, cur = money.from_qbo(obj.get("TotalAmt", 0), (obj.get("CurrencyRef") or {}).get("value"))
    balance, _ = money.from_qbo(obj.get("Balance", 0), cur)
    note = _note(obj)
    period = (PERIOD.search(note) or [None, None])[1]
    if period is None:
        for line in obj.get("Line", []):
            m = DESC_PERIOD.search(line.get("Description") or "")
            if m:
                period = m[1]
                break
    if period is None:
        issues.append(Issue("billing_period", f"QBO Invoice {obj.get('Id')}: billing period not found", "warning"))
    row = {
        "invoice_id": record_id("qbo", "invoice", obj["Id"]), "source": "qbo", "source_invoice_id": obj["Id"],
        "doc_number": obj.get("DocNumber"), "client_id": cid, "location_id": lid, "qbo_customer_id": cust,
        "billing_period": period, "txn_date": obj["TxnDate"], "due_date": obj.get("DueDate"),
        "total_cents": total, "balance_cents": balance, "currency": cur, "private_note": note,
        "scenario_id": _seed(note), "run_id": run_id, "ingested_at": now,
    }
    return row, issues


def qbo_payment(obj: dict, ids: IdMap, methods: dict[str, dict], run_id: str, now: str):
    issues = _require(obj, ["Id", "TxnDate", "TotalAmt", "CustomerRef"], f"QBO Payment {obj.get('Id')}")
    cust = (obj.get("CustomerRef") or {}).get("value")
    cid, lid = ids.resolve("qbo", "customer", cust)
    if cid is None:
        issues.append(Issue("unmapped_external_id", f"QBO Payment {obj.get('Id')}: customer {cust} not in source_id_map"))
    cur = (obj.get("CurrencyRef") or {}).get("value") or "USD"
    total, cur = money.from_qbo(obj.get("TotalAmt", 0), cur)
    unapplied, _ = money.from_qbo(obj.get("UnappliedAmt", 0), cur)
    method_id = (obj.get("PaymentMethodRef") or {}).get("value")
    method = methods.get(method_id or "", {})
    note = _note(obj)
    ref = STRIPE_PI.search(note)
    allocations: list[tuple[str, int]] = []
    for line in obj.get("Line", []):
        for linked in line.get("LinkedTxn", []):
            if linked.get("TxnType") == "Invoice":
                amt, _ = money.from_qbo(line.get("Amount", 0), cur)
                allocations.append((linked["TxnId"], amt))
    if sum(a for _, a in allocations) + unapplied != total:
        issues.append(Issue("allocation_totals", f"QBO Payment {obj.get('Id')}: lines + unapplied != total"))
    row = {
        "payment_id": record_id("qbo", "payment", obj["Id"]), "source_payment_id": obj["Id"],
        "client_id": cid, "location_id": lid, "qbo_customer_id": cust, "txn_date": obj["TxnDate"],
        "total_cents": total, "unapplied_cents": unapplied, "currency": cur,
        "payment_method_id": method_id, "payment_method_name": method.get("Name"),
        "payment_method_type": method.get("Type"), "payment_ref_num": obj.get("PaymentRefNum") or None,
        "private_note": note, "referenced_stripe_id": ref[1] if ref else None,
        "scenario_id": _seed(note), "run_id": run_id, "ingested_at": now,
    }
    return row, allocations, issues


def qbo_refund(obj: dict, ids: IdMap, run_id: str, now: str):
    issues = _require(obj, ["Id", "TxnDate", "TotalAmt", "CustomerRef"], f"QBO RefundReceipt {obj.get('Id')}")
    cust = (obj.get("CustomerRef") or {}).get("value")
    cid, lid = ids.resolve("qbo", "customer", cust)
    total, cur = money.from_qbo(obj.get("TotalAmt", 0), (obj.get("CurrencyRef") or {}).get("value"))
    note = _note(obj)
    ref = STRIPE_RE.search(note)
    return {
        "refund_id": record_id("qbo", "refund", obj["Id"]), "source_refund_id": obj["Id"], "client_id": cid,
        "location_id": lid, "qbo_customer_id": cust, "txn_date": obj["TxnDate"], "total_cents": total,
        "currency": cur, "private_note": note, "referenced_stripe_id": ref[1] if ref else None,
        "scenario_id": _seed(note), "run_id": run_id, "ingested_at": now,
    }, issues


# ------------------------------------------------------------------ Stripe
def stripe_payment(pi: dict, charges: dict[str, dict], ids: IdMap, run_id: str, now: str):
    issues = _require(pi, ["id", "amount", "currency", "created", "customer"], f"Stripe {pi.get('id')}")
    amount, cur = money.from_stripe(pi["amount"], pi["currency"])
    received, _ = money.from_stripe(pi.get("amount_received", 0), cur)
    cid, _ = ids.resolve("stripe", "customer", pi.get("customer"))
    if cid is None:
        issues.append(Issue("unmapped_external_id", f"Stripe {pi['id']}: customer {pi.get('customer')} not mapped"))
    md = pi.get("metadata") or {}
    lcid, lid = ids.resolve("stripe", "metadata.location_key", md.get("location_key"))
    if lcid and cid and lcid != cid:
        issues.append(Issue("referential_integrity", f"Stripe {pi['id']}: location belongs to a different client"))
    created_date = _date_from_unix(pi["created"])
    effective = created_date
    # Sandbox objects cannot be back-dated, so live seeding records the
    # business date in metadata. Offline fixtures carry the same field.
    if md.get("event_date"):
        try:
            effective = date.fromisoformat(md["event_date"]).isoformat()
        except ValueError:
            issues.append(Issue("invalid_value", f"Stripe {pi['id']}: bad metadata.event_date", "warning"))
    charge = charges.get(pi.get("latest_charge") or "", {})
    row = {
        "payment_id": record_id("stripe", "payment_intent", pi["id"]), "source_payment_id": pi["id"],
        "charge_id": pi.get("latest_charge"), "balance_transaction_id": charge.get("balance_transaction"),
        "stripe_customer_id": pi.get("customer"), "client_id": cid, "location_id": lid,
        "amount_cents": amount, "amount_received_cents": received, "currency": cur, "status": pi["status"],
        "created_at": _iso_from_unix(pi["created"]), "effective_date": effective,
        "description": pi.get("description"), "qbo_invoice_ref": md.get("qbo_invoice_no"),
        "scenario_id": md.get("seed_key"), "run_id": run_id, "ingested_at": now,
    }
    return row, issues


def stripe_refund(obj: dict, payments_by_pi: dict[str, dict], run_id: str, now: str):
    amount, cur = money.from_stripe(obj["amount"], obj["currency"])
    pay = payments_by_pi.get(obj.get("payment_intent") or "", {})
    issues = [] if pay else [Issue("referential_integrity", f"Stripe refund {obj['id']}: payment not found")]
    effective = _date_from_unix(obj["created"])
    ed = (obj.get("metadata") or {}).get("event_date")
    if ed:   # same rule as payments: sandbox objects cannot be back-dated
        try:
            effective = date.fromisoformat(ed).isoformat()
        except ValueError:
            issues.append(Issue("invalid_value", f"Stripe refund {obj['id']}: bad metadata.event_date", "warning"))
    return {
        "refund_id": record_id("stripe", "refund", obj["id"]), "source_refund_id": obj["id"],
        "source_payment_id": obj.get("payment_intent"), "charge_id": obj.get("charge"),
        "client_id": pay.get("client_id"), "location_id": pay.get("location_id"), "amount_cents": amount,
        "currency": cur, "status": obj.get("status", "succeeded"), "created_at": _iso_from_unix(obj["created"]),
        "effective_date": effective, "balance_transaction_id": obj.get("balance_transaction"),
        "scenario_id": (obj.get("metadata") or {}).get("seed_key"), "run_id": run_id, "ingested_at": now,
    }, issues


def stripe_balance_transaction(obj: dict, payout_of: dict[str, str], run_id: str, now: str):
    amount, cur = money.from_stripe(obj["amount"], obj["currency"])
    return {
        "bt_id": record_id("stripe", "balance_transaction", obj["id"]), "source_bt_id": obj["id"],
        "type": obj["type"], "source_object_id": obj.get("source"), "amount_cents": amount,
        "fee_cents": int(obj.get("fee", 0)), "net_cents": int(obj["net"]), "currency": cur,
        "created_at": _iso_from_unix(obj["created"]),
        "available_on": _date_from_unix(obj["available_on"]) if obj.get("available_on") else None,
        "payout_source_id": payout_of.get(obj["id"]), "run_id": run_id, "ingested_at": now,
    }


def stripe_payout(obj: dict, run_id: str, now: str):
    amount, cur = money.from_stripe(obj["amount"], obj["currency"])
    return {
        "payout_id": record_id("stripe", "payout", obj["id"]), "source_payout_id": obj["id"],
        "amount_cents": amount, "currency": cur, "status": obj["status"],
        "arrival_date": _date_from_unix(obj["arrival_date"]), "created_at": _iso_from_unix(obj["created"]),
        "simulated": (obj.get("metadata") or {}).get("simulated") == "true", "run_id": run_id, "ingested_at": now,
    }


# ------------------------------------------------------------------ ad platform
def ad_spend(obj: dict, client_internal: str, location_internal: str, run_id: str, now: str):
    issues = _require(obj, ["id", "date", "amount_cents", "currency"], f"ad spend {obj.get('id')}")
    cur = money.normalize_currency(obj.get("currency"))
    amount = obj.get("amount_cents")
    if not isinstance(amount, int) or amount < 0:
        issues.append(Issue("invalid_value", f"ad spend {obj.get('id')}: invalid amount {amount!r}"))
        amount = None
    return {
        "spend_id": record_id("ad_platform", "spend", obj["id"]), "source_spend_id": obj["id"],
        "client_id": client_internal, "location_id": location_internal, "spend_date": obj["date"],
        "campaign": obj.get("campaign") or "unknown", "amount_cents": amount, "currency": cur,
        "run_id": run_id, "ingested_at": now,
    }, issues
