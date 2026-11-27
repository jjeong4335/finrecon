"""Page 4 — Architecture and methodology."""
import streamlit as st

from common import setup
from finrecon.config import get_settings

db, run = setup("Architecture & Methodology")
settings = get_settings()

st.graphviz_chart("""
digraph G {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f4f6fa", color="#9aa6b8", fontname="Helvetica", fontsize=11];
  edge [color="#7b8799", fontname="Helvetica", fontsize=9];
  subgraph cluster_src { label="Sources (sandbox / simulated)"; color="#d8dee8"; fontname="Helvetica";
    qbo [label="QuickBooks Online\\nsandbox API\\nCustomers · Invoices · Payments · Refunds"];
    stripe [label="Stripe sandbox API\\nPaymentIntents · Charges · Refunds\\nBalance txns · Payouts"];
    ad [label="Mock ad platform API\\n(FastAPI)\\nlocations · daily spend · summary"]; }
  ingest [label="Ingestion runner\\nOAuth · pagination · retry/backoff\\nexpected-vs-received checks"];
  raw [label="Raw archive\\n(run_id, sha256)"];
  core [label="PostgreSQL core\\ninternal IDs + source_id_map\\nidempotent upserts", fillcolor="#e9effc"];
  dq [label="Data status\\nVALIDATED · ZERO · NO_DATA\\nINCOMPLETE · STALE"];
  recon [label="Reconciliation\\nL1 exact ID → L2 candidate → L3 typed exceptions\\nbilling · gross→net · payouts", fillcolor="#e9effc"];
  eval [label="Ground-truth evaluator\\n(scenario plan, not engine output)"];
  ui [label="Streamlit dashboard\\n(read-only DB role)", fillcolor="#e6f4ec"];
  qbo -> ingest; stripe -> ingest; ad -> ingest;
  ingest -> raw [label="every page"]; ingest -> core [label="validated only"]; ingest -> dq;
  core -> recon; dq -> recon; recon -> eval; recon -> ui; eval -> ui; dq -> ui;
}
""")

st.subheader("Key design decisions")
st.markdown("""
- **Internal IDs, explicit mappings.** Clients and locations have internal UUIDs. `source_id_map` records which
  QuickBooks customer (a sub-customer per location), Stripe customer (one per client) and ad-platform location
  each one corresponds to. QuickBooks `DepartmentRef` is not used as a location key.
- **Two records of the same money.** A Stripe payment is the processing record; a QBO payment is the accounting
  record. They are matched, never added together. Allocations (`Line.LinkedTxn`) decide which invoice is paid, so
  a payment covering two invoices is counted once per invoice at its allocated amount.
- **Matching order.** Level 1: the Stripe PaymentIntent id the sync writes into the QBO memo; if several QBO records
  cite it, the one whose amount agrees is the match. Level 2: same client, currency and amount within ±{tol} days,
  accepted only if the pair is mutually unique and nothing contradicts it (location, invoice reference).
  Otherwise `NEEDS_REVIEW`.
- **Time is a parameter.** Sync windows ({window} days), aging and staleness use `RECON_AS_OF`
  ({as_of}), not the clock, so a deployed demo never changes its answers.
- **Failures are data.** A failed or partial pull is recorded, retried with bounded backoff, never loaded over the
  last validated data, and surfaced as `NO_DATA` / `INCOMPLETE` / `STALE`, never as $0.
- **Money is integer cents** plus currency. Stripe already sends minor units; QuickBooks decimals are parsed with
  `Decimal` (never float) and converted in one tested function. Unknown currencies are rejected.
- **Ground truth is independent.** Expected statuses come from the scenario plan that generated the data, keyed by
  a `seed_key` carried in Stripe metadata and the QBO memo. The evaluator can fail, and tests prove it does.
""".format(tol=settings.candidate_date_tolerance_days, window=settings.sync_delay_days, as_of=settings.recon_as_of))

st.subheader("Interview questions → where they are answered")
st.table({
    "Question": ["Pull reports via API for many clients × locations", "Verify completeness and accuracy",
                 "Relate client/location/invoice/transaction/payment IDs", "Find transactions in one system only",
                 "Explain the pipeline architecture", "Connect QuickBooks, Stripe and the ad platform"],
    "Answer in this project": ["ingestion/runner.py, adapters/*, Data Quality page",
                               "expected-vs-received, pagination, source totals, duplicates (Data Quality page)",
                               "db/schema.sql: source_id_map, payment_allocations",
                               "recon/sql/*.sql + engine.py (Reconciliation page)", "this page",
                               "Billing & Payouts page: spend → invoice → payment → payout"],
})

st.subheader("Limitations")
st.markdown("""
- Reconciliation data is synthetic. The billing formula, fees and the Stripe→QBO sync are simulated to create
  known cases. The separate retail module (page 5) uses real public data from UCI Online Retail II, which is never
  matched against QuickBooks or Stripe.
- Sandbox objects cannot be back-dated, so live seeding stores the business date in Stripe metadata (`event_date`).
- Single currency (USD). Multi-currency would need an explicit FX policy before any comparison.
- QBO and Stripe are fully re-read each run (small volumes); the ad platform loads incrementally with a look-back.
- Payout membership is simulated in offline mode.
""")
st.markdown(f"Code, tests and README: {settings.github_url}")
