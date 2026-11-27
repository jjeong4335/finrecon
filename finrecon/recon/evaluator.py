"""Compare actual classifications against the ground truth.

Ground truth (tests/fixtures/expected_reconciliation.json) is written from the
scenario plan, keyed by seed_key, which travels with each seeded record
(Stripe metadata / QBO memo). API-generated IDs are never used as keys.

Outcomes per item: correct | wrong | missed (expected, not produced) |
unexpected (produced, not in ground truth). The dashboard reads these rows;
no success number is ever hard-coded.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT
from ..db import Database
from .engine import EXCEPTION_STATUSES

GROUND_TRUTH = ROOT / "tests" / "fixtures" / "expected_reconciliation.json"


def evaluate(db: Database, run_id: str, path: Path = GROUND_TRUTH) -> dict:
    truth = json.loads(path.read_text())
    db.execute("DELETE FROM evaluation_results WHERE run_id = ?", (run_id,))
    rows: list[dict] = []

    def add(dim, key, scenario, expected, actual):
        if expected is None:
            outcome = "unexpected"
        elif actual is None:
            outcome = "missed"
        else:
            outcome = "correct" if expected == actual else "wrong"
        rows.append({"run_id": run_id, "dimension": dim, "item_key": key, "scenario": scenario,
                     "expected": expected, "actual": actual, "outcome": outcome})

    actual_tx = {(r["record_type"], r["scenario_id"]): r["status"] for r in db.query(
        "SELECT record_type, scenario_id, status FROM reconciliation_results WHERE run_id = ?", (run_id,))}
    expected_tx = {(t["record_type"], t["seed_key"]): t for t in truth["transactions"]}
    for k, t in expected_tx.items():
        add("transaction", f"{k[0]}|{k[1]}", t["scenario"], t["expected"], actual_tx.get(k))
    for k, status in actual_tx.items():
        if k not in expected_tx:
            add("transaction", f"{k[0]}|{k[1]}", (k[1] or "?").split(":")[0], None, status)

    actual_ds = {r["scope_key"]: r["status"] for r in db.query(
        "SELECT c.client_key || '/' || l.location_key AS scope_key, d.status FROM data_status d "
        "JOIN locations l ON l.location_id = d.location_id JOIN clients c ON c.client_id = l.client_id "
        "WHERE d.source = 'ad_platform'")}
    for d in truth["data_status"]:
        add("data_status", d["scope"], d["scenario"], d["expected"], actual_ds.get(d["scope"]))

    actual_bill = {r["check_key"]: r["status"] for r in db.query(
        "SELECT check_key, status FROM billing_checks WHERE run_id = ?", (run_id,))}
    expected_bill = {b["check_key"]: b for b in truth["billing"]}
    for k, b in expected_bill.items():
        add("billing", k, b["scenario"], b["expected"], actual_bill.get(k))
    for k, status in actual_bill.items():
        if k not in expected_bill:
            add("billing", k, "?", None, status)

    db.upsert_many("evaluation_results", rows, ["run_id", "dimension", "item_key"])
    db.commit()
    return summarize(rows, truth["scenarios"])


def summarize(rows: list[dict], scenarios: dict | None = None) -> dict:
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    scen: dict[str, bool] = {}
    for r in rows:
        if r["scenario"] and r["scenario"] != "?":
            scen[r["scenario"]] = scen.get(r["scenario"], True) and r["outcome"] == "correct"
    false_pos = sum(1 for r in rows if r["dimension"] == "transaction" and r["actual"] in EXCEPTION_STATUSES
                    and r["expected"] not in EXCEPTION_STATUSES)
    missed_exc = sum(1 for r in rows if r["dimension"] == "transaction" and r["expected"] in EXCEPTION_STATUSES
                     and r["actual"] != r["expected"])
    return {
        "items": len(rows), "correct": by_outcome.get("correct", 0), "wrong": by_outcome.get("wrong", 0),
        "missed": by_outcome.get("missed", 0), "unexpected": by_outcome.get("unexpected", 0),
        "false_positive_exceptions": false_pos, "missed_exceptions": missed_exc,
        "scenarios_total": len(scen), "scenarios_passed": sum(scen.values()),
        "failed_scenarios": sorted(k for k, ok in scen.items() if not ok),
    }


def latest_summary(db: Database, run_id: str) -> dict | None:
    rows = db.query("SELECT * FROM evaluation_results WHERE run_id = ?", (run_id,))
    return summarize(rows) if rows else None
