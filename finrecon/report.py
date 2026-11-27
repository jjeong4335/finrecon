"""Static, self-contained HTML snapshot of the latest run.

A fallback that needs no running server (attach it, or host it anywhere),
built from the same queries as the dashboard.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import dashboard_data as dd
from .config import ROOT, Settings
from .db import Database
from .money import fmt

DISCLAIMER = ("This demonstration uses synthetic sandbox financial records and historical public retail transaction data from"
              " UCI. No real QuickBooks or Stripe customer transactions are included. Not affiliated with any company named in this project, Intuit, or Stripe.")

CSS = """
:root{--ink:#1d2430;--muted:#5d6778;--line:#e3e7ee;--bg:#f7f8fa;--ok:#1f7a4d;--warn:#a86400;--bad:#b3261e;--acc:#2b59c3}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:#fff;border-bottom:1px solid var(--line);padding:20px 32px}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:32px 0 10px}.sub{color:var(--muted);font-size:13px}
main{max-width:1180px;margin:0 auto;padding:8px 32px 48px}
.banner{background:#eef3ff;border:1px solid #cdd9f7;color:#22408f;padding:8px 12px;border-radius:6px;font-size:12.5px;margin-top:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px;margin-top:20px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi .v{font-size:19px;white-space:nowrap;font-weight:650;font-variant-numeric:tabular-nums}.kpi .l{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:12.5px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#fafbfc;font-weight:600;color:var(--muted)}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap}
.ok{background:#e6f4ec;color:var(--ok)}.warn{background:#fff3e0;color:var(--warn)}.bad{background:#fdecea;color:var(--bad)}.neu{background:#eef0f4;color:#3c4656}
.badge{display:inline-flex;gap:8px;align-items:center;background:#e6f4ec;color:var(--ok);border:1px solid #bfe3cf;padding:6px 12px;border-radius:6px;font-weight:600;margin-top:16px}
.badge.fail{background:#fdecea;color:var(--bad);border-color:#f4c7c3}
footer{color:var(--muted);font-size:12px;margin-top:40px}
"""

GOOD = {"MATCHED", "VALIDATED", "BILLING_MATCHED", "ZERO_TRANSACTIONS", "NO_INVOICE_EXPECTED", "NON_STRIPE_PAYMENT"}
NEUTRAL = {"OPEN_AR", "PARTIAL", "PENDING_SYNC"}
WARN = {"STALE", "INCOMPLETE", "NEEDS_REVIEW", "BILLING_DATA_UNAVAILABLE"}


def pill(status: str) -> str:
    cls = "ok" if status in GOOD else "neu" if status in NEUTRAL else "warn" if status in WARN else "bad"
    return f'<span class="pill {cls}">{html.escape(status)}</span>'


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    def cell(i, c):
        if i in numeric:
            return f'<td class="n">{c}</td>'
        short = len(re.sub(r"<[^>]+>", "", str(c))) <= 14
        return f'<td style="white-space:nowrap">{c}</td>' if short else f"<td>{c}</td>"

    body = "".join("<tr>" + "".join(cell(i, c) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def e(v) -> str:
    return html.escape("" if v is None else str(v))


def write_report(db: Database, settings: Settings, out: str | None = None) -> str:
    run = dd.latest_run(db)
    if not run:
        raise SystemExit("no completed run")
    run_id = run["run_id"]
    o = dd.overview(db, run_id)
    ev = o["evaluation"] or {}
    catalog = json.loads((ROOT / "tests" / "fixtures" / "expected_reconciliation.json").read_text())["scenarios"]

    kpis = [
        (fmt(o["invoiced_cents"]), f"Invoiced ({o['invoice_count']} invoices)"),
        (fmt(o["recorded_payments_cents"]), "Payments recorded in QBO"),
        (fmt(o["open_ar_cents"]), "Open AR"),
        (f"{o['match_rate'] * 100:.1f}%" if o["match_rate"] is not None else "—", "Payment records reconciled"),
        (str(o["exceptions"]), "Reconciliation exceptions"),
        (str(o["billing_issues"]), "Billing exceptions"),
        (str(o["data_issues"]), "Sources not fully validated"),
    ]
    badge_ok = ev and ev["scenarios_passed"] == ev["scenarios_total"] and not ev["false_positive_exceptions"]
    badge = (f'<div class="badge{"" if badge_ok else " fail"}">Scenario validation: {ev.get("scenarios_passed")} / '
             f'{ev.get("scenarios_total")} scenarios passed · {ev.get("correct")} / {ev.get("items")} items correct · '
             f'{ev.get("false_positive_exceptions")} false-positive exceptions</div>') if ev else ""

    exc = dd.results(db, run_id, exceptions_only=True)
    exc_rows = [[pill(r["status"]), e(r["record_type"]), e(r["client"]), e(r["location"] or "client-level"),
                 e(r["source_record_id"]), fmt(r["amount_cents"]), fmt(r["diff_cents"]) if r["diff_cents"] else "",
                 e(r["reason"])] for r in exc]
    ar = [r for r in dd.results(db, run_id) if r["status"] in ("OPEN_AR", "PARTIAL", "PENDING_SYNC", "NON_STRIPE_PAYMENT")]
    ar_rows = [[pill(r["status"]), e(r["record_type"]), e(r["client"]), e(r["location"] or ""),
                e(r["source_record_id"]), fmt(r["amount_cents"]), e(r["reason"])] for r in ar]
    ds_rows = [[e(r["source"]), e(r["scope"]), pill(r["status"]), e(r["validated_through"]), e(r["detail"])]
               for r in dd.data_status(db)]
    bill = [r for r in dd.billing(db, run_id) if r["status"] != "BILLING_MATCHED"]
    bill_rows = [[pill(r["status"]), e(r["client"]), e(r["location"]), e(r["billing_period"]),
                  fmt(r["expected_cents"]), fmt(r["invoiced_cents"]), e(r["reason"])] for r in bill]
    board_rows = [[e(b["scenario"]), e(b["title"]), e(b["description"]), e(b["interview_question"]),
                   f'{b["correct"]}/{b["items"]}', pill("MATCHED" if b["passed"] else "AMOUNT_MISMATCH").replace(
                       "MATCHED", "PASS").replace("AMOUNT_MISMATCH", "FAIL")]
                  for b in dd.scenario_scoreboard(db, run_id, catalog)]
    acct_rows = [[e(a["check_name"]), f'{a["n_passed"]}/{a["total"]}'] for a in dd.accounting_summary(db, run_id)]

    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Finance Reconciliation Snapshot</title>
<style>{CSS}</style></head><body>
<header><h1>Multi-Location Finance Reconciliation</h1>
<div class="sub">Snapshot of run <b>{e(run_id)}</b> · reconciliation as of <b>{settings.recon_as_of}</b> ·
QuickBooks Online + Stripe + ad platform · {o['clients']} clients, {o['locations']} locations</div>
<div class="banner">{e(DISCLAIMER)}</div></header>
<main>
<div class="kpis">{''.join(f'<div class="kpi"><div class="v">{v}</div><div class="l">{e(l)}</div></div>' for v, l in kpis)}</div>
{badge}
<h2>Reconciliation exceptions ({len(exc_rows)})</h2>
{_table(["Status", "Record", "Client", "Location", "Source ID", "Amount", "Diff", "Why"], exc_rows, {5, 6})}
<h2>Billing checks other than BILLING_MATCHED: ad spend → invoice ({len(bill_rows)})</h2>
{_table(["Status", "Client", "Location", "Period", "Expected", "Invoiced", "Why"], bill_rows, {4, 5})}
<h2>Data status per source</h2>
{_table(["Source", "Scope", "Status", "Validated through", "Detail"], ds_rows)}
<h2>Not exceptions, but worth knowing ({len(ar_rows)})</h2>
{_table(["Status", "Record", "Client", "Location", "Source ID", "Amount", "Why"], ar_rows, {5})}
<h2>Scenario validation against ground truth</h2>
{_table(["Code", "Scenario", "What it tests", "Interview topic", "Items", "Result"], board_rows, {4})}
<h2>Accounting checks</h2>
{_table(["Check", "Passed"], acct_rows, {1})}
<footer>Generated from the database by finrecon.report. Code and methodology: {e(settings.github_url)}</footer>
</main></body></html>"""
    path = Path(out) if out else ROOT / "reports" / "snapshot.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
    return str(path)
