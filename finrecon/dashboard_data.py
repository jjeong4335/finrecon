"""Read-only queries shared by the Django web app and the static report.

Everything here reads the database; nothing calls an external API. Keeping the
queries out of the UI files means they are covered by the test suite.
"""
from __future__ import annotations

from .db import Database
from .recon.evaluator import latest_summary

NAMES = """
    LEFT JOIN clients c ON c.client_id = r.client_id
    LEFT JOIN locations l ON l.location_id = r.location_id
"""

STATUS_HELP = {
    "MATCHED": "Both records exist and agree (invoices: fully paid).",
    "PARTIAL": "Allocations cover part of the invoice; the rest is open.",
    "OPEN_AR": "Unpaid invoice. Normal receivable, not an error.",
    "PENDING_SYNC": "In Stripe, not yet in QBO, still inside the sync window.",
    "MISSING_IN_QBO": "Stripe collected cash that QBO never recorded.",
    "MISSING_IN_STRIPE": "QBO records a card payment Stripe does not have.",
    "NON_STRIPE_PAYMENT": "Paid by check/ACH. No Stripe record expected.",
    "AMOUNT_MISMATCH": "Matched records whose amounts differ.",
    "DUPLICATE": "The same Stripe payment recorded more than once in QBO.",
    "UNRECORDED_REFUND": "Stripe refund not reflected in QBO.",
    "NEEDS_REVIEW": "Ambiguous candidates; a person must decide.",
    "VALIDATED": "Complete extract that passed every check.",
    "ZERO_TRANSACTIONS": "Complete extract with genuinely no activity.",
    "NO_DATA": "Expected extract never arrived. Not zero.",
    "INCOMPLETE": "Partial extract (missing pages / totals disagree). Not loaded.",
    "STALE": "Latest pull failed or is too old; last validated data shown.",
}


def latest_run(db: Database) -> dict | None:
    rows = db.query("SELECT * FROM pipeline_runs WHERE status = 'completed' ORDER BY as_of DESC, run_no DESC LIMIT 1")
    return rows[0] if rows else None


def overview(db: Database, run_id: str) -> dict:
    q = db.scalar
    stripe_gross = q("SELECT COALESCE(SUM(amount_received_cents), 0) FROM stripe_payments WHERE status = 'succeeded'")
    pay_rows = db.query("SELECT status, COUNT(*) AS n FROM reconciliation_results WHERE run_id = ? "
                        "AND record_type IN ('stripe_payment', 'qbo_payment') GROUP BY status", (run_id,))
    pay_total = sum(r["n"] for r in pay_rows)
    pay_ok = sum(r["n"] for r in pay_rows if r["status"] in ("MATCHED", "NON_STRIPE_PAYMENT"))
    return {
        "clients": q("SELECT COUNT(*) FROM clients"),
        "locations": q("SELECT COUNT(*) FROM locations"),
        "invoiced_cents": q("SELECT COALESCE(SUM(total_cents), 0) FROM invoices"),
        "invoice_count": q("SELECT COUNT(*) FROM invoices"),
        "recorded_payments_cents": q("SELECT COALESCE(SUM(total_cents - unapplied_cents), 0) FROM qbo_payments"),
        "stripe_gross_cents": stripe_gross,
        "open_ar_cents": q("SELECT COALESCE(SUM(amount_cents - COALESCE(counterpart_amount_cents, 0)), 0) "
                           "FROM reconciliation_results WHERE run_id = ? AND record_type = 'invoice' "
                           "AND status IN ('OPEN_AR', 'PARTIAL')", (run_id,)),
        "payment_records": pay_total,
        "match_rate": (pay_ok / pay_total) if pay_total else None,
        "exceptions": q("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND is_exception", (run_id,)),
        "exception_cents": q("SELECT COALESCE(SUM(ABS(COALESCE(diff_cents, amount_cents))), 0) "
                             "FROM reconciliation_results WHERE run_id = ? AND is_exception", (run_id,)),
        "billing_issues": q("SELECT COUNT(*) FROM billing_checks WHERE run_id = ? AND status IN "
                            "('BILLING_MISMATCH', 'NOT_INVOICED', 'INVOICE_WITHOUT_SPEND')", (run_id,)),
        "out_of_scope_records": q(
            "SELECT (SELECT COUNT(*) FROM invoices WHERE client_id IS NULL) + "
            "(SELECT COUNT(*) FROM qbo_payments WHERE client_id IS NULL) + "
            "(SELECT COUNT(*) FROM stripe_payments WHERE client_id IS NULL)"),
        "data_issues": q("SELECT COUNT(*) FROM data_status WHERE status NOT IN ('VALIDATED', 'ZERO_TRANSACTIONS')"),
        "evaluation": latest_summary(db, run_id),
    }


def status_counts(db: Database, run_id: str) -> list[dict]:
    return db.query("SELECT record_type, status, COUNT(*) AS n, SUM(amount_cents) AS amount_cents "
                    "FROM reconciliation_results WHERE run_id = ? GROUP BY record_type, status "
                    "ORDER BY record_type, status", (run_id,))


def source_freshness(db: Database) -> list[dict]:
    return db.query("SELECT d.source, COUNT(*) AS scopes, "
                    "SUM(CASE WHEN d.status IN ('VALIDATED', 'ZERO_TRANSACTIONS') THEN 1 ELSE 0 END) AS healthy, "
                    "MAX(d.last_successful_sync) AS last_successful_sync, MIN(d.validated_through) AS oldest_validated "
                    "FROM data_status d GROUP BY d.source ORDER BY d.source")


def results(db: Database, run_id: str, exceptions_only: bool = False) -> list[dict]:
    where = " AND r.is_exception" if exceptions_only else ""
    return db.query(
        "SELECT r.record_type, r.status, r.is_exception, c.name AS client, l.name AS location, r.source, "
        "r.source_record_id, r.match_method, r.counterpart_source_ids, r.amount_cents, "
        "r.counterpart_amount_cents, r.diff_cents, r.currency, r.record_date, r.reason, r.scenario_id, "
        "r.record_id " f"FROM reconciliation_results r {NAMES} WHERE r.run_id = ?{where} "
        "ORDER BY r.is_exception DESC, r.status, COALESCE(c.name, ''), COALESCE(l.name, ''), r.record_date, "
        "r.record_type, r.source_record_id", (run_id,))


_SOURCE_TABLE = {
    "invoice": ("invoices", "invoice_id"), "qbo_payment": ("qbo_payments", "payment_id"),
    "stripe_payment": ("stripe_payments", "payment_id"), "stripe_refund": ("stripe_refunds", "refund_id"),
    "qbo_refund": ("qbo_refunds", "refund_id"),
}


def record_detail(db: Database, run_id: str, record_type: str, record_id: str) -> dict:
    res = db.query("SELECT * FROM reconciliation_results WHERE run_id = ? AND record_type = ? AND record_id = ?",
                   (run_id, record_type, record_id))
    table, pk = _SOURCE_TABLE[record_type]
    src = db.query(f"SELECT * FROM {table} WHERE {pk} = ?", (record_id,))
    source = "qbo" if record_type in ("invoice", "qbo_payment", "qbo_refund") else "stripe"
    ingest_run = src[0]["run_id"] if src else None
    raw = db.query("SELECT request_key, path, sha256, fetched_at FROM raw_data_manifest WHERE run_id = ? "
                   "AND source = ? ORDER BY request_key", (ingest_run, source)) if ingest_run else []
    allocations = []
    if record_type == "qbo_payment":
        allocations = db.query("SELECT i.doc_number, i.source_invoice_id, pa.amount_cents FROM payment_allocations pa "
                               "JOIN invoices i ON i.invoice_id = pa.invoice_id WHERE pa.qbo_payment_id = ?", (record_id,))
    elif record_type == "invoice":
        allocations = db.query("SELECT q.source_payment_id, q.txn_date, pa.amount_cents FROM payment_allocations pa "
                               "JOIN qbo_payments q ON q.payment_id = pa.qbo_payment_id WHERE pa.invoice_id = ?",
                               (record_id,))
    return {"result": res[0] if res else None, "source_row": src[0] if src else None,
            "allocations": allocations, "raw_pages": raw}


def data_status(db: Database) -> list[dict]:
    return db.query(
        "SELECT d.source, d.scope, c.name AS client, l.name AS location, d.status, d.validated_through, "
        "d.last_successful_sync, d.detail FROM data_status d LEFT JOIN clients c ON c.client_id = d.client_id "
        "LEFT JOIN locations l ON l.location_id = d.location_id ORDER BY d.source, d.scope")


def ingestion_history(db: Database) -> list[dict]:
    return db.query("SELECT run_id, source, scope, status, window_start, window_end, records_expected, "
                    "records_received, retry_count, error_details FROM ingestion_runs "
                    "ORDER BY run_id DESC, source, scope")


def quality_findings(db: Database, run_id: str) -> list[dict]:
    return db.query("SELECT source, scope, check_name, passed, severity, detail FROM data_quality_results "
                    "WHERE run_id = ? ORDER BY passed, severity, source, scope, check_name", (run_id,))


def billing(db: Database, run_id: str) -> list[dict]:
    return db.query(
        "SELECT c.name AS client, l.name AS location, b.billing_period, b.status, b.spend_cents, b.fee_cents, "
        "b.discount_cents, b.tax_cents, b.expected_cents, b.invoiced_cents, b.diff_cents, b.invoice_source_id, "
        "b.data_status, b.reason FROM billing_checks b JOIN clients c ON c.client_id = b.client_id "
        "JOIN locations l ON l.location_id = b.location_id WHERE b.run_id = ? "
        "ORDER BY CASE WHEN b.status = 'BILLING_MATCHED' THEN 1 ELSE 0 END, c.name, l.name, b.billing_period",
        (run_id,))


def billing_rules(db: Database) -> list[dict]:
    return db.query("SELECT c.name AS client, r.mgmt_fee_bps, r.min_fee_cents, r.discount_bps, r.tax_bps, "
                    "r.description FROM billing_rules r JOIN clients c ON c.client_id = r.client_id ORDER BY c.name")


def accounting_summary(db: Database, run_id: str) -> list[dict]:
    return db.query("SELECT check_name, SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS n_passed, COUNT(*) AS total "
                    "FROM accounting_checks WHERE run_id = ? GROUP BY check_name ORDER BY check_name", (run_id,))


def payouts(db: Database) -> list[dict]:
    return db.query(
        "SELECT p.source_payout_id, p.arrival_date, p.amount_cents, p.simulated, "
        "COALESCE(SUM(CASE WHEN b.type = 'charge' THEN b.amount_cents END), 0) AS gross_cents, "
        "COALESCE(SUM(CASE WHEN b.type = 'charge' THEN b.fee_cents END), 0) AS fee_cents, "
        "COALESCE(SUM(CASE WHEN b.type = 'refund' THEN b.net_cents END), 0) AS refund_cents, "
        "COALESCE(SUM(CASE WHEN b.type <> 'payout' THEN b.net_cents END), 0) AS net_cents, "
        "COUNT(CASE WHEN b.type <> 'payout' THEN 1 END) AS transactions "
        "FROM stripe_payouts p LEFT JOIN stripe_balance_transactions b ON b.payout_source_id = p.source_payout_id "
        "GROUP BY p.source_payout_id, p.arrival_date, p.amount_cents, p.simulated ORDER BY p.arrival_date")


def evaluation_rows(db: Database, run_id: str) -> list[dict]:
    return db.query("SELECT dimension, scenario, item_key, expected, actual, outcome FROM evaluation_results "
                    "WHERE run_id = ? ORDER BY CASE outcome WHEN 'correct' THEN 1 ELSE 0 END, dimension, scenario, "
                    "item_key", (run_id,))


def scenario_scoreboard(db: Database, run_id: str, catalog: dict) -> list[dict]:
    rows = db.query("SELECT scenario, COUNT(*) AS items, SUM(CASE WHEN outcome = 'correct' THEN 1 ELSE 0 END) "
                    "AS correct FROM evaluation_results WHERE run_id = ? GROUP BY scenario ORDER BY scenario", (run_id,))
    for r in rows:
        meta = catalog.get(r["scenario"], {})
        r["title"] = meta.get("title", "")
        r["description"] = meta.get("description", "")
        r["interview_question"] = meta.get("interview_question", "")
        r["passed"] = r["correct"] == r["items"]
    return rows


def cross_system(db: Database) -> list[dict]:
    from .recon.engine import sql

    return db.query(sql("cross_system_view"))


# ----------------------------------------------------------------------- UCI retail module
def retail_summary(db: Database) -> dict | None:
    """Everything the Retail Data Quality page shows; None if nothing is loaded."""
    try:
        runs = db.query("SELECT * FROM retail_load_runs WHERE status <> 'running' ORDER BY started_at DESC LIMIT 1")
    except Exception:
        return None
    if not runs:
        return None
    canon = "NOT overlap_excluded"
    return {
        "load": runs[0],
        "classes": db.query(f"SELECT line_class, COUNT(*) AS lines, COALESCE(SUM(line_amount_cents), 0) AS amount_cents "
                            f"FROM retail_lines WHERE {canon} GROUP BY line_class ORDER BY lines DESC"),
        "checks": db.query("SELECT check_name, rows_affected, amount_cents, share, passed, severity, detail "
                           "FROM retail_quality_results WHERE load_id = ? "
                           "ORDER BY CASE WHEN check_name LIKE 'control%' THEN 0 WHEN passed THEN 2 ELSE 1 END, "
                           "check_name", (runs[0]["load_id"],)),
        "monthly": db.query(
            f"SELECT SUBSTR(CAST(invoice_ts AS TEXT), 1, 7) AS month, "
            f"SUM(CASE WHEN line_class = 'VALID_SALE' THEN line_amount_cents ELSE 0 END) AS sales_cents, "
            f"SUM(CASE WHEN line_class = 'CANCELLATION' THEN line_amount_cents ELSE 0 END) AS cancellations_cents, "
            f"COUNT(*) AS lines, SUM(CASE WHEN missing_customer THEN 1 ELSE 0 END) AS missing_customer_lines "
            f"FROM retail_lines WHERE {canon} AND invoice_ts IS NOT NULL "
            f"GROUP BY SUBSTR(CAST(invoice_ts AS TEXT), 1, 7) ORDER BY month"),
        "cancellation_links": db.query(
            "SELECT SUBSTR(CAST(l.invoice_ts AS TEXT), 1, 7) AS month, c.status, COUNT(*) AS lines "
            "FROM retail_cancellation_links c JOIN retail_lines l ON l.source_row_id = c.source_row_id "
            "GROUP BY SUBSTR(CAST(l.invoice_ts AS TEXT), 1, 7), c.status ORDER BY month, c.status"),
        "countries": db.query(f"SELECT country, COUNT(DISTINCT invoice) AS invoices, "
                              f"COALESCE(SUM(line_amount_cents), 0) AS amount_cents FROM retail_lines "
                              f"WHERE {canon} AND line_class = 'VALID_SALE' GROUP BY country "
                              f"ORDER BY amount_cents DESC, country LIMIT 10"),
        "non_product_codes": db.query(f"SELECT stock_code, MIN(description) AS description, COUNT(*) AS lines, "
                                      f"COALESCE(SUM(line_amount_cents), 0) AS amount_cents FROM retail_lines "
                                      f"WHERE {canon} AND line_class = 'NON_PRODUCT' GROUP BY stock_code "
                                      f"ORDER BY lines DESC, stock_code LIMIT 15"),
        "examples": db.query(
            f"SELECT source_row_id, invoice, stock_code, description, quantity, unit_price, customer_id, line_class, "
            f"invalid_reason, missing_customer, zero_price, possible_duplicate, overlap_excluded, overlap_conflict "
            f"FROM retail_lines WHERE line_class IN ('INVALID', 'STOCK_ADJUSTMENT', 'ADJUSTMENT') OR overlap_conflict "
            f"OR possible_duplicate ORDER BY line_class, source_row_id LIMIT 200"),
    }


# ----------------------------------------------------------------------- controller queue
# Priority: what is wrong in the books first, then money at stake.
PRIORITY = {
    "MISSING_IN_QBO": (1, "Cash received but not recorded: AR overstated"),
    "UNRECORDED_REFUND": (1, "Refund not in the books: revenue and cash overstated"),
    "MISSING_IN_STRIPE": (1, "Recorded as received, no processor record: cash may not exist"),
    "AMOUNT_MISMATCH": (2, "Books and processor disagree on the amount"),
    "DUPLICATE": (2, "Extra record of one payment: unapplied credit to clean up"),
    "NEEDS_REVIEW": (3, "Ambiguous or contradictory: a person must decide"),
}


def exception_queue(db: Database, run_id: str) -> dict:
    """Exceptions ordered for a controller, grouped into incidents.

    Counts in the rest of the app are per record. One incident often has two
    records (e.g. an amount mismatch is flagged on the Stripe and the QBO side),
    so records that reference each other are grouped here.
    """
    rows = results(db, run_id, exceptions_only=True)
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = {r["source_record_id"] for r in rows}
    for r in rows:
        find(r["source_record_id"])
        for c in (r["counterpart_source_ids"] or "").split(","):
            if c and c in ids:
                parent[find(c)] = find(r["source_record_id"])
    incidents: dict[str, list[dict]] = {}
    for r in rows:
        incidents.setdefault(find(r["source_record_id"]), []).append(r)
    queue = []
    for members in incidents.values():
        prio = min(PRIORITY.get(m["status"], (9, ""))[0] for m in members)
        lead = min(members, key=lambda m: (PRIORITY.get(m["status"], (9, ""))[0], m["record_type"]))
        # A duplicate's whole amount sits unapplied; elsewhere the difference is what is at stake.
        exposure = max(abs(m["amount_cents"] or 0) if m["status"] == "DUPLICATE" or not m["diff_cents"]
                       else abs(m["diff_cents"]) for m in members)
        queue.append({"priority": prio, "why_it_matters": PRIORITY.get(lead["status"], (9, ""))[1],
                      "status": lead["status"], "client": lead["client"], "location": lead["location"],
                      "exposure_cents": exposure, "records": members, "lead": lead})
    queue.sort(key=lambda i: (i["priority"], -i["exposure_cents"], i["lead"]["source_record_id"]))
    return {"incidents": queue, "record_count": len(rows), "incident_count": len(queue),
            "exposure_cents": sum(i["exposure_cents"] for i in queue)}
