"""Page 1 — Overview.   Run: streamlit run dashboard/Home.py"""
import pandas as pd
import streamlit as st

from common import dd, money_cols, scenario_catalog, setup, styled
from finrecon.money import fmt

db, run = setup("Finance Reconciliation Overview")
o = dd.overview(db, run["run_id"])
ev = o["evaluation"]

c = st.columns(4)
c[0].metric("Invoiced", fmt(o["invoiced_cents"]), help=f"{o['invoice_count']} QuickBooks invoices")
c[1].metric("Payments recorded in QBO", fmt(o["recorded_payments_cents"]),
            help=f"Stripe gross collected: {fmt(o['stripe_gross_cents'])}. The two are compared, never added.")
c[2].metric("Open AR", fmt(o["open_ar_cents"]), help="Unpaid and partially paid invoices. Not an exception.")
c[3].metric("Payment records reconciled",
            f"{o['match_rate'] * 100:.1f}%" if o["match_rate"] is not None else "—",
            help="Stripe + QBO payment records that are MATCHED or NON_STRIPE_PAYMENT")
c = st.columns(4)
c[0].metric("Reconciliation exceptions", o["exceptions"], help=f"Exposure {fmt(o['exception_cents'])}")
c[1].metric("Billing exceptions", o["billing_issues"], help="Ad spend → invoice mismatches and missing invoices")
c[2].metric("Sources not fully validated", o["data_issues"], help="NO_DATA, INCOMPLETE or STALE scopes")
c[3].metric("Clients / locations", f"{o['clients']} / {o['locations']}",
            help=f"{o['out_of_scope_records']} records belong to customers not in the client registry "
                 "(e.g. a sandbox's sample data) and are not reconciled")

if ev:
    ok = ev["scenarios_passed"] == ev["scenarios_total"] and ev["false_positive_exceptions"] == 0
    msg = (f"**Scenario validation:** {ev['scenarios_passed']} / {ev['scenarios_total']} scenarios passed · "
           f"{ev['correct']} / {ev['items']} classifications match the ground truth · "
           f"{ev['false_positive_exceptions']} false-positive exceptions · {ev['missed_exceptions']} missed exceptions. "
           "Computed from the evaluator's results table for this run, not hard-coded.")
    (st.success if ok else st.error)(msg)

st.subheader("Where things stand")
left, right = st.columns([3, 2])
with left:
    counts = pd.DataFrame(dd.status_counts(db, run["run_id"]))
    if not counts.empty:
        pivot = counts.pivot_table(index="status", columns="record_type", values="n", aggfunc="sum", fill_value=0)
        st.dataframe(pivot, use_container_width=True)
with right:
    fresh = pd.DataFrame(dd.source_freshness(db))
    st.markdown("**Source freshness**")
    st.dataframe(fresh, hide_index=True, use_container_width=True)

st.subheader("Top exceptions")
exc = pd.DataFrame(dd.results(db, run["run_id"], exceptions_only=True))
if exc.empty:
    st.write("No exceptions.")
else:
    exc = money_cols(exc[["status", "record_type", "client", "location", "source_record_id", "amount_cents",
                          "diff_cents", "reason"]].copy(), ["amount_cents", "diff_cents"])
    st.dataframe(styled(exc), hide_index=True, use_container_width=True)
    st.caption("Open **Reconciliation** in the sidebar to filter and inspect any record.")

st.subheader("30-second tour")
st.markdown(
    "1. **Reconciliation** → filter status `MISSING_IN_QBO` → select the row: the reason, the Stripe ID, the "
    "invoice it claims to pay and the raw API page it came from.\n"
    "2. Same page, status `NEEDS_REVIEW`: two identical $500 deposits, one QBO entry. The engine refuses to guess.\n"
    "3. **Data Quality** → Paterson is `NO_DATA` (API down) and is never shown as $0; Astoria is `STALE`.\n"
    "4. **Billing & Payouts** → Hackensack was billed the management fee twice; New Rochelle's October spend was "
    "never invoiced; every payout equals gross − fees − refunds.\n"
    "5. **Architecture** → how the pieces fit and why.\n"
    "6. **Retail Data Quality** → 1.07M real public retail rows (UCI): nothing dropped, every problem counted.")
board = pd.DataFrame(dd.scenario_scoreboard(db, run["run_id"], scenario_catalog()))
with st.expander(f"All {len(board)} test scenarios and their results"):
    st.dataframe(board[["scenario", "title", "description", "interview_question", "correct", "items", "passed"]],
                 hide_index=True, use_container_width=True)
