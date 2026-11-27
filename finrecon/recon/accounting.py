"""Gross-to-net, payout reconciliation and educational journal-entry previews.

  gross payment - processing fee = net balance transaction
  sum(net of transactions in a payout) = payout amount

Journal entries use a "Stripe Clearing" account: card receipts land there,
fees come out of it, and payouts move the remainder to the bank. They are
illustrative previews, not accounting advice and not posted to QuickBooks.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import Database
from ..money import fmt


def run_checks(db: Database, run_id: str) -> dict[str, int]:
    db.execute("DELETE FROM accounting_checks WHERE run_id = ?", (run_id,))
    rows = []
    for b in db.query("SELECT * FROM stripe_balance_transactions"):
        ok = b["amount_cents"] - b["fee_cents"] == b["net_cents"]
        rows.append({"run_id": run_id, "check_name": "gross_minus_fee_equals_net", "ref": b["source_bt_id"],
                     "passed": ok, "expected_cents": b["amount_cents"] - b["fee_cents"], "actual_cents": b["net_cents"],
                     "detail": f"{b['type']}: {fmt(b['amount_cents'])} - {fmt(b['fee_cents'])} fee = {fmt(b['net_cents'])}"})
    for p in db.query(
            "SELECT s.source_payment_id, s.amount_received_cents, b.amount_cents AS bt_amount "
            "FROM stripe_payments s JOIN stripe_balance_transactions b "
            "ON b.source_bt_id = s.balance_transaction_id"):
        rows.append({"run_id": run_id, "check_name": "charge_gross_equals_payment", "ref": p["source_payment_id"],
                     "passed": p["bt_amount"] == p["amount_received_cents"], "expected_cents": p["amount_received_cents"],
                     "actual_cents": p["bt_amount"], "detail": "balance transaction gross vs PaymentIntent amount"})
    for po in db.query(
            "SELECT p.source_payout_id, p.amount_cents, p.simulated, "
            "COALESCE(SUM(b.net_cents), 0) AS net_sum, COUNT(b.bt_id) AS n "
            "FROM stripe_payouts p LEFT JOIN stripe_balance_transactions b "
            "  ON b.payout_source_id = p.source_payout_id AND b.type <> 'payout' "
            "GROUP BY p.source_payout_id, p.amount_cents, p.simulated"):
        rows.append({"run_id": run_id, "check_name": "payout_equals_sum_of_net", "ref": po["source_payout_id"],
                     "passed": po["net_sum"] == po["amount_cents"], "expected_cents": po["net_sum"],
                     "actual_cents": po["amount_cents"],
                     "detail": f"{po['n']} transactions{' (simulated payout)' if po['simulated'] else ''}"})
    db.upsert_many("accounting_checks", rows, ["run_id", "check_name", "ref"])
    db.commit()
    counts: dict[str, int] = {}
    for r in rows:
        k = f"{r['check_name']}:{'pass' if r['passed'] else 'fail'}"
        counts[k] = counts.get(k, 0) + 1
    return counts


@dataclass
class Line:
    account: str
    debit: int = 0
    credit: int = 0


@dataclass
class Entry:
    title: str
    date: str | None
    lines: list[Line]
    note: str = ""

    @property
    def balanced(self) -> bool:
        return sum(l.debit for l in self.lines) == sum(l.credit for l in self.lines)


def journal_preview(db: Database, stripe_payment_source_id: str, run_id: str) -> list[Entry]:
    """Double-entry preview for one Stripe payment and the invoice(s) it paid."""
    s = db.query("SELECT * FROM stripe_payments WHERE source_payment_id = ?", (stripe_payment_source_id,))
    if not s:
        return []
    s = s[0]
    entries: list[Entry] = []
    docs = [d.strip() for d in (s["qbo_invoice_ref"] or "").split(",") if d.strip()]
    for doc in docs:
        inv = db.query("SELECT i.*, b.spend_cents, b.fee_cents, b.discount_cents, b.tax_cents FROM invoices i "
                       "LEFT JOIN billing_checks b ON b.location_id = i.location_id "
                       " AND b.billing_period = i.billing_period AND b.run_id = ? WHERE i.doc_number = ?", (run_id, doc))
        if not inv:
            continue
        i = inv[0]
        lines = [Line("Accounts Receivable", debit=i["total_cents"])]
        if i["spend_cents"] is not None and i["spend_cents"] + (i["fee_cents"] or 0) - (i["discount_cents"] or 0) \
                + (i["tax_cents"] or 0) == i["total_cents"]:
            lines += [Line("Advertising Revenue (media pass-through)", credit=i["spend_cents"]),
                      Line("Management Fee Revenue", credit=i["fee_cents"] - i["discount_cents"])]
            if i["tax_cents"]:
                lines.append(Line("Sales Tax Payable", credit=i["tax_cents"]))
        else:
            lines.append(Line("Revenue", credit=i["total_cents"]))
        entries.append(Entry(f"Invoice {doc} issued", i["txn_date"], lines))
    bt = db.query("SELECT * FROM stripe_balance_transactions WHERE source_bt_id = ?", (s["balance_transaction_id"],))
    gross = s["amount_received_cents"]
    entries.append(Entry("Customer pays by card (Stripe)", s["effective_date"],
                         [Line("Stripe Clearing", debit=gross), Line("Accounts Receivable", credit=gross)]))
    if bt:
        b = bt[0]
        entries.append(Entry("Stripe processing fee", s["effective_date"],
                             [Line("Merchant Processing Fees", debit=b["fee_cents"]),
                              Line("Stripe Clearing", credit=b["fee_cents"])],
                             note=f"Gross {fmt(b['amount_cents'])} - fee {fmt(b['fee_cents'])} = net {fmt(b['net_cents'])}"))
        for r in db.query("SELECT * FROM stripe_refunds WHERE source_payment_id = ?", (stripe_payment_source_id,)):
            entries.append(Entry("Refund issued through Stripe", r["effective_date"],
                                 [Line("Refunds and Allowances", debit=r["amount_cents"]),
                                  Line("Stripe Clearing", credit=r["amount_cents"])],
                                 note="Stripe deducts the refund from the next payout after it, so Stripe "
                                      "Clearing nets to zero once that later payout is recorded."))
        if b["payout_source_id"]:
            po = db.query("SELECT * FROM stripe_payouts WHERE source_payout_id = ?", (b["payout_source_id"],))[0]
            entries.append(Entry(f"Payout {po['source_payout_id']} to bank (this payment's share)", po["arrival_date"],
                                 [Line("Cash - Operating Bank", debit=b["net_cents"]),
                                  Line("Stripe Clearing", credit=b["net_cents"])],
                                 note=f"Payout total {fmt(po['amount_cents'])}"
                                      f"{' (simulated payout)' if po['simulated'] else ''}"))
    return entries
