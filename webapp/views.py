"""Read-only views over the reconciliation database.

Every query lives in finrecon.dashboard_data (covered by the test suite); the
views only filter, shape and render. Nothing here writes to the database.
"""
from __future__ import annotations

import json
from datetime import date

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render

from finrecon import dashboard_data as dd
from finrecon.config import ROOT, get_settings
from finrecon.db import Database
from finrecon.ingestion.uci_retail import CITATION
from finrecon.recon.accounting import journal_preview
from finrecon.report import DISCLAIMER

RECORD_TYPES = {"invoice", "stripe_payment", "qbo_payment", "stripe_refund", "qbo_refund"}
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_url, autocommit=True)
    return _db


def reset_db() -> None:
    """Drop the cached connection (tests, or after a lost connection)."""
    global _db
    if _db is not None:
        try:
            _db.close()
        except Exception:
            pass
    _db = None


def _q(fn, *args, **kwargs):
    """Run a query function, reconnecting once if the connection was lost."""
    try:
        return fn(get_db(), *args, **kwargs)
    except Exception as exc:
        if type(exc).__name__ not in ("OperationalError", "InterfaceError"):
            raise
        reset_db()
        return fn(get_db(), *args, **kwargs)


def _catalog() -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "expected_reconciliation.json").read_text())["scenarios"]


def site_context(request) -> dict:
    """Available in every template."""
    settings = get_settings()
    try:
        run = _q(dd.latest_run)
    except Exception:
        run = None
    return {"disclaimer": DISCLAIMER, "run": run, "as_of": settings.recon_as_of, "github_url": settings.github_url,
            "nav": [("overview", "Overview"), ("reconciliation", "Reconciliation"), ("billing", "Billing & Payouts"),
                    ("data_quality", "Data Quality"), ("retail", "Retail (real data)"),
                    ("architecture", "Architecture")]}


def _needs_run(request, template: str):
    return render(request, "webapp/no_data.html", status=503)


# ---------------------------------------------------------------------------- pages
def overview(request):
    run = _q(dd.latest_run)
    if not run:
        return _needs_run(request, "overview")
    run_id = run["run_id"]
    o = _q(dd.overview, run_id)
    counts = _q(dd.status_counts, run_id)
    types = sorted({c["record_type"] for c in counts})
    statuses = sorted({c["status"] for c in counts})
    cell = {(c["status"], c["record_type"]): c["n"] for c in counts}
    pivot = [{"status": s, "cells": [cell.get((s, t), 0) for t in types]} for s in statuses]
    ev = o["evaluation"] or {}
    return render(request, "webapp/overview.html", {
        "o": o, "ev": ev,
        "ev_ok": bool(ev) and ev["scenarios_passed"] == ev["scenarios_total"] and not ev["false_positive_exceptions"],
        "types": types, "pivot": pivot, "freshness": _q(dd.source_freshness),
        "exceptions": _q(dd.results, run_id, exceptions_only=True),
        "board": _q(dd.scenario_scoreboard, run_id, _catalog()),
    })


def reconciliation(request):
    run = _q(dd.latest_run)
    if not run:
        return _needs_run(request, "reconciliation")
    rows = _q(dd.results, run["run_id"])
    g = request.GET
    f = {"exceptions": g.get("exceptions", "1"), "client": g.get("client", ""), "location": g.get("location", ""),
         "status": g.get("status", ""), "record_type": g.get("record_type", ""),
         "date_from": g.get("date_from", ""), "date_to": g.get("date_to", "")}
    view = rows
    if f["exceptions"] == "1":
        view = [r for r in view if r["is_exception"]]
    for key, col in (("client", "client"), ("location", "location"), ("status", "status"),
                     ("record_type", "record_type")):
        if f[key]:
            view = [r for r in view if (r[col] or "") == f[key]]
    for key, op in (("date_from", lambda d, v: d >= v), ("date_to", lambda d, v: d <= v)):
        if f[key]:
            try:
                bound = date.fromisoformat(f[key]).isoformat()
            except ValueError:
                continue
            view = [r for r in view if r["record_date"] and op(r["record_date"], bound)]
    return render(request, "webapp/reconciliation.html", {
        "rows": view, "total": len(rows), "f": f,
        "clients": sorted({r["client"] for r in rows if r["client"]}),
        "locations": sorted({r["location"] for r in rows if r["location"]}),
        "statuses": sorted({r["status"] for r in rows}),
        "record_types": sorted({r["record_type"] for r in rows}),
        "status_help": dd.STATUS_HELP, "cross": _q(dd.cross_system),
    })


def record(request, record_type: str, record_id: str):
    if record_type not in RECORD_TYPES:
        raise Http404("unknown record type")
    run = _q(dd.latest_run)
    if not run:
        return _needs_run(request, "record")
    d = _q(dd.record_detail, run["run_id"], record_type, record_id)
    if not d["result"]:
        raise Http404("record not found")
    src = d["source_row"] or {}
    fields = [("Internal record ID", d["result"]["record_id"]), ("Source system", d["result"]["source"]),
              ("Source record ID", d["result"]["source_record_id"]),
              ("Counterpart ID(s)", d["result"]["counterpart_source_ids"] or "—"),
              ("Seed / scenario key", d["result"]["scenario_id"] or "—"),
              ("Ingestion run", src.get("run_id")), ("Ingested at", src.get("ingested_at"))]
    if "referenced_stripe_id" in src:
        fields += [("Stripe ID in QBO memo", src.get("referenced_stripe_id") or "— (none)"),
                   ("QBO payment method", f"{src.get('payment_method_name')} ({src.get('payment_method_type')})")]
    if "qbo_invoice_ref" in src:
        fields.append(("Invoice named in Stripe metadata", src.get("qbo_invoice_ref") or "—"))
    journal = []
    if record_type == "stripe_payment":
        journal = _q(journal_preview, d["result"]["source_record_id"], run["run_id"])
    return render(request, "webapp/record.html", {
        "r": d["result"], "help": dd.STATUS_HELP.get(d["result"]["status"], ""), "fields": fields,
        "allocations": d["allocations"], "raw_pages": d["raw_pages"], "source_row": src,
        "source_json": json.dumps(src, indent=2, default=str), "journal": journal,
    })


def billing(request):
    run = _q(dd.latest_run)
    if not run:
        return _needs_run(request, "billing")
    show_all = request.GET.get("all") == "1"
    rows = _q(dd.billing, run["run_id"])
    if not show_all:
        rows = [r for r in rows if r["status"] != "BILLING_MATCHED"]
    return render(request, "webapp/billing.html", {
        "rules": _q(dd.billing_rules), "rows": rows, "show_all": show_all,
        "payouts": _q(dd.payouts), "accounting": _q(dd.accounting_summary, run["run_id"]),
    })


def data_quality(request):
    run = _q(dd.latest_run)
    if not run:
        return _needs_run(request, "data_quality")
    history = _q(dd.ingestion_history)
    runs = sorted({h["run_id"] for h in history}, reverse=True)
    picked = request.GET.get("run") if request.GET.get("run") in runs else (runs[0] if runs else None)
    findings = _q(dd.quality_findings, run["run_id"])
    evals = _q(dd.evaluation_rows, run["run_id"])
    return render(request, "webapp/data_quality.html", {
        "status": _q(dd.data_status), "runs": runs, "picked": picked,
        "history": [h for h in history if h["run_id"] == picked],
        "failed": [x for x in findings if not x["passed"]], "findings": findings,
        "board": _q(dd.scenario_scoreboard, run["run_id"], _catalog()),
        "evals_total": len(evals), "evals_bad": [e for e in evals if e["outcome"] != "correct"],
    })


def retail(request):
    s = _q(dd.retail_summary)
    chart = None
    if s and s["monthly"]:
        chart = _monthly_chart(s["monthly"])
    links = {}
    if s:
        for r in s["cancellation_links"]:
            links.setdefault(r["month"], {"LINKED": 0, "NO_PRIOR_SALE": 0, "MISSING_CUSTOMER": 0})[r["status"]] = r["lines"]
    return render(request, "webapp/retail.html", {
        "s": s, "citation": CITATION, "chart": chart,
        "links": [{"month": m, **v} for m, v in sorted(links.items())],
        "load_seconds": sum((s["load"].get(k) or 0) for k in ("seconds_read_parse", "seconds_load", "seconds_validate"))
        if s else None,
    })


def _monthly_chart(monthly: list[dict], width: int = 900, height: int = 220, pad: int = 36) -> dict:
    """Server-side SVG line chart: monthly valid sales vs cancellations (GBP)."""
    xs = [m["month"] for m in monthly]
    sales = [(m["sales_cents"] or 0) / 100 for m in monthly]
    canc = [abs(m["cancellations_cents"] or 0) / 100 for m in monthly]
    top = max(sales + canc + [1])
    n = max(len(xs) - 1, 1)

    def pts(values):
        return " ".join(f"{pad + i * (width - 2 * pad) / n:.1f},{height - pad - v / top * (height - 2 * pad):.1f}"
                        for i, v in enumerate(values))

    ticks = [{"x": pad + i * (width - 2 * pad) / n, "label": x} for i, x in enumerate(xs) if i % 3 == 0]
    return {"w": width, "h": height, "pad": pad, "sales": pts(sales), "canc": pts(canc), "ticks": ticks,
            "top": f"£{top:,.0f}", "baseline": height - pad}


def architecture(request):
    settings = get_settings()
    return render(request, "webapp/architecture.html", {
        "tol": settings.candidate_date_tolerance_days, "window": settings.sync_delay_days,
    })


def healthz(request):
    try:
        _q(lambda db: db.scalar("SELECT 1"))
    except Exception as exc:
        return JsonResponse({"status": "error", "detail": type(exc).__name__}, status=503)
    return HttpResponse("ok", content_type="text/plain")
