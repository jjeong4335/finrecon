"""Tests 1, 7, 10, 12: idempotency, failure handling, traceability, coverage."""
import hashlib
import json
from dataclasses import replace

import pytest

from finrecon import pipeline
from finrecon.db import Database
from finrecon.ingestion.runner import IngestionRunner, build_sources
from tests.conftest import no_sleep

TABLES = ["clients", "locations", "source_id_map", "invoices", "qbo_payments", "payment_allocations",
          "qbo_refunds", "stripe_payments", "stripe_refunds", "stripe_balance_transactions", "stripe_payouts",
          "ad_spend"]


def _counts(db):
    return {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in TABLES}


def _checksums(db):
    return {
        "invoices": db.scalar("SELECT SUM(total_cents) FROM invoices"),
        "qbo": db.scalar("SELECT SUM(total_cents) FROM qbo_payments"),
        "stripe": db.scalar("SELECT SUM(amount_cents) FROM stripe_payments"),
        "spend": db.scalar("SELECT SUM(amount_cents) FROM ad_spend"),
    }


def test_running_the_same_etl_twice_creates_no_duplicates(fresh_demo):
    """Test 1."""
    db, settings, run_id = fresh_demo
    before, sums = _counts(db), _checksums(db)
    results_before = db.query("SELECT record_type, scenario_id, status FROM reconciliation_results WHERE run_id = ? "
                              "ORDER BY record_type, scenario_id", (run_id,))
    pipeline.ingest(db, settings, 2, settings.recon_as_of, sleep=no_sleep)
    pipeline.reconcile_all(db, settings, run_id)
    assert _counts(db) == before
    assert _checksums(db) == sums
    assert db.query("SELECT record_type, scenario_id, status FROM reconciliation_results WHERE run_id = ? "
                    "ORDER BY record_type, scenario_id", (run_id,)) == results_before


def test_failed_pull_is_no_data_not_zero(demo):
    """Test 7: failure -> NO_DATA, never $0; genuine zero -> ZERO_TRANSACTIONS."""
    db, _, run_id = demo
    status = {r["scope"]: r for r in db.query("SELECT * FROM data_status WHERE source = 'ad_platform'")}
    assert status["sunbeam/paterson"]["status"] == "NO_DATA"
    assert status["sunbeam/morristown"]["status"] == "ZERO_TRANSACTIONS"
    paterson = db.query("SELECT spend_cents, status FROM billing_checks WHERE run_id = ? AND check_key LIKE "
                        "'sunbeam/paterson/%'", (run_id,))
    assert paterson and all(r["spend_cents"] is None and r["status"] == "BILLING_DATA_UNAVAILABLE" for r in paterson)
    morristown = db.query("SELECT spend_cents, status FROM billing_checks WHERE run_id = ? AND check_key LIKE "
                          "'sunbeam/morristown/%'", (run_id,))
    assert all(r["spend_cents"] == 0 and r["status"] == "NO_INVOICE_EXPECTED" for r in morristown)


def test_incomplete_and_stale_pulls_keep_last_validated_data(demo):
    db, settings, _ = demo
    status = {r["scope"]: r for r in db.query("SELECT * FROM data_status WHERE source = 'ad_platform'")}
    for scope, expected in (("harbor/astoria", "STALE"), ("maple/yonkers", "INCOMPLETE"),
                            ("maple/whiteplains", "INCOMPLETE")):
        assert status[scope]["status"] == expected
        assert status[scope]["validated_through"] == settings.first_run_as_of.isoformat()
        lid = status[scope]["location_id"]
        # Run 2's partial/failed data was not loaded; run 1's validated rows remain.
        assert db.scalar("SELECT MAX(spend_date) FROM ad_spend WHERE location_id = ?", (lid,)) == "2026-11-25"
        assert db.scalar("SELECT COUNT(*) FROM ad_spend WHERE location_id = ?", (lid,)) > 0


def test_rate_limit_is_retried_and_pagination_is_complete(demo):
    """Test 12: 429 retried; expected vs received records verified for every validated pull."""
    db, _, _ = demo
    hob = db.query("SELECT retry_count, status FROM ingestion_runs WHERE source = 'ad_platform' "
                   "AND scope = 'harbor/hoboken' ORDER BY run_id")
    assert [(r["retry_count"], r["status"]) for r in hob] == [(1, "succeeded"), (1, "succeeded")]
    for r in db.query("SELECT * FROM ingestion_runs WHERE source = 'ad_platform' AND status = 'succeeded'"):
        assert r["records_expected"] == r["records_received"]
    yon = db.query("SELECT records_expected, records_received FROM ingestion_runs WHERE run_id LIKE 'run-02%' "
                   "AND scope = 'maple/yonkers'")[0]
    assert yon["records_received"] < yon["records_expected"]


def test_expected_locations_checked_against_platform(fresh_demo, tmp_path):
    db, settings, _ = fresh_demo
    src = build_sources(settings, settings.recon_as_of, 3, sleep=no_sleep)
    src.registry["clients"][0]["locations"].append({
        "location_key": "ghost", "name": "Ghost Branch", "opened_on": "2026-07-01",
        "external": {"qbo_customer_id": "999", "ad_location_id": "adv-101-99"}})
    IngestionRunner(db, settings, src, 3, settings.recon_as_of).run()
    row = db.query("SELECT status, detail FROM data_status WHERE scope = 'harbor/ghost'")[0]
    assert row["status"] == "NO_DATA" and "not listed" in row["detail"]
    dq = db.query("SELECT passed FROM data_quality_results WHERE run_id LIKE 'run-03%' AND scope = 'harbor' "
                  "AND check_name = 'expected_vs_received'")[0]
    assert dq["passed"] is False


def test_duplicate_rows_are_deduplicated_with_warning(demo):
    db, _, _ = demo
    dq = db.query("SELECT passed, severity FROM data_quality_results WHERE scope = 'sunbeam/newark' "
                  "AND check_name = 'duplicate_external_ids' AND run_id LIKE 'run-02%'")[0]
    assert dq == {"passed": False, "severity": "warning"}
    assert db.scalar("SELECT COUNT(*) FROM ad_spend") == db.scalar("SELECT COUNT(DISTINCT source_spend_id) FROM ad_spend")


def test_every_record_traces_to_source_id_run_and_raw_bytes(demo):
    """Test 10."""
    db, settings, run_id = demo
    run_ids = {r["run_id"] for r in db.query("SELECT run_id FROM pipeline_runs")}
    for table, col in (("invoices", "source_invoice_id"), ("qbo_payments", "source_payment_id"),
                       ("stripe_payments", "source_payment_id"), ("ad_spend", "source_spend_id")):
        rows = db.query(f"SELECT {col} AS sid, run_id FROM {table}")
        assert rows and all(r["sid"] and r["run_id"] in run_ids for r in rows), table
    for r in db.query("SELECT record_type, source_record_id FROM reconciliation_results WHERE run_id = ?", (run_id,)):
        assert r["source_record_id"]
    manifest = db.query("SELECT path, sha256 FROM raw_data_manifest")
    assert len(manifest) > 100
    for m in manifest[:50]:
        data = (settings.raw_dir / m["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == m["sha256"]


def test_raw_archive_contains_no_credentials(demo):
    db, settings, _ = demo
    for m in db.query("SELECT path FROM raw_data_manifest"):
        text = (settings.raw_dir / m["path"]).read_text().lower()
        assert "authorization" not in text and "sk_test" not in text and "refresh_token" not in text


def test_offline_adapters_paginate(demo):
    db, _, _ = demo
    keys = {r["request_key"] for r in db.query("SELECT request_key FROM raw_data_manifest WHERE run_id LIKE "
                                               "'run-02%' AND source IN ('qbo', 'stripe')")}
    assert {"Invoice_start1", "Invoice_start21", "Invoice_start41"} <= keys       # 55 invoices / 20 per page
    assert {"payment_intents_p1", "payment_intents_p2"} <= keys                   # 44 PIs / 25 per page


def test_referential_integrity_enforced(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'ri.db'}")
    db.init_schema()
    with pytest.raises(Exception):
        db.execute("INSERT INTO payment_allocations (qbo_payment_id, invoice_id, amount_cents) VALUES (?, ?, ?)",
                   ("00000000-0000-0000-0000-000000000000", "00000000-0000-0000-0000-000000000001", 100))
        db.commit()


def test_live_mode_refuses_non_test_stripe_key():
    from finrecon.config import Settings

    with pytest.raises(ValueError):
        replace(Settings(), stripe_secret_key="sk_live_123")


def test_registry_is_consistent_with_fixtures(demo):
    db, settings, _ = demo
    registry = json.loads((settings.fixture_dir / "registry.json").read_text())
    n_locations = sum(len(c["locations"]) for c in registry["clients"])
    assert db.scalar("SELECT COUNT(*) FROM locations") == n_locations
