"""Payment reconciliation engine.

Matching order
  Level 1  exact: Stripe PaymentIntent id recorded in the QBO payment memo
  Level 2  candidate: same client, currency, amount, dates within tolerance;
           accepted only when the pair is mutually unique, otherwise NEEDS_REVIEW
  Level 3  everything left is classified with a typed status and a reason

Semantic rules
  * QBO payments and Stripe payments are two records of the same money; they
    are compared, never added together.
  * An unpaid invoice is OPEN_AR, not an exception.
  * A Stripe payment not yet in QBO is PENDING_SYNC inside the sync window and
    MISSING_IN_QBO after it.
  * A QBO payment by check/ACH is NON_STRIPE_PAYMENT: no Stripe record expected.
  * Time rules use RECON_AS_OF, never the wall clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..config import Settings
from ..db import Database
from ..money import fmt

SQL_DIR = Path(__file__).parent / "sql"

EXCEPTION_STATUSES = {"MISSING_IN_QBO", "MISSING_IN_STRIPE", "AMOUNT_MISMATCH", "DUPLICATE",
                      "UNRECORDED_REFUND", "NEEDS_REVIEW"}


def sql(name: str) -> str:
    return (SQL_DIR / f"{name}.sql").read_text()


@dataclass
class Result:
    record_type: str
    record_id: str
    source: str
    source_record_id: str
    status: str
    reason: str
    amount_cents: int
    currency: str
    scenario_id: str | None = None
    client_id: str | None = None
    location_id: str | None = None
    match_method: str | None = None
    counterpart_source_ids: list[str] = field(default_factory=list)
    counterpart_amount_cents: int | None = None
    record_date: str | None = None

    def row(self, run_id: str) -> dict:
        diff = None if self.counterpart_amount_cents is None else self.amount_cents - self.counterpart_amount_cents
        return {"run_id": run_id, "record_type": self.record_type, "record_id": self.record_id,
                "source": self.source, "source_record_id": self.source_record_id, "scenario_id": self.scenario_id,
                "client_id": self.client_id, "location_id": self.location_id, "status": self.status,
                "is_exception": self.status in EXCEPTION_STATUSES, "match_method": self.match_method,
                "counterpart_source_ids": ",".join(self.counterpart_source_ids) or None,
                "amount_cents": self.amount_cents, "counterpart_amount_cents": self.counterpart_amount_cents,
                "diff_cents": diff, "currency": self.currency, "record_date": self.record_date,
                "reason": self.reason}


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def reconcile(db: Database, settings: Settings, run_id: str) -> dict[str, int]:
    as_of = settings.recon_as_of
    window = settings.sync_delay_days
    db.execute("DELETE FROM payment_matches WHERE run_id = ?", (run_id,))
    db.execute("DELETE FROM reconciliation_results WHERE run_id = ?", (run_id,))

    stripe = {r["payment_id"]: r for r in db.query("SELECT * FROM stripe_payments WHERE status = 'succeeded'")}
    qbo = {r["payment_id"]: r for r in db.query("SELECT * FROM qbo_payments")}
    results: dict[tuple[str, str], Result] = {}
    matches: list[dict] = []

    def put(r: Result) -> None:
        results[(r.record_type, r.record_id)] = r

    def sres(s: dict, status: str, reason: str, **kw) -> Result:
        return Result("stripe_payment", s["payment_id"], "stripe", s["source_payment_id"], status, reason,
                      s["amount_received_cents"], s["currency"], s["scenario_id"], s["client_id"], s["location_id"],
                      record_date=s["effective_date"], **kw)

    def qres(q: dict, status: str, reason: str, **kw) -> Result:
        return Result("qbo_payment", q["payment_id"], "qbo", q["source_payment_id"], status, reason,
                      q["total_cents"], q["currency"], q["scenario_id"], q["client_id"], q["location_id"],
                      record_date=q["txn_date"], **kw)

    # ---------------------------------------------------------------- Level 1
    for m in db.query(sql("level1_exact_matches")):
        s, q = stripe[m["stripe_payment_id"]], qbo[m["qbo_payment_id"]]
        if m["rn"] > 1:
            put(qres(q, "DUPLICATE",
                     f"Second QBO record of Stripe payment {s['source_payment_id']}; the first is QBO payment "
                     f"{_first_qbo(db, s['source_payment_id'])}. {fmt(q['unapplied_cents'])} sits unapplied.",
                     match_method="exact_id", counterpart_source_ids=[s["source_payment_id"]],
                     counterpart_amount_cents=s["amount_received_cents"]))
            continue
        if m["qbo_client_id"] != m["stripe_client_id"] or m["qbo_currency"] != m["stripe_currency"]:
            reason = "Memo names a Stripe payment that belongs to a different client or currency."
            put(qres(q, "NEEDS_REVIEW", reason, match_method="exact_id", counterpart_source_ids=[s["source_payment_id"]]))
            put(sres(s, "NEEDS_REVIEW", reason, match_method="exact_id", counterpart_source_ids=[q["source_payment_id"]]))
            continue
        diff = m["stripe_amount_cents"] - m["qbo_amount_cents"]
        matches.append({"run_id": run_id, "stripe_payment_id": s["payment_id"], "qbo_payment_id": q["payment_id"],
                        "method": "exact_id", "amount_diff_cents": diff})
        if diff == 0:
            put(sres(s, "MATCHED", f"Exact match: QBO payment {q['source_payment_id']} records this Stripe ID "
                                   f"for the same amount.", match_method="exact_id",
                     counterpart_source_ids=[q["source_payment_id"]], counterpart_amount_cents=q["total_cents"]))
            put(qres(q, "MATCHED", f"Exact match: memo references {s['source_payment_id']} with the same amount.",
                     match_method="exact_id", counterpart_source_ids=[s["source_payment_id"]],
                     counterpart_amount_cents=s["amount_received_cents"]))
        else:
            text = (f"Stripe collected {fmt(s['amount_received_cents'])} but QBO recorded {fmt(q['total_cents'])} "
                    f"(difference {fmt(abs(diff))}).")
            put(sres(s, "AMOUNT_MISMATCH", text, match_method="exact_id",
                     counterpart_source_ids=[q["source_payment_id"]], counterpart_amount_cents=q["total_cents"]))
            put(qres(q, "AMOUNT_MISMATCH", text, match_method="exact_id",
                     counterpart_source_ids=[s["source_payment_id"]], counterpart_amount_cents=s["amount_received_cents"]))
    db.upsert_many("payment_matches", matches, ["run_id", "stripe_payment_id", "qbo_payment_id"])

    for o in db.query(sql("orphan_references")):
        q = qbo[o["payment_id"]]
        put(qres(q, "MISSING_IN_STRIPE",
                 f"QBO card payment references {o['referenced_stripe_id']}, which does not exist in Stripe "
                 f"(e.g. a declined payment recorded as received). Cash may not have been collected."))

    # ---------------------------------------------------------------- Level 2
    pairs = [p for p in db.query(sql("level2_candidates"), (run_id,))
             if _days(p["qbo_date"], p["stripe_date"]) <= settings.candidate_date_tolerance_days
             and ("qbo_payment", p["qbo_payment_id"]) not in results]
    by_q: dict[str, list[dict]] = {}
    by_s: dict[str, list[dict]] = {}
    for p in pairs:
        by_q.setdefault(p["qbo_payment_id"], []).append(p)
        by_s.setdefault(p["stripe_payment_id"], []).append(p)
    for qid, cands in by_q.items():
        q = qbo[qid]
        unique = len(cands) == 1 and len(by_s[cands[0]["stripe_payment_id"]]) == 1
        if unique:
            s = stripe[cands[0]["stripe_payment_id"]]
            matches.append({"run_id": run_id, "stripe_payment_id": s["payment_id"], "qbo_payment_id": qid,
                            "method": "candidate", "amount_diff_cents": 0})
            db.upsert("payment_matches", matches[-1], ["run_id", "stripe_payment_id", "qbo_payment_id"])
            why = (f"Candidate match: no Stripe ID in the memo, but exactly one Stripe payment for this client has "
                   f"the same amount within {settings.candidate_date_tolerance_days} days "
                   f"({s['effective_date']} vs {q['txn_date']}).")
            put(sres(s, "MATCHED", why, match_method="candidate", counterpart_source_ids=[q["source_payment_id"]],
                     counterpart_amount_cents=q["total_cents"]))
            put(qres(q, "MATCHED", why, match_method="candidate", counterpart_source_ids=[s["source_payment_id"]],
                     counterpart_amount_cents=s["amount_received_cents"]))
    for qid, cands in by_q.items():
        if ("qbo_payment", qid) in results:
            continue
        q = qbo[qid]
        ids = sorted({c["stripe_source_id"] for c in cands} |
                     {c2["stripe_source_id"] for c in cands for c2 in by_s[c["stripe_payment_id"]]})
        why = (f"Ambiguous: {len(ids)} Stripe payments of {fmt(q['total_cents'])} for this client within "
               f"{settings.candidate_date_tolerance_days} days could be this QBO entry. A person must choose.")
        put(qres(q, "NEEDS_REVIEW", why, match_method="candidate", counterpart_source_ids=ids))
        for c in cands:
            for s_id in {c2["stripe_payment_id"] for c2 in by_s[c["stripe_payment_id"]]} | {c["stripe_payment_id"]}:
                s = stripe[s_id]
                put(sres(s, "NEEDS_REVIEW",
                         f"One of several identical Stripe payments that could match QBO payment "
                         f"{q['source_payment_id']}; not auto-matched.", match_method="candidate",
                         counterpart_source_ids=[q["source_payment_id"]]))

    # ---------------------------------------------------------------- Level 3
    for q in qbo.values():
        if ("qbo_payment", q["payment_id"]) in results:
            continue
        if q["payment_method_type"] != "CREDIT_CARD":
            put(qres(q, "NON_STRIPE_PAYMENT",
                     f"Paid by {q['payment_method_name'] or 'a non-card method'}"
                     f"{' (ref ' + q['payment_ref_num'] + ')' if q['payment_ref_num'] else ''}. "
                     f"No Stripe record is expected."))
        else:
            put(qres(q, "MISSING_IN_STRIPE",
                     "QBO records a card payment, but no Stripe payment references it or matches it "
                     f"(same client and amount within {settings.candidate_date_tolerance_days} days)."))
    for s in stripe.values():
        if ("stripe_payment", s["payment_id"]) in results:
            continue
        age = (as_of - date.fromisoformat(s["effective_date"])).days
        if age <= window:
            put(sres(s, "PENDING_SYNC", f"Paid {age} day(s) before {as_of}; still inside the {window}-day sync "
                                        f"window, so not yet expected in QBO."))
        else:
            hint = f" Stripe metadata says it paid invoice {s['qbo_invoice_ref']}." if s["qbo_invoice_ref"] else ""
            put(sres(s, "MISSING_IN_QBO", f"Stripe collected {fmt(s['amount_received_cents'])} {age} days ago, "
                                          f"but no QBO payment records it.{hint} Cash received but AR not reduced."))

    _invoices(db, settings, put)
    _refunds(db, settings, put)

    rows = [r.row(run_id) for r in results.values()]
    db.upsert_many("reconciliation_results", rows, ["run_id", "record_type", "record_id"])
    db.commit()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _first_qbo(db: Database, stripe_id: str) -> str:
    rows = db.query("SELECT source_payment_id FROM qbo_payments WHERE referenced_stripe_id = ? "
                    "ORDER BY txn_date, CAST(source_payment_id AS BIGINT)", (stripe_id,))
    return rows[0]["source_payment_id"] if rows else "?"


def _invoices(db: Database, settings: Settings, put) -> None:
    as_of = settings.recon_as_of
    stripe_refs: dict[str, list[dict]] = {}
    for s in db.query("SELECT source_payment_id, qbo_invoice_ref FROM stripe_payments WHERE qbo_invoice_ref IS NOT NULL"):
        for doc in s["qbo_invoice_ref"].split(","):
            stripe_refs.setdefault(doc.strip(), []).append(s)
    for i in db.query(sql("invoice_status")):
        applied, total = i["applied_cents"], i["total_cents"]
        pays = i["payment_ids"] or ""
        if applied > total:
            status, reason = "NEEDS_REVIEW", f"Over-applied: {fmt(applied)} applied to a {fmt(total)} invoice ({pays})."
        elif applied == total:
            status, reason = "MATCHED", f"Fully paid: {fmt(applied)} applied from QBO payment(s) {pays}."
        else:
            due = date.fromisoformat(i["due_date"]) if i["due_date"] else None
            aging = ""
            if due and as_of > due:
                aging = f" {(as_of - due).days} days past due."
            elif due:
                aging = f" Due {due.isoformat()} (not yet due)."
            if applied > 0:
                status = "PARTIAL"
                reason = f"Partially paid: {fmt(applied)} of {fmt(total)} applied ({pays}); {fmt(total - applied)} open.{aging}"
            else:
                status = "OPEN_AR"
                reason = f"Unpaid invoice: {fmt(total)} open.{aging}"
            hints = [s["source_payment_id"] for s in stripe_refs.get(i["doc_number"] or "", [])
                     if not db.scalar("SELECT COUNT(*) FROM qbo_payments WHERE referenced_stripe_id = ?",
                                      (s["source_payment_id"],))]
            if hints:
                reason += f" Note: Stripe payment {', '.join(hints)} references this invoice but is not in QBO."
        if i["balance_cents"] != total - applied:
            reason += f" (QBO Balance {fmt(i['balance_cents'])} disagrees with applied payments.)"
        put(Result("invoice", i["invoice_id"], "qbo", i["source_invoice_id"], status, reason, total, i["currency"],
                   i["scenario_id"], i["client_id"], i["location_id"], match_method="allocation" if applied else None,
                   counterpart_source_ids=[p for p in pays.split(",") if p], counterpart_amount_cents=applied,
                   record_date=i["txn_date"]))


def _refunds(db: Database, settings: Settings, put) -> None:
    as_of = settings.recon_as_of
    qbo_by_ref = {}
    for r in db.query("SELECT * FROM qbo_refunds"):
        qbo_by_ref.setdefault(r["referenced_stripe_id"], []).append(r)
    seen_qbo: set[str] = set()
    for r in db.query("SELECT * FROM stripe_refunds WHERE status = 'succeeded'"):
        hits = qbo_by_ref.get(r["source_refund_id"], [])
        base = dict(record_type="stripe_refund", record_id=r["refund_id"], source="stripe",
                    source_record_id=r["source_refund_id"], amount_cents=r["amount_cents"], currency=r["currency"],
                    scenario_id=r["scenario_id"], client_id=r["client_id"], location_id=r["location_id"],
                    record_date=r["effective_date"])
        if hits:
            q = hits[0]
            seen_qbo.add(q["refund_id"])
            ok = q["total_cents"] == r["amount_cents"]
            status = "MATCHED" if ok else "AMOUNT_MISMATCH"
            why = (f"Refund recorded in QBO as refund receipt {q['source_refund_id']}." if ok else
                   f"Refund of {fmt(r['amount_cents'])} recorded in QBO as {fmt(q['total_cents'])}.")
            put(Result(status=status, reason=why, match_method="exact_id",
                       counterpart_source_ids=[q["source_refund_id"]], counterpart_amount_cents=q["total_cents"], **base))
            put(Result("qbo_refund", q["refund_id"], "qbo", q["source_refund_id"], status, why, q["total_cents"],
                       q["currency"], q["scenario_id"], q["client_id"], q["location_id"], match_method="exact_id",
                       counterpart_source_ids=[r["source_refund_id"]], counterpart_amount_cents=r["amount_cents"],
                       record_date=q["txn_date"]))
            continue
        age = (as_of - date.fromisoformat(r["effective_date"])).days
        if age <= settings.sync_delay_days:
            put(Result(status="PENDING_SYNC", reason=f"Refunded {age} day(s) ago; inside the sync window.", **base))
        else:
            put(Result(status="UNRECORDED_REFUND",
                       reason=f"Stripe refunded {fmt(r['amount_cents'])} on {r['effective_date']} (payment "
                              f"{r['source_payment_id']}), but QBO has no refund receipt. Revenue and cash are "
                              f"overstated in the books.", **base))
    for q in db.query("SELECT * FROM qbo_refunds"):
        if q["refund_id"] in seen_qbo:
            continue
        status = "MISSING_IN_STRIPE" if q["referenced_stripe_id"] else "NON_STRIPE_PAYMENT"
        why = (f"QBO refund references {q['referenced_stripe_id']}, which is not in Stripe." if q["referenced_stripe_id"]
               else "Refund issued outside Stripe (no Stripe reference).")
        put(Result("qbo_refund", q["refund_id"], "qbo", q["source_refund_id"], status, why, q["total_cents"],
                   q["currency"], q["scenario_id"], q["client_id"], q["location_id"], record_date=q["txn_date"]))
