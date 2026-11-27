"""The read-only queries behind the dashboard and the static report."""
import json

from finrecon import dashboard_data as dd
from finrecon.report import write_report


def test_overview_numbers_are_consistent(demo):
    db, _, run_id = demo
    o = dd.overview(db, run_id)
    assert o["clients"] == 3 and o["locations"] == 15
    assert o["exceptions"] == db.scalar("SELECT COUNT(*) FROM reconciliation_exceptions WHERE run_id = ?", (run_id,))
    assert 0 < o["match_rate"] < 1
    assert o["open_ar_cents"] > 0
    assert o["evaluation"]["scenarios_passed"] == o["evaluation"]["scenarios_total"]


def test_drill_down_returns_source_row_allocations_and_raw_pages(demo):
    db, _, run_id = demo
    r = db.query("SELECT record_type, record_id FROM reconciliation_results WHERE run_id = ? "
                 "AND record_type = 'qbo_payment' AND scenario_id LIKE 'S03%'", (run_id,))[0]
    d = dd.record_detail(db, run_id, r["record_type"], r["record_id"])
    assert d["result"]["status"] == "MATCHED"
    assert d["source_row"]["referenced_stripe_id"].startswith("pi_")
    assert len(d["allocations"]) == 2
    assert d["raw_pages"]


def test_other_views(demo):
    db, settings, run_id = demo
    assert len(dd.data_status(db)) == 17          # qbo + stripe + 15 locations
    assert dd.ingestion_history(db)
    assert any(not r["passed"] for r in dd.quality_findings(db, run_id))
    assert len(dd.billing(db, run_id)) == 60
    assert len(dd.payouts(db)) == db.scalar("SELECT COUNT(*) FROM stripe_payouts")
    presence = {r["presence"] for r in dd.cross_system(db)}
    assert {"both", "only_in_stripe", "only_in_qbo", "amount_differs"} <= presence
    catalog = json.loads((settings.fixture_dir.parent.parent / "tests" / "fixtures" /
                          "expected_reconciliation.json").read_text())["scenarios"]
    board = dd.scenario_scoreboard(db, run_id, catalog)
    assert len(board) == 27 and all(b["passed"] for b in board)


def test_static_report_renders(demo, tmp_path):
    db, settings, _ = demo
    out = write_report(db, settings, str(tmp_path / "snap.html"))
    html = open(out).read()
    assert "Not affiliated" in html and "MISSING_IN_QBO" in html and "<table" in html


def test_exception_queue_groups_records_into_incidents(demo):
    db, _, run_id = demo
    q = dd.exception_queue(db, run_id)
    assert q["record_count"] == db.scalar("SELECT COUNT(*) FROM reconciliation_exceptions WHERE run_id = ?", (run_id,))
    assert q["incident_count"] < q["record_count"]                  # mismatches have one record per side
    prios = [i["priority"] for i in q["incidents"]]
    assert prios == sorted(prios) and prios[0] == 1
    s04 = [i for i in q["incidents"] if any((m["scenario_id"] or "").startswith("S04") for m in i["records"])]
    assert len(s04) == 1 and len(s04[0]["records"]) == 2 and s04[0]["exposure_cents"] == 4500
    s11 = [i for i in q["incidents"] if any((m["scenario_id"] or "").startswith("S11") for m in i["records"])]
    assert len(s11) == 1 and len(s11[0]["records"]) == 3                  # one QBO entry, two candidate deposits


def _row(record_type, rid, sid, counterparts="", client="c1", currency="USD", status="NEEDS_REVIEW"):
    return {"record_type": record_type, "record_id": rid, "source_record_id": sid, "counterpart_source_ids": counterparts,
            "client_id": client, "currency": currency, "status": status}


def _groups(rows):
    return sorted(sorted(r["record_id"] for r in g) for g in dd.group_incidents(rows))


def test_incidents_link_a_normal_pair():
    rows = [_row("stripe_payment", "a", "pi_1", "145"), _row("qbo_payment", "b", "145", "pi_1")]
    assert _groups(rows) == [["a", "b"]]


def test_incidents_same_external_id_in_other_entity_type_is_not_merged():
    # QBO numbers payments and refunds separately: payment 145 and refund 145 are unrelated.
    rows = [_row("qbo_payment", "p", "145"), _row("qbo_refund", "r", "145"), _row("stripe_payment", "s", "pi_1", "145")]
    assert _groups(rows) == [["p", "s"], ["r"]]


def test_incidents_different_client_or_currency_stay_separate():
    assert _groups([_row("stripe_payment", "a", "pi_1", "145"), _row("qbo_payment", "b", "145", client="c2")]) == [["a"], ["b"]]
    assert _groups([_row("stripe_payment", "a", "pi_1", "145"), _row("qbo_payment", "b", "145", currency="GBP")]) == [["a"], ["b"]]
    assert _groups([_row("stripe_payment", "a", "pi_1", "145", client=None), _row("qbo_payment", "b", "145", client=None)]) == [["a"], ["b"]]


def test_incidents_ambiguous_external_id_is_not_merged():
    rows = [_row("stripe_payment", "a", "pi_1", "145"), _row("qbo_payment", "b1", "145"), _row("qbo_payment", "b2", "145")]
    assert _groups(rows) == [["a"], ["b1"], ["b2"]]
