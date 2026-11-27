"""Ad spend -> invoice reconciliation (kept separate from payment matching).

Expected invoice per location-month, using the client's billing_rules row:
    fee      = max(spend x fee%, minimum fee)      (0 when there is no spend)
    discount = fee x discount%
    tax      = (fee - discount) x tax%
    expected = spend + fee - discount + tax
The rule set is synthetic and documented on the dashboard. Ad spend and cash
are never compared directly: spend -> invoice here, invoice -> payment in
engine.py.
"""
from __future__ import annotations

from datetime import date

from ..config import Settings
from ..db import Database
from ..money import fmt, pct_of


def expected_invoice(spend: int, rule: dict) -> dict[str, int]:
    if spend == 0:
        return {"fee": 0, "discount": 0, "tax": 0, "expected": 0}
    fee = max(pct_of(spend, rule["mgmt_fee_bps"]), rule["min_fee_cents"])
    discount = pct_of(fee, rule["discount_bps"])
    tax = pct_of(fee - discount, rule["tax_bps"])
    return {"fee": fee, "discount": discount, "tax": tax, "expected": spend + fee - discount + tax}


def _month_end(period: str) -> date:
    y, m = int(period[:4]), int(period[5:])
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def reconcile_billing(db: Database, settings: Settings, run_id: str) -> dict[str, int]:
    db.execute("DELETE FROM billing_checks WHERE run_id = ?", (run_id,))
    periods = sorted({r["billing_period"] for r in db.query(
        "SELECT DISTINCT billing_period FROM invoices WHERE billing_period IS NOT NULL")})
    periods = [p for p in periods if _month_end(p) < settings.recon_as_of]
    rules = {r["client_id"]: r for r in db.query("SELECT * FROM billing_rules")}
    status_of = {r["location_id"]: r for r in db.query("SELECT * FROM data_status WHERE source = 'ad_platform'")}
    spend = {(r["location_id"], r["period"]): r["cents"] for r in db.query(
        "SELECT location_id, SUBSTR(CAST(spend_date AS TEXT), 1, 7) AS period, SUM(amount_cents) AS cents "
        "FROM ad_spend GROUP BY location_id, SUBSTR(CAST(spend_date AS TEXT), 1, 7)")}
    invoiced: dict[tuple[str, str], list[dict]] = {}
    for i in db.query("SELECT * FROM invoices WHERE location_id IS NOT NULL"):
        invoiced.setdefault((i["location_id"], i["billing_period"]), []).append(i)

    rows, counts = [], {}
    for loc in db.query("SELECT l.*, c.client_key FROM locations l JOIN clients c ON c.client_id = l.client_id "
                        "ORDER BY c.client_key, l.location_key"):
        ds = status_of.get(loc["location_id"])
        through = date.fromisoformat(ds["validated_through"]) if ds and ds["validated_through"] else None
        for period in periods:
            key = f"{loc['client_key']}/{loc['location_key']}/{period}"
            invs = invoiced.get((loc["location_id"], period), [])
            inv_total = sum(i["total_cents"] for i in invs) if invs else None
            row = {"run_id": run_id, "client_id": loc["client_id"], "location_id": loc["location_id"],
                   "billing_period": period, "check_key": key, "spend_cents": None, "fee_cents": None,
                   "discount_cents": None, "tax_cents": None, "expected_cents": None, "invoiced_cents": inv_total,
                   "diff_cents": None, "invoice_source_id": ",".join(i["doc_number"] or i["source_invoice_id"] for i in invs) or None,
                   "data_status": ds["status"] if ds else "NO_DATA"}
            if through is None or through < _month_end(period):
                row["status"] = "BILLING_DATA_UNAVAILABLE"
                row["reason"] = (f"Ad spend for {period} has not been received and validated "
                                 f"(data status {row['data_status']}). Billing cannot be checked; this is not $0.")
            else:
                s = spend.get((loc["location_id"], period), 0)
                e = expected_invoice(s, rules[loc["client_id"]])
                row.update(spend_cents=s, fee_cents=e["fee"], discount_cents=e["discount"], tax_cents=e["tax"],
                           expected_cents=e["expected"])
                stale = "" if row["data_status"] == "VALIDATED" else f" (spend from data marked {row['data_status']})"
                if not invs and s == 0:
                    row["status"], row["reason"] = "NO_INVOICE_EXPECTED", f"No spend in {period}; no invoice expected."
                elif not invs:
                    row["status"] = "NOT_INVOICED"
                    row["reason"] = f"{fmt(s)} of ad spend in {period} but no invoice: expected {fmt(e['expected'])}{stale}."
                elif s == 0:
                    row["status"] = "INVOICE_WITHOUT_SPEND"
                    row["reason"] = f"Invoiced {fmt(inv_total)} for {period} with no recorded spend."
                else:
                    row["diff_cents"] = inv_total - e["expected"]
                    if row["diff_cents"] == 0:
                        row["status"] = "BILLING_MATCHED"
                        row["reason"] = f"Invoice equals spend {fmt(s)} + fee {fmt(e['fee'])}{stale}."
                    else:
                        row["status"] = "BILLING_MISMATCH"
                        row["reason"] = (f"Invoiced {fmt(inv_total)}, expected {fmt(e['expected'])} "
                                         f"(spend {fmt(s)} + fee {fmt(e['fee'])} - discount {fmt(e['discount'])} "
                                         f"+ tax {fmt(e['tax'])}); off by {fmt(row['diff_cents'])}{stale}.")
            rows.append(row)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    db.upsert_many("billing_checks", rows, ["run_id", "location_id", "billing_period"])
    db.commit()
    return counts
