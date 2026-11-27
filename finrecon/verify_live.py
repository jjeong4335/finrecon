"""Check that what the deployed site shows still matches the live sandbox APIs.

    set -a; source .env; set +a          # MODE=live + sandbox credentials
    python -m finrecon.verify_live

1. Capture the current QuickBooks and Stripe sandbox data (same code as
   `seed.live_seed capture`) into data/verify/<timestamp>/.
2. Compare it object by object with fixtures/live_snapshot (what the deployed
   site serves): added, removed and changed objects per entity.
3. Run the full pipeline offline on both and compare every reconciliation
   result (status and amounts); both must score 27/27 against the ground truth.
4. Report payouts: every payout must equal the net of the balance transactions
   Stripe attached to it.
Nothing on EC2 or in the production database is touched. Exit code 1 means a
difference that changes reconciliation results, or a failed check.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, get_settings
from .db import Database
from .ingestion.runner import run_id_for
from .money import fmt

DEPLOYED = ROOT / "fixtures" / "live_snapshot"
VOLATILE = {"LastUpdatedTime", "SyncToken", "_fixture_created", "_fixture_payout", "status_transitions"}


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


def diff_snapshots(old: Path, new: Path) -> dict[str, dict[str, list[str]]]:
    out = {}
    for path in sorted({p.relative_to(old) for p in old.glob("*/*.json")} | {p.relative_to(new) for p in new.glob("*/*.json")}):
        if path.parts[0] == "ad_platform":
            continue
        load = lambda base: {str(o.get("Id") or o.get("id")): _strip(o) for o in json.loads((base / path).read_text())} \
            if (base / path).exists() else {}  # noqa: E731
        a, b = load(old), load(new)
        out[str(path)] = {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                          "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    return out


def run_offline(fixture_dir: Path, work: Path) -> tuple[Database, str, dict]:
    from . import pipeline

    s = replace(get_settings(), mode="offline", fixture_dir=fixture_dir,
                database_url=f"sqlite:///{work / 'verify.db'}", raw_dir=work / "raw")
    db = Database(s.database_url)
    result = pipeline.demo(s, db, sleep=lambda _s: None)
    return db, run_id_for(s.recon_as_of, 2), result["reconciliation"]["evaluation"]


def results_of(db: Database, run_id: str) -> dict[tuple, tuple]:
    return {(r["record_type"], r["scenario_id"] or r["source_record_id"]): (r["status"], r["amount_cents"], r["diff_cents"])
            for r in db.query("SELECT * FROM reconciliation_results WHERE run_id = ?", (run_id,))}


def main() -> int:
    settings = get_settings()
    if settings.mode != "live":
        print("Set MODE=live and the sandbox credentials first (set -a; source .env; set +a)")
        return 2
    from seed.live_seed import capture

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = ROOT / "data" / "verify" / stamp
    current = capture(str(work / "snapshot"))
    ok = True

    print("\n== 1. Current API data vs the deployed snapshot")
    for name, d in diff_snapshots(DEPLOYED, current).items():
        if any(d.values()):
            print(f"  {name:<36} +{len(d['added'])} added  -{len(d['removed'])} removed  ~{len(d['changed'])} changed"
                  + (f"   e.g. {', '.join((d['added'] + d['changed'] + d['removed'])[:3])}" if any(d.values()) else ""))
        else:
            print(f"  {name:<36} identical")

    print("\n== 2. Reconciliation on deployed snapshot vs current API data")
    db_old, rid, ev_old = run_offline(DEPLOYED, work / "deployed")
    db_new, _, ev_new = run_offline(current, work / "current")
    for label, ev in (("deployed", ev_old), ("current ", ev_new)):
        good = ev["scenarios_passed"] == ev["scenarios_total"] and not (ev["wrong"] or ev["missed"] or ev["unexpected"])
        ok &= good
        print(f"  {label}: {ev['scenarios_passed']}/{ev['scenarios_total']} scenarios, {ev['correct']}/{ev['items']} items"
              f"{'' if good else '  <-- FAILS ground truth'}")
    a, b = results_of(db_old, rid), results_of(db_new, rid)
    changed = [k for k in set(a) & set(b) if a[k] != b[k]]
    only_old, only_new = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    if changed or only_old or only_new:
        ok = False
        print(f"  DIFFERENT: {len(changed)} changed, {len(only_old)} only in deployed, {len(only_new)} only in current")
        for k in sorted(changed)[:10]:
            print(f"    {k}: {a[k]} -> {b[k]}")
    else:
        print(f"  identical: all {len(a)} reconciliation results match")
    out_of_scope = db_new.scalar("SELECT COUNT(*) FROM stripe_payments WHERE client_id IS NULL")
    print(f"  Stripe payments outside the client registry (e.g. payout float): {out_of_scope}")

    print("\n== 3. Payouts (current API data)")
    payouts = db_new.query("SELECT * FROM accounting_checks WHERE run_id = ? AND check_name = 'payout_equals_sum_of_net'",
                           (rid,))
    if not payouts:
        print("  no payouts yet. Run `python -m seed.live_seed payouts`, keep automatic payouts on, and re-run "
              "after Stripe's next payout.")
    for p in payouts:
        ok &= bool(p["passed"])
        print(f"  {p['ref']}: payout {fmt(p['actual_cents'])} vs net of attached transactions "
              f"{fmt(p['expected_cents'])}  {'OK' if p['passed'] else 'MISMATCH'}  ({p['detail']})")

    print(f"\nResult: {'OK' if ok else 'DIFFERENCES FOUND'} (work files: {work})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
