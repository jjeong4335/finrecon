"""Command-line entry point.

  python -m finrecon.pipeline demo        reset DB, two ingestion runs, reconcile, evaluate
  python -m finrecon.pipeline ingest --run-no 3 --as-of 2026-12-01
  python -m finrecon.pipeline reconcile   payment + billing + accounting checks + evaluation
  python -m finrecon.pipeline report      write a static HTML snapshot (reports/snapshot.html)

MODE=offline (default) needs no credentials. MODE=live reads the QBO/Stripe
sandboxes (see .env.example and seed/live_seed.py).
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .config import get_settings
from .db import Database
from .ingestion.runner import IngestionRunner, build_sources, run_id_for
from .recon import accounting, billing, engine, evaluator


def latest_run_id(db: Database) -> str | None:
    return db.scalar("SELECT run_id FROM pipeline_runs WHERE status = 'completed' ORDER BY as_of DESC, run_no DESC LIMIT 1")


def ingest(db: Database, settings, run_no: int, as_of: date, sleep=None) -> dict:
    sources = build_sources(settings, as_of, run_no, sleep=sleep)
    return IngestionRunner(db, settings, sources, run_no, as_of).run()


def reconcile_all(db: Database, settings, run_id: str) -> dict:
    return {
        "payments": engine.reconcile(db, settings, run_id),
        "billing": billing.reconcile_billing(db, settings, run_id),
        "accounting": accounting.run_checks(db, run_id),
        "evaluation": evaluator.evaluate(db, run_id),
    }


def demo(settings=None, db: Database | None = None, sleep=None) -> dict:
    """The reproducible offline demo: two runs, five days apart, then reconcile."""
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    db.drop_all()
    db.init_schema()
    out = {"run1": ingest(db, settings, 1, settings.first_run_as_of, sleep),
           "run2": ingest(db, settings, 2, settings.recon_as_of, sleep)}
    out["reconciliation"] = reconcile_all(db, settings, run_id_for(settings.recon_as_of, 2))
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="finrecon")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--strict", action="store_true",
                   help="exit with status 1 unless every scenario matches the ground truth")
    sub.add_parser("init-db")
    i = sub.add_parser("ingest")
    i.add_argument("--run-no", type=int, required=True)
    i.add_argument("--as-of", type=date.fromisoformat)
    sub.add_parser("reconcile")
    r = sub.add_parser("report")
    r.add_argument("--out", default=None)
    args = p.parse_args(argv)
    settings = get_settings()

    if args.cmd == "demo":
        result = demo(settings)
        ev = result["reconciliation"]["evaluation"]
        print(json.dumps(result["reconciliation"], indent=2))
        print(f"\nScenario validation: {ev['scenarios_passed']} / {ev['scenarios_total']} scenarios passed, "
              f"{ev['correct']} / {ev['items']} items correct, {ev['false_positive_exceptions']} false-positive "
              f"exceptions, {ev['missed_exceptions']} missed exceptions")
        perfect = (ev["scenarios_passed"] == ev["scenarios_total"] and ev["wrong"] == ev["missed"] == ev["unexpected"] == 0)
        if args.strict and not perfect:
            raise SystemExit(f"ground-truth evaluation failed: {ev['failed_scenarios']}")
        return
    db = Database(settings.database_url)
    if args.cmd == "init-db":
        db.init_schema()
    elif args.cmd == "ingest":
        db.init_schema()
        print(json.dumps(ingest(db, settings, args.run_no, args.as_of or settings.recon_as_of), indent=2))
    elif args.cmd == "reconcile":
        run_id = latest_run_id(db)
        if not run_id:
            raise SystemExit("no completed ingestion run")
        print(json.dumps(reconcile_all(db, settings, run_id), indent=2))
    elif args.cmd == "report":
        from .report import write_report

        print(write_report(db, settings, args.out))


if __name__ == "__main__":
    main()
