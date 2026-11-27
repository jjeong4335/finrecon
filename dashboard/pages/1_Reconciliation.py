"""Page 2 — Reconciliation exceptions and drill-down."""
import pandas as pd
import streamlit as st

from common import dd, money_cols, money_config, setup, styled
from finrecon.money import fmt
from finrecon.recon.accounting import journal_preview

db, run = setup("Reconciliation")
run_id = run["run_id"]
rows = pd.DataFrame(dd.results(db, run_id))

f1, f2, f3, f4 = st.columns(4)
only_exc = f1.toggle("Exceptions only", value=True)
clients = sorted(rows["client"].dropna().unique())
pick_clients = f2.multiselect("Client", clients)
statuses = sorted(rows["status"].unique())
pick_status = f3.multiselect("Status", statuses)
types = sorted(rows["record_type"].unique())
pick_types = f4.multiselect("Record type", types)
g1, g2 = st.columns(2)
locs = sorted(rows["location"].dropna().unique())
pick_locs = g1.multiselect("Location", locs)
dates = pd.to_datetime(rows["record_date"])
date_range = g2.date_input("Record date range", (dates.min().date(), dates.max().date()))

view = rows
if only_exc:
    view = view[view["is_exception"]]
if pick_clients:
    view = view[view["client"].isin(pick_clients)]
if pick_status:
    view = view[view["status"].isin(pick_status)]
if pick_types:
    view = view[view["record_type"].isin(pick_types)]
if pick_locs:
    view = view[view["location"].isin(pick_locs)]
if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    d = pd.to_datetime(view["record_date"])
    view = view[(d >= pd.Timestamp(date_range[0])) & (d <= pd.Timestamp(date_range[1]))]

st.caption(f"{len(view)} of {len(rows)} records. Select a row to see why it was classified this way.")
table = money_cols(view[["status", "record_type", "client", "location", "source", "source_record_id", "match_method",
                         "amount_cents", "counterpart_amount_cents", "diff_cents", "record_date", "reason"]].copy(),
                   ["amount_cents", "counterpart_amount_cents", "diff_cents"])
event = st.dataframe(styled(table), hide_index=True, use_container_width=True, on_select="rerun",
                     selection_mode="single-row", column_config=money_config(table), key="recon_table")

selected = event.selection.rows if event and hasattr(event, "selection") else []
if selected:
    rec = view.iloc[selected[0]]
    detail = dd.record_detail(db, run_id, rec["record_type"], rec["record_id"])
    r = detail["result"]
    st.divider()
    st.subheader(f"{r['record_type'].replace('_', ' ').title()} {r['source_record_id']} — {r['status']}")
    st.write(dd.STATUS_HELP.get(r["status"], ""))
    a, b, c, d = st.columns(4)
    a.metric("Amount", fmt(r["amount_cents"]))
    b.metric("Counterpart amount", fmt(r["counterpart_amount_cents"]))
    c.metric("Difference", fmt(r["diff_cents"]) if r["diff_cents"] is not None else "—")
    d.metric("Match method", r["match_method"] or "—")
    st.markdown(f"**Why:** {r['reason']}")
    src = detail["source_row"] or {}
    ids = {
        "Internal record ID": r["record_id"],
        "Source system": r["source"],
        "Source record ID": r["source_record_id"],
        "Counterpart ID(s)": r["counterpart_source_ids"] or "—",
        "Seed / scenario key": r["scenario_id"] or "—",
        "Ingestion run": src.get("run_id"),
        "Ingested at": src.get("ingested_at"),
    }
    if "referenced_stripe_id" in src:
        ids["Stripe ID in QBO memo"] = src.get("referenced_stripe_id") or "— (none)"
        ids["QBO payment method"] = f"{src.get('payment_method_name')} ({src.get('payment_method_type')})"
    if "qbo_invoice_ref" in src:
        ids["Invoice named in Stripe metadata"] = src.get("qbo_invoice_ref") or "—"
    st.table(pd.DataFrame(ids.items(), columns=["Field", "Value"]).set_index("Field"))
    if detail["allocations"]:
        st.markdown("**Allocations (QBO Line.LinkedTxn)**")
        st.dataframe(money_cols(pd.DataFrame(detail["allocations"]), ["amount_cents"]), hide_index=True)
    with st.expander(f"Raw API pages archived for this ingestion run ({len(detail['raw_pages'])})"):
        st.dataframe(pd.DataFrame(detail["raw_pages"]), hide_index=True, use_container_width=True)
    with st.expander("Normalized source row"):
        st.json(src)
    if r["record_type"] == "stripe_payment":
        st.markdown("**Journal-entry preview** (educational illustration, not posted to QuickBooks)")
        for e in journal_preview(db, r["source_record_id"], run_id):
            lines = pd.DataFrame([{"Account": l.account, "Debit": l.debit / 100 if l.debit else None,
                                   "Credit": l.credit / 100 if l.credit else None} for l in e.lines])
            st.markdown(f"*{e.title}* · {e.date} · {'balanced ✓' if e.balanced else 'NOT balanced'}"
                        + (f" · {e.note}" if e.note else ""))
            st.dataframe(lines, hide_index=True, column_config={
                "Debit": st.column_config.NumberColumn(format="$%.2f"),
                "Credit": st.column_config.NumberColumn(format="$%.2f")})

st.divider()
with st.expander("Cross-system view (SQL FULL OUTER JOIN on the verified reference)"):
    st.caption("Rows with one side empty are the starting point for 'exists in one system but not the other'. "
               "Sync windows, payment methods and candidate matching then refine them into the statuses above.")
    cross = money_cols(pd.DataFrame(dd.cross_system(db)), ["stripe_amount_cents", "qbo_amount_cents"])
    st.dataframe(styled(cross, ("presence",)), hide_index=True, use_container_width=True)
