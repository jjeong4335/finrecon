"""Tests 2-6 and 11: payment, invoice and refund classification rules."""
from dataclasses import replace
from datetime import date

from finrecon.recon import engine
from tests.conftest import status_of

COUNTED_TABLES = ["invoices", "qbo_payments", "payment_allocations", "stripe_payments", "stripe_refunds",
                  "qbo_refunds", "stripe_balance_transactions", "stripe_payouts", "ad_spend"]


def test_stripe_only_payment_pending_inside_window_missing_after(fresh_demo):
    """Test 2: Stripe-only payments -> PENDING_SYNC inside the window, MISSING_IN_QBO after."""
    db, settings, run_id = fresh_demo
    assert status_of(db, run_id, "stripe_payment", "S14") == ["PENDING_SYNC"]
    assert status_of(db, run_id, "stripe_payment", "S05") == ["MISSING_IN_QBO"]
    later = replace(settings, recon_as_of=date(2026, 12, 3))
    engine.reconcile(db, later, "later")
    assert status_of(db, "later", "stripe_payment", "S14") == ["MISSING_IN_QBO"]


def test_qbo_only_payments_classified_by_method(demo):
    """Test 3: check payment is NON_STRIPE_PAYMENT; card payment naming a ghost ID is MISSING_IN_STRIPE."""
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S06") == ["NON_STRIPE_PAYMENT"]
    assert status_of(db, run_id, "qbo_payment", "S12") == ["MISSING_IN_STRIPE"]
    row = db.query("SELECT is_exception FROM reconciliation_results WHERE run_id = ? AND scenario_id LIKE 'S06%' "
                   "AND record_type = 'qbo_payment'", (run_id,))[0]
    assert row["is_exception"] is False


def test_card_payment_without_reference_or_candidate_is_missing_in_stripe(fresh_demo):
    db, settings, _ = fresh_demo
    # Remove the only Stripe candidate for S10's hand-entered QBO card payment.
    sp = db.query("SELECT payment_id FROM stripe_payments WHERE scenario_id LIKE 'S10%'")[0]["payment_id"]
    db.execute("DELETE FROM payment_matches WHERE stripe_payment_id = ?", (sp,))
    db.execute("DELETE FROM stripe_payments WHERE payment_id = ?", (sp,))
    db.commit()
    engine.reconcile(db, settings, "no-candidate")
    assert status_of(db, "no-candidate", "qbo_payment", "S10") == ["MISSING_IN_STRIPE"]


def test_amount_mismatch_detected_on_both_sides(demo):
    """Test 4."""
    db, _, run_id = demo
    assert status_of(db, run_id, "stripe_payment", "S04") == ["AMOUNT_MISMATCH"]
    assert status_of(db, run_id, "qbo_payment", "S04") == ["AMOUNT_MISMATCH"]
    diff = db.scalar("SELECT diff_cents FROM reconciliation_results WHERE run_id = ? AND record_type = "
                     "'stripe_payment' AND scenario_id LIKE 'S04%'", (run_id,))
    assert diff == 4500


def test_unpaid_invoices_are_open_ar_not_exceptions(demo):
    """Test 5."""
    db, _, run_id = demo
    open_ar = db.query("SELECT is_exception FROM reconciliation_results WHERE run_id = ? AND status = 'OPEN_AR'",
                       (run_id,))
    assert len(open_ar) == 12
    assert not any(r["is_exception"] for r in open_ar)
    assert db.scalar("SELECT COUNT(*) FROM reconciliation_exceptions WHERE run_id = ? AND status = 'OPEN_AR'",
                     (run_id,)) == 0


def test_partial_split_and_multi_invoice_payments(demo):
    """Test 6: allocations, never double counted."""
    db, _, run_id = demo
    assert status_of(db, run_id, "invoice", "S01") == ["PARTIAL"]
    assert status_of(db, run_id, "invoice", "S02") == ["MATCHED"]
    assert status_of(db, run_id, "stripe_payment", "S02") == ["MATCHED", "MATCHED"]
    assert status_of(db, run_id, "invoice", "S03") == ["MATCHED", "MATCHED"]
    # The S03 payment is allocated across two invoices exactly once.
    pay = db.query("SELECT payment_id, total_cents FROM qbo_payments WHERE scenario_id LIKE 'S03%'")[0]
    allocs = db.query("SELECT amount_cents FROM payment_allocations WHERE qbo_payment_id = ?", (pay["payment_id"],))
    assert len(allocs) == 2 and sum(a["amount_cents"] for a in allocs) == pay["total_cents"]
    # Globally: cash applied to invoices == QBO payments minus unapplied amounts.
    applied = db.scalar("SELECT SUM(amount_cents) FROM payment_allocations")
    received = db.scalar("SELECT SUM(total_cents - unapplied_cents) FROM qbo_payments")
    assert applied == received


def test_duplicate_import_detected_and_not_counted(demo):
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S07") == ["DUPLICATE", "MATCHED"]   # sorted: qp-dup < qp
    assert status_of(db, run_id, "invoice", "S07") == ["MATCHED"]
    inv = db.query("SELECT counterpart_amount_cents, amount_cents FROM reconciliation_results WHERE run_id = ? "
                   "AND record_type = 'invoice' AND scenario_id LIKE 'S07%'", (run_id,))[0]
    assert inv["counterpart_amount_cents"] == inv["amount_cents"]   # applied once, not twice


def test_ambiguous_candidates_require_review(demo):
    """Test 11."""
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S11") == ["NEEDS_REVIEW"]
    assert status_of(db, run_id, "stripe_payment", "S11") == ["NEEDS_REVIEW", "NEEDS_REVIEW"]
    assert db.scalar("SELECT COUNT(*) FROM payment_matches m JOIN qbo_payments q ON q.payment_id = m.qbo_payment_id "
                     "WHERE m.run_id = ? AND q.scenario_id LIKE 'S11%'", (run_id,)) == 0


def test_unique_candidate_is_matched_with_method_recorded(demo):
    db, _, run_id = demo
    rows = db.query("SELECT status, match_method FROM reconciliation_results WHERE run_id = ? "
                    "AND scenario_id LIKE 'S10%' AND record_type IN ('qbo_payment', 'stripe_payment')", (run_id,))
    assert {(r["status"], r["match_method"]) for r in rows} == {("MATCHED", "candidate")}


def test_refunds(demo):
    db, _, run_id = demo
    assert status_of(db, run_id, "stripe_refund", "S08") == ["UNRECORDED_REFUND"]
    assert status_of(db, run_id, "stripe_refund", "S09") == ["MATCHED"]
    assert status_of(db, run_id, "qbo_refund", "S09") == ["MATCHED"]


def test_qbo_and_stripe_amounts_are_never_summed_as_revenue(demo):
    db, _, run_id = demo
    from finrecon.dashboard_data import overview

    o = overview(db, run_id)
    # Recorded payments come from QBO only; Stripe gross is shown separately.
    assert o["recorded_payments_cents"] == db.scalar("SELECT SUM(total_cents - unapplied_cents) FROM qbo_payments")
    assert o["recorded_payments_cents"] < o["invoiced_cents"]


def test_every_record_gets_exactly_one_result(demo):
    db, _, run_id = demo
    for record_type, table in (("invoice", "invoices"), ("qbo_payment", "qbo_payments"),
                               ("stripe_payment", "stripe_payments"), ("stripe_refund", "stripe_refunds"),
                               ("qbo_refund", "qbo_refunds")):
        n = db.scalar(f"SELECT COUNT(*) FROM {table}")
        got = db.scalar("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND record_type = ?",
                        (run_id, record_type))
        assert got == n, record_type


def test_wrong_first_record_does_not_become_the_match(demo):
    """S17: the record whose amount agrees with Stripe is the match, even if it came second."""
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S17") == ["DUPLICATE", "MATCHED"]   # sorted: qp-wrong, qp
    assert status_of(db, run_id, "stripe_payment", "S17") == ["MATCHED"]
    dup = db.query("SELECT reason FROM reconciliation_results WHERE run_id = ? AND scenario_id LIKE 'S17:qp-wrong%'",
                   (run_id,))[0]["reason"]
    assert "also differs from Stripe" in dup


def test_unique_candidate_with_conflicting_location_is_not_matched(demo):
    """S18: one candidate, same amount, but the systems disagree on the location."""
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S18") == ["NEEDS_REVIEW"]
    assert status_of(db, run_id, "stripe_payment", "S18") == ["NEEDS_REVIEW"]
    reason = db.scalar("SELECT reason FROM reconciliation_results WHERE run_id = ? AND scenario_id LIKE 'S18:qp%'",
                       (run_id,))
    assert "Williamsburg" in reason and "Jersey City Heights" in reason


def test_unique_candidate_with_conflicting_invoice_is_not_matched(demo):
    """S19: one candidate, same amount, but Stripe names a different invoice than QBO applied it to."""
    db, _, run_id = demo
    assert status_of(db, run_id, "qbo_payment", "S19") == ["NEEDS_REVIEW"]
    assert status_of(db, run_id, "stripe_payment", "S19") == ["NEEDS_REVIEW"]
    assert db.scalar("SELECT COUNT(*) FROM payment_matches m JOIN qbo_payments q ON q.payment_id = m.qbo_payment_id "
                     "WHERE m.run_id = ? AND q.scenario_id LIKE 'S19%'", (run_id,)) == 0


def test_records_of_unregistered_customers_are_out_of_scope(fresh_demo):
    """A QBO sandbox ships with sample customers; they must not pollute reconciliation."""
    db, settings, run_id = fresh_demo
    now = "2026-11-30T06:00:00+00:00"
    db.execute("INSERT INTO invoices (invoice_id, source, source_invoice_id, doc_number, client_id, location_id, "
               "qbo_customer_id, billing_period, txn_date, due_date, total_cents, balance_cents, currency, private_note, "
               "scenario_id, run_id, ingested_at) VALUES (?, 'qbo', '9001', '1037', NULL, NULL, '58', NULL, "
               "'2026-10-01', '2026-10-31', 36207, 36207, 'USD', NULL, NULL, ?, ?)",
               ("00000000-0000-0000-0000-00000000a001", run_id, now))
    db.execute("INSERT INTO qbo_payments (payment_id, source_payment_id, client_id, location_id, qbo_customer_id, "
               "txn_date, total_cents, unapplied_cents, currency, payment_method_id, payment_method_name, "
               "payment_method_type, payment_ref_num, private_note, referenced_stripe_id, scenario_id, run_id, "
               "ingested_at) VALUES (?, '9002', NULL, NULL, '58', '2026-10-05', 5000, 5000, 'USD', '1', "
               "'Credit Card (Stripe)', 'CREDIT_CARD', NULL, NULL, NULL, NULL, ?, ?)",
               ("00000000-0000-0000-0000-00000000a002", run_id, now))
    db.commit()
    from finrecon import pipeline
    from finrecon.dashboard_data import overview

    out = pipeline.reconcile_all(db, settings, run_id)
    assert db.scalar("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND source_record_id IN "
                     "('9001', '9002')", (run_id,)) == 0
    assert out["evaluation"]["unexpected"] == 0 and out["evaluation"]["scenarios_passed"] == 27
    assert overview(db, run_id)["out_of_scope_records"] == 2
