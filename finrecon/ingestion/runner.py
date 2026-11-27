"""Ingestion runner: pull every source, validate, and load idempotently.

Failure rules (the same for every source):
  1. Record the failure in ingestion_runs (with retries and error details).
  2. Retry 429/5xx with bounded exponential backoff (finrecon.http).
  3. Never overwrite the last validated dataset with a failed or partial pull.
  4. Mark data NO_DATA (never received), INCOMPLETE (partial pull) or STALE
     (latest pull failed / too old; last validated data retained).
  5. Keep last_successful_sync so the dashboard can show it.
A failed pull is never stored as zero.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import money
from ..adapters.fixtures import QboFixtureTransport, StripeFixtureTransport
from ..adapters.mock_ad import MockAdClient
from ..adapters.qbo import QboClient
from ..adapters.stripe_api import StripeClient
from ..config import ROOT, Settings
from ..db import Database
from ..http import ApiError, CallStats, InProcessTransport, RequestsTransport, RetryPolicy
from . import normalize as N
from .raw import archive


@dataclass
class Sources:
    qbo: QboClient
    stripe: StripeClient
    ad: MockAdClient
    registry: dict


def build_sources(settings: Settings, as_of: date, run_no: int,
                  sleep: Callable[[float], None] | None = None) -> Sources:
    import time

    policy = RetryPolicy(sleep=sleep or time.sleep)
    if settings.mode == "offline":
        fx = settings.fixture_dir
        from mock_ad_api.service import MockAdService

        ad_service = MockAdService.from_dir(fx / "ad_platform", run_no=run_no)
        return Sources(
            qbo=QboClient(QboFixtureTransport(fx, as_of), realm_id="offline-sandbox", page_size=20, policy=policy),
            stripe=StripeClient(StripeFixtureTransport(fx, as_of), page_size=25, policy=policy),
            # Small page size on purpose: exercises multi-page pulls on a small dataset.
            ad=MockAdClient(InProcessTransport(ad_service.handle), page_size=10, policy=policy),
            registry=json.loads((fx / "registry.json").read_text()),
        )
    from ..adapters import qbo as qbo_mod
    from ..adapters import stripe_api

    registry_path = ROOT / "data" / "registry.json"
    if not registry_path.exists():
        raise FileNotFoundError("data/registry.json not found: run `python -m seed.live_seed` first")
    if settings.mock_ad_api_url:
        ad = MockAdClient(RequestsTransport(settings.mock_ad_api_url), page_size=10, policy=policy,
                          headers={"X-Demo-Run": str(run_no)})
    else:
        from mock_ad_api.service import MockAdService

        ad = MockAdClient(InProcessTransport(
            MockAdService.from_dir(settings.fixture_dir / "ad_platform", run_no=run_no).handle),
            page_size=10, policy=policy)
    q = qbo_mod.live_client(settings)
    s = stripe_api.live_client(settings)
    q.policy = s.policy = policy
    return Sources(q, s, ad, json.loads(registry_path.read_text()))


def run_id_for(as_of: date, run_no: int) -> str:
    return f"run-{run_no:02d}-{as_of.isoformat()}"


class IngestionRunner:
    def __init__(self, db: Database, settings: Settings, sources: Sources, run_no: int, as_of: date,
                 now: str | None = None):
        self.db = db
        self.s = settings
        self.src = sources
        self.run_no = run_no
        self.as_of = as_of
        self.run_id = run_id_for(as_of, run_no)
        # Offline runs use a simulated clock (06:00 UTC on the as-of date) so the
        # snapshot is reproducible; live runs use the real time.
        self.now = now or (f"{as_of.isoformat()}T06:00:00+00:00" if settings.mode == "offline"
                           else datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        self.ids = N.IdMap()
        self.summary: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _dq(self, source: str, scope: str, check: str, passed: bool, severity: str, detail: str = "") -> None:
        self.db.upsert("data_quality_results", {"run_id": self.run_id, "source": source, "scope": scope,
                                                "check_name": check, "passed": passed,
                                                "severity": severity if not passed else "info",
                                                "detail": detail[:2000]}, ["run_id", "source", "scope", "check_name"])

    def _issues(self, source: str, scope: str, issues: list[N.Issue], checks: list[str]) -> None:
        by_check: dict[str, list[N.Issue]] = {}
        for i in issues:
            by_check.setdefault(i.check, []).append(i)
        for check in sorted(set(checks) | set(by_check)):
            found = by_check.get(check, [])
            sev = "error" if any(i.severity == "error" for i in found) else "warning"
            detail = "; ".join(i.detail for i in found[:10]) + (f" (+{len(found) - 10} more)" if len(found) > 10 else "")
            self._dq(source, scope, check, not found, sev, detail or "ok")

    def _checkpoint(self, source: str, scope: str) -> dict | None:
        rows = self.db.query("SELECT * FROM ingestion_checkpoints WHERE source = ? AND scope = ?", (source, scope))
        return rows[0] if rows else None

    def _record_run(self, source, scope, status, started, *, client=None, location=None, window=(None, None),
                    expected=None, received=None, stats: CallStats | None = None, error=None, last_ok=None) -> None:
        self.db.upsert("ingestion_runs", {
            "run_id": self.run_id, "source": source, "scope": scope, "client_id": client, "location_id": location,
            "window_start": window[0], "window_end": window[1], "started_at": started, "finished_at": self.now,
            "status": status, "records_expected": expected, "records_received": received,
            "retry_count": stats.retries if stats else 0, "error_details": error,
            "last_successful_sync": last_ok}, ["run_id", "source", "scope"])

    def _set_status(self, source, scope, status, *, client=None, location=None, detail="", checkpoint=None) -> None:
        self.db.upsert("data_status", {
            "source": source, "scope": scope, "client_id": client, "location_id": location, "status": status,
            "run_id": self.run_id, "as_of": self.as_of.isoformat(),
            "last_successful_sync": checkpoint["last_successful_sync"] if checkpoint else None,
            "validated_through": checkpoint["validated_through"] if checkpoint else None,
            "detail": detail}, ["source", "scope"])

    def _mark_success(self, source, scope, through: date) -> dict:
        cp = {"source": source, "scope": scope, "last_successful_run_id": self.run_id,
              "last_successful_sync": self.now, "validated_through": through.isoformat()}
        self.db.upsert("ingestion_checkpoints", cp, ["source", "scope"])
        return cp

    def _fail(self, source, scope, started, exc: Exception, stats, *, client=None, location=None, window=(None, None)):
        cp = self._checkpoint(source, scope)
        status = "STALE" if cp else "NO_DATA"
        detail = (f"Latest pull failed ({exc}). Showing last validated data through {cp['validated_through']}."
                  if cp else f"No data has ever been received ({exc}).")
        self._record_run(source, scope, "failed", started, client=client, location=location, window=window,
                         stats=stats, error=str(exc)[:1000], last_ok=cp["last_successful_sync"] if cp else None)
        self._dq(source, scope, "api_reachable", False, "error", str(exc)[:500])
        self._set_status(source, scope, status, client=client, location=location, detail=detail, checkpoint=cp)
        self.db.commit()
        return status

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        self.db.upsert("pipeline_runs", {"run_id": self.run_id, "run_no": self.run_no, "mode": self.s.mode,
                                         "as_of": self.as_of.isoformat(), "started_at": self.now,
                                         "finished_at": None, "status": "running"}, ["run_id"])
        self.load_registry()
        self.ingest_qbo()
        self.ingest_stripe()
        self.ingest_ad_platform()
        self.apply_age_staleness()
        self.db.execute("UPDATE pipeline_runs SET finished_at = ?, status = ? WHERE run_id = ?",
                        (self.now, "completed", self.run_id))
        self.db.commit()
        return self.summary

    def load_registry(self) -> None:
        clients, locations, mappings, rules = N.registry_rows(self.src.registry, self.now)
        with self.db.transaction():
            self.db.upsert_many("clients", clients, ["client_id"])
            self.db.upsert_many("locations", locations, ["location_id"])
            self.db.upsert_many("source_id_map", mappings, ["source", "entity_type", "external_id"])
            self.db.upsert_many("billing_rules", rules, ["client_id"])
        self.ids.location_client = {l["location_id"]: l["client_id"] for l in locations}
        for m in mappings:
            self.ids.entries[(m["source"], m["entity_type"], m["external_id"])] = (m["internal_type"], m["internal_id"])

    # ------------------------------------------------------------------ QuickBooks
    def ingest_qbo(self) -> None:
        source, scope, started = "qbo", "company", self.now
        client = self.src.qbo
        client.stats = CallStats()
        try:
            fetched, pages = {}, []
            for entity in ("Customer", "PaymentMethod", "Invoice", "Payment", "RefundReceipt"):
                rows, p = client.query_all(entity)
                fetched[entity] = rows
                pages += p
        except ApiError as exc:
            self.summary[source] = {"status": self._fail(source, scope, started, exc, client.stats)}
            return
        archive(self.db, self.s.raw_dir, self.run_id, source, pages, self.now)

        issues: list[N.Issue] = []
        expected_customers = {e[2] for e in self.ids.entries if e[0] == "qbo"}
        got_customers = {c["Id"] for c in fetched["Customer"]}
        for missing in sorted(expected_customers - got_customers):
            issues.append(N.Issue("expected_vs_received", f"QBO customer {missing} expected but not returned"))
        methods = {m["Id"]: m for m in fetched["PaymentMethod"]}

        invoices, payments, refunds, allocs = [], [], [], []
        for obj in fetched["Invoice"]:
            try:
                row, iss = N.qbo_invoice(obj, self.ids, self.run_id, self.now)
                invoices.append(row)
                issues += iss
            except money.MoneyError as exc:
                issues.append(N.Issue("currency", f"Invoice {obj.get('Id')}: {exc}"))
        invoice_ids = {r["source_invoice_id"]: r["invoice_id"] for r in invoices}
        for obj in fetched["Payment"]:
            try:
                row, allocations, iss = N.qbo_payment(obj, self.ids, methods, self.run_id, self.now)
            except money.MoneyError as exc:
                issues.append(N.Issue("currency", f"Payment {obj.get('Id')}: {exc}"))
                continue
            payments.append(row)
            issues += iss
            for qbo_invoice_id, cents in allocations:
                if qbo_invoice_id not in invoice_ids:
                    issues.append(N.Issue("referential_integrity",
                                          f"Payment {obj['Id']} applied to unknown invoice {qbo_invoice_id}"))
                    continue
                allocs.append({"qbo_payment_id": row["payment_id"], "invoice_id": invoice_ids[qbo_invoice_id],
                               "amount_cents": cents})
        for obj in fetched["RefundReceipt"]:
            try:
                row, iss = N.qbo_refund(obj, self.ids, self.run_id, self.now)
                refunds.append(row)
                issues += iss
            except money.MoneyError as exc:
                issues.append(N.Issue("currency", f"RefundReceipt {obj.get('Id')}: {exc}"))

        for label, objs in (("Invoice", fetched["Invoice"]), ("Payment", fetched["Payment"])):
            ids = [o.get("Id") for o in objs]
            if len(ids) != len(set(ids)):
                issues.append(N.Issue("duplicate_external_ids", f"{label}: duplicate Ids returned"))
        applied: dict[str, int] = {}
        for a in allocs:
            applied[a["invoice_id"]] = applied.get(a["invoice_id"], 0) + a["amount_cents"]
        for inv in invoices:
            if inv["total_cents"] - applied.get(inv["invoice_id"], 0) != inv["balance_cents"]:
                issues.append(N.Issue("invoice_balance_consistency",
                                      f"Invoice {inv['doc_number']}: Balance != TotalAmt - applied payments", "warning"))

        with self.db.transaction():
            self.db.upsert_many("invoices", invoices, ["source", "source_invoice_id"])
            self.db.upsert_many("qbo_payments", payments, ["source_payment_id"])
            for p in payments:   # allocations are replaced per payment (payments can be edited)
                self.db.execute("DELETE FROM payment_allocations WHERE qbo_payment_id = ?", (p["payment_id"],))
            self.db.upsert_many("payment_allocations", allocs, ["qbo_payment_id", "invoice_id"])
            self.db.upsert_many("qbo_refunds", refunds, ["source_refund_id"])
            self._issues(source, scope, issues, ["required_fields", "unmapped_external_id", "expected_vs_received",
                                                 "allocation_totals", "referential_integrity", "currency",
                                                 "duplicate_external_ids", "invoice_balance_consistency"])
            self._dq(source, scope, "api_reachable", True, "info", f"{client.stats.requests} requests")
            cp = self._mark_success(source, scope, self.as_of)
            n = len(invoices) + len(payments) + len(refunds)
            self._record_run(source, scope, "succeeded", started, expected=None, received=n, stats=client.stats,
                             last_ok=self.now)
            self._set_status(source, scope, "VALIDATED" if n else "ZERO_TRANSACTIONS", checkpoint=cp,
                             detail=f"{_n(len(invoices), 'invoice')}, {_n(len(payments), 'payment')}, "
                                    f"{_n(len(refunds), 'refund')}")
        self.summary[source] = {"status": "succeeded", "records": n, "requests": client.stats.requests}

    # ------------------------------------------------------------------ Stripe
    def ingest_stripe(self) -> None:
        source, scope, started = "stripe", "account", self.now
        client = self.src.stripe
        client.stats = CallStats()
        try:
            fetched, pages = {}, []
            for resource in ("customers", "payment_intents", "charges", "refunds", "balance_transactions", "payouts"):
                rows, p = client.list_all(resource)
                fetched[resource] = rows
                pages += p
            payout_of: dict[str, str] = {}
            for po in fetched["payouts"]:
                rows, p = client.list_all("balance_transactions", {"payout": po["id"]})
                pages += p
                for bt in rows:
                    payout_of[bt["id"]] = po["id"]
        except ApiError as exc:
            self.summary[source] = {"status": self._fail(source, scope, started, exc, client.stats)}
            return
        archive(self.db, self.s.raw_dir, self.run_id, source, pages, self.now)

        issues: list[N.Issue] = []
        expected = {e[2] for e in self.ids.entries if e[0] == "stripe" and e[1] == "customer"}
        for missing in sorted(expected - {c["id"] for c in fetched["customers"]}):
            issues.append(N.Issue("expected_vs_received", f"Stripe customer {missing} expected but not returned"))
        charges = {c["id"]: c for c in fetched["charges"]}
        payments, skipped = [], 0
        for pi in fetched["payment_intents"]:
            if pi.get("status") != "succeeded":
                skipped += 1
                continue
            try:
                row, iss = N.stripe_payment(pi, charges, self.ids, self.run_id, self.now)
                payments.append(row)
                issues += iss
            except money.MoneyError as exc:
                issues.append(N.Issue("currency", f"{pi.get('id')}: {exc}"))
        by_pi = {p["source_payment_id"]: p for p in payments}
        for r in self.db.query("SELECT source_payment_id, client_id, location_id FROM stripe_payments"):
            by_pi.setdefault(r["source_payment_id"], r)
        refunds = []
        for obj in fetched["refunds"]:
            row, iss = N.stripe_refund(obj, by_pi, self.run_id, self.now)
            refunds.append(row)
            issues += iss
        bts = [N.stripe_balance_transaction(b, payout_of, self.run_id, self.now) for b in fetched["balance_transactions"]]
        payouts = [N.stripe_payout(p, self.run_id, self.now) for p in fetched["payouts"]]
        for b in bts:
            if b["amount_cents"] - b["fee_cents"] != b["net_cents"]:
                issues.append(N.Issue("invalid_value", f"{b['source_bt_id']}: amount - fee != net"))
        ids = [o["id"] for o in fetched["payment_intents"]]
        if len(ids) != len(set(ids)):
            issues.append(N.Issue("duplicate_external_ids", "payment_intents: duplicate ids across pages"))

        with self.db.transaction():
            self.db.upsert_many("stripe_payments", payments, ["source_payment_id"])
            self.db.upsert_many("stripe_refunds", refunds, ["source_refund_id"])
            self.db.upsert_many("stripe_balance_transactions", bts, ["source_bt_id"])
            self.db.upsert_many("stripe_payouts", payouts, ["source_payout_id"])
            self._issues(source, scope, issues, ["required_fields", "unmapped_external_id", "expected_vs_received",
                                                 "referential_integrity", "currency", "invalid_value",
                                                 "duplicate_external_ids"])
            self._dq(source, scope, "api_reachable", True, "info", f"{client.stats.requests} requests")
            if skipped:
                self._dq(source, scope, "non_succeeded_payment_intents", True, "info",
                         f"{skipped} PaymentIntents not in status 'succeeded' were skipped")
            cp = self._mark_success(source, scope, self.as_of)
            n = len(payments) + len(refunds)
            self._record_run(source, scope, "succeeded", started, received=n, stats=client.stats, last_ok=self.now)
            self._set_status(source, scope, "VALIDATED" if n else "ZERO_TRANSACTIONS", checkpoint=cp,
                             detail=f"{_n(len(payments), 'payment')}, {_n(len(refunds), 'refund')}, "
                                    f"{_n(len(payouts), 'payout')}")
        self.summary[source] = {"status": "succeeded", "records": n, "requests": client.stats.requests}

    # ------------------------------------------------------------------ ad platform
    def ingest_ad_platform(self) -> None:
        source = "ad_platform"
        results = {}
        for c in self.src.registry["clients"]:
            cid = self.ids.entries[("ad_platform", "client", c["external"]["ad_client_id"])][1]
            listing_stats = CallStats()
            try:
                listed = {l["id"] for l in self.src.ad.list_locations(c["external"]["ad_client_id"], listing_stats)}
                listing_error = None
            except ApiError as exc:
                listed, listing_error = set(), exc
            expected = {l["external"]["ad_location_id"] for l in c["locations"]}
            missing = expected - listed
            self._dq(source, c["client_key"], "expected_vs_received", not missing and listing_error is None, "error",
                     f"locations not reported: {sorted(missing)}" if missing else "all expected locations listed")
            for loc in c["locations"]:
                scope = f"{c['client_key']}/{loc['location_key']}"
                lid = self.ids.entries[("ad_platform", "location", loc["external"]["ad_location_id"])][1]
                results[scope] = self._ingest_location(c, loc, cid, lid, scope,
                                                       listing_error or (ApiError(404, "location not listed", 1)
                                                                         if loc["external"]["ad_location_id"] in missing else None))
        self.summary[source] = results

    def _ingest_location(self, c: dict, loc: dict, cid: str, lid: str, scope: str, pre_error) -> str:
        source, started = "ad_platform", self.now
        cp = self._checkpoint(source, scope)
        start = self.s.window_start
        if cp:
            start = max(self.s.window_start,
                        date.fromisoformat(cp["validated_through"]) - timedelta(days=self.s.incremental_lookback_days))
        window = (start.isoformat(), self.as_of.isoformat())
        stats = CallStats()
        if pre_error:
            return self._fail(source, scope, started, pre_error, stats, client=cid, location=lid, window=window)
        try:
            ex = self.src.ad.extract_spend(c["external"]["ad_client_id"], loc["external"]["ad_location_id"],
                                           window[0], window[1], stats)
        except ApiError as exc:
            return self._fail(source, scope, started, exc, stats, client=cid, location=lid, window=window)
        archive(self.db, self.s.raw_dir, self.run_id, source, ex.pages, self.now)

        issues: list[N.Issue] = []
        unique: dict[str, dict] = {}
        for r in ex.rows:
            unique.setdefault(r["id"], r)
        dupes = len(ex.rows) - len(unique)
        if dupes:
            issues.append(N.Issue("duplicate_external_ids",
                                  f"{dupes} repeated row(s) across pages; de-duplicated by source id", "warning"))
        if ex.pages_received != ex.total_pages_reported or len(unique) != ex.total_records_reported:
            issues.append(N.Issue("pagination_completeness",
                                  f"received {len(unique)} of {ex.total_records_reported} records "
                                  f"over {ex.pages_received}/{ex.total_pages_reported} pages"))
        rows = []
        for r in unique.values():
            try:
                row, iss = N.ad_spend(r, cid, lid, self.run_id, self.now)
            except money.MoneyError as exc:
                issues.append(N.Issue("currency", f"{r.get('id')}: {exc}"))
                continue
            issues += iss
            if row["amount_cents"] is not None:
                rows.append(row)
        detail_total = sum(r["amount_cents"] for r in rows)
        if detail_total != ex.summary_total_cents or len(unique) != ex.summary_record_count:
            issues.append(N.Issue("source_totals",
                                  f"detail {money.fmt(detail_total)} / {len(unique)} rows vs source summary "
                                  f"{money.fmt(ex.summary_total_cents)} / {ex.summary_record_count} rows"))
        self._issues(source, scope, issues, ["duplicate_external_ids", "pagination_completeness", "source_totals",
                                             "required_fields", "invalid_value", "currency"])
        self._dq(source, scope, "api_reachable", True, "info",
                 f"{_n(stats.requests, 'request')}, {_n(stats.retries, 'retry', 'retries')}")

        blocking = [i for i in issues if i.severity == "error"]
        if blocking:
            detail = "; ".join(i.detail for i in blocking)
            if cp:
                detail += f". Not loaded; last validated data through {cp['validated_through']} retained."
            else:
                detail += ". Not loaded; no validated data exists yet."
            self._record_run(source, scope, "incomplete", started, client=cid, location=lid, window=window,
                             expected=ex.total_records_reported, received=len(unique), stats=stats,
                             error=detail[:1000], last_ok=cp["last_successful_sync"] if cp else None)
            self._set_status(source, scope, "INCOMPLETE", client=cid, location=lid, detail=detail, checkpoint=cp)
            self.db.commit()
            return "INCOMPLETE"

        with self.db.transaction():
            self.db.upsert_many("ad_spend", rows, ["source_spend_id"])
            cp = self._mark_success(source, scope, self.as_of)
            self._record_run(source, scope, "succeeded", started, client=cid, location=lid, window=window,
                             expected=ex.total_records_reported, received=len(unique), stats=stats, last_ok=self.now)
            total_rows = self.db.scalar("SELECT COUNT(*) FROM ad_spend WHERE location_id = ?", (lid,))
            status = "VALIDATED" if total_rows else "ZERO_TRANSACTIONS"
            detail = (f"{_n(len(unique), 'row')} in window, {_n(stats.retries, 'retry', 'retries')}" if total_rows
                      else "Complete extract with no activity in the window (genuine zero).")
            self._set_status(source, scope, status, client=cid, location=lid, detail=detail, checkpoint=cp)
        return status

    def apply_age_staleness(self) -> None:
        """Validated data whose last successful sync is too old becomes STALE."""
        limit = datetime.fromisoformat(self.now) - timedelta(days=self.s.stale_after_days)
        for r in self.db.query("SELECT * FROM data_status WHERE status IN ('VALIDATED','ZERO_TRANSACTIONS')"):
            if r["last_successful_sync"] and datetime.fromisoformat(r["last_successful_sync"]) < limit:
                self.db.execute("UPDATE data_status SET status = 'STALE', detail = ? WHERE source = ? AND scope = ?",
                                (f"Last successful sync {r['last_successful_sync']} is older than "
                                 f"{self.s.stale_after_days} days.", r["source"], r["scope"]))
        self.db.commit()


def _n(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def raw_dir_default() -> Path:
    return ROOT / "data" / "raw"
