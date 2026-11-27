"""Tests 8 and 9: ground-truth evaluation (and that it can fail), gross/net/payout checks."""
import hashlib
import json
from pathlib import Path

from finrecon.recon import accounting, evaluator
from finrecon.recon.accounting import journal_preview
from seed.generate_offline import GROUND_TRUTH, OUT, render
from seed.scenarios import build_plan, ground_truth


def test_engine_output_matches_ground_truth(demo):
    """Test 8."""
    db, _, run_id = demo
    s = evaluator.latest_summary(db, run_id)
    assert s["items"] > 200
    assert s["wrong"] == s["missed"] == s["unexpected"] == 0
    assert s["false_positive_exceptions"] == 0 and s["missed_exceptions"] == 0
    assert s["scenarios_passed"] == s["scenarios_total"] == 27


def test_evaluator_detects_a_wrong_classification(fresh_demo):
    db, _, run_id = fresh_demo
    db.execute("UPDATE reconciliation_results SET status = 'MATCHED', is_exception = ? WHERE run_id = ? "
               "AND record_type = 'stripe_payment' AND scenario_id LIKE 'S05%'", (False, run_id))
    db.execute("DELETE FROM reconciliation_results WHERE run_id = ? AND record_type = 'stripe_refund' "
               "AND scenario_id LIKE 'S08%'", (run_id,))
    db.commit()
    s = evaluator.evaluate(db, run_id)
    assert s["wrong"] == 1 and s["missed"] == 1
    assert s["missed_exceptions"] == 2
    assert set(s["failed_scenarios"]) == {"S05", "S08"}


def test_evaluator_flags_false_positive_exceptions(fresh_demo):
    db, _, run_id = fresh_demo
    db.execute("UPDATE reconciliation_results SET status = 'MISSING_IN_QBO', is_exception = ? WHERE run_id = ? "
               "AND record_type = 'stripe_payment' AND scenario_id LIKE 'S14%'", (True, run_id))
    db.commit()
    assert evaluator.evaluate(db, run_id)["false_positive_exceptions"] == 1


def test_ground_truth_and_fixtures_are_reproducible():
    """The committed ground truth and fixtures equal a fresh deterministic render."""
    plan = build_plan()
    assert json.loads(Path(GROUND_TRUTH).read_text()) == json.loads(json.dumps(ground_truth(plan)))
    rendered = render(plan)
    for rel in ("qbo/Invoice.json", "stripe/payment_intents.json", "ad_platform/dataset.json"):
        on_disk = json.loads((OUT / rel).read_text())
        fresh = json.loads(json.dumps(rendered[rel], default=lambda o: float(o) if hasattr(o, "as_tuple") else str(o)))
        assert hashlib.sha256(json.dumps(on_disk, sort_keys=True).encode()).hexdigest() == \
               hashlib.sha256(json.dumps(fresh, sort_keys=True).encode()).hexdigest(), rel


def test_gross_fees_net_and_payouts_reconcile(demo):
    """Test 9."""
    db, _, run_id = demo
    rows = db.query("SELECT check_name, passed FROM accounting_checks WHERE run_id = ?", (run_id,))
    assert {r["check_name"] for r in rows} == {"gross_minus_fee_equals_net", "charge_gross_equals_payment",
                                               "payout_equals_sum_of_net"}
    assert all(r["passed"] for r in rows)


def test_accounting_check_catches_a_bad_net(fresh_demo):
    db, _, run_id = fresh_demo
    db.execute("UPDATE stripe_balance_transactions SET net_cents = net_cents + 1 WHERE source_bt_id = "
               "(SELECT MIN(source_bt_id) FROM stripe_balance_transactions WHERE type = 'charge')")
    db.commit()
    counts = accounting.run_checks(db, run_id)
    assert counts.get("gross_minus_fee_equals_net:fail") == 1


def test_journal_previews_balance(demo):
    db, _, run_id = demo
    for sp in db.query("SELECT source_payment_id FROM stripe_payments"):
        entries = journal_preview(db, sp["source_payment_id"], run_id)
        assert entries and all(e.balanced for e in entries)
