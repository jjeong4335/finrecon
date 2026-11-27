"""Page 5 — Real public data: UCI Online Retail II quality benchmark."""
import pandas as pd
import streamlit as st

from common import dd, money_cols, setup
from finrecon.ingestion.uci_retail import CITATION

db, run = setup("Retail Data Quality (real public data)")
st.markdown(
    "A separate module that loads **UCI Online Retail II**: about 1.07 million real invoice lines from a UK online "
    "retailer (2009-12 to 2011-12, amounts in GBP). It shows how messy real data is cleaned without losing anything. "
    "This data has no Stripe or QuickBooks IDs, so it is **never** matched against the reconciliation above, and "
    "Country is the customer's country, not a store location.")
st.caption(f"Source: {CITATION}")

s = dd.retail_summary(db)
if not s:
    st.warning("The UCI dataset has not been loaded yet. Run:\n\n"
               "`python -m finrecon.ingestion.uci_retail all`   (downloads about 44 MB, then loads in a few minutes)")
    st.stop()

ld = s["load"]
c = st.columns(4)
c[0].metric("Rows read", f"{ld['rows_read']:,}")
c[1].metric("Rows kept", f"{ld['rows_loaded']:,}", help="Every source row is stored; problems are flagged, not dropped")
c[2].metric("Load time", f"{(ld['seconds_read_parse'] or 0) + (ld['seconds_load'] or 0) + (ld['seconds_validate'] or 0):.0f} s",
            help=f"read+parse {ld['seconds_read_parse']}s · write {ld['seconds_load']}s · validate {ld['seconds_validate']}s")
c[3].metric("Throughput", f"{ld['rows_per_second']:,.0f} rows/s", help=f"chunk size {ld['chunk_size']:,}")
st.caption(f"File {ld['source_file']} · sha256 {ld['source_sha256'][:16]}… · load {ld['load_id']} · status {ld['status']}")

st.subheader("Control totals and checks")
checks = pd.DataFrame(s["checks"])
checks["amount_gbp"] = checks.pop("amount_cents").map(lambda v: None if v is None else v / 100)
checks["share"] = checks["share"].map(lambda v: None if v is None else round(v * 100, 2))
checks = checks.rename(columns={"share": "share_%"})
st.dataframe(checks, hide_index=True, use_container_width=True,
             column_config={"amount_gbp": st.column_config.NumberColumn("Amount (£)", format="£%.2f")})
st.caption("Control checks prove nothing was lost: raw rows = parsed rows, and line totals = sum of classes = "
           "invoice totals. Repeated identical lines are flagged, not deleted, because the same item can be keyed "
           "twice on one invoice. Identical copies of an invoice in two sheets are excluded once.")

left, right = st.columns(2)
with left:
    st.subheader("Every line has exactly one class")
    cls = money_cols(pd.DataFrame(s["classes"]), ["amount_cents"])
    st.dataframe(cls, hide_index=True, use_container_width=True,
                 column_config={"amount": st.column_config.NumberColumn("Amount (£)", format="£%.2f")})
with right:
    st.subheader("Non-product stock codes")
    npc = money_cols(pd.DataFrame(s["non_product_codes"]), ["amount_cents"])
    st.dataframe(npc, hide_index=True, use_container_width=True,
                 column_config={"amount": st.column_config.NumberColumn("Amount (£)", format="£%.2f")})

st.subheader("By month")
monthly = pd.DataFrame(s["monthly"])
if not monthly.empty:
    monthly["sales_gbp"] = monthly.pop("sales_cents") / 100
    monthly["cancellations_gbp"] = monthly.pop("cancellations_cents") / 100
    monthly["missing_customer_%"] = (monthly["missing_customer_lines"] / monthly["lines"] * 100).round(1)
    st.line_chart(monthly.set_index("month")[["sales_gbp", "cancellations_gbp"]])
    st.dataframe(monthly, hide_index=True, use_container_width=True)

st.subheader("Cancellations: can each one be traced to an earlier sale?")
st.caption("Cancellation invoices do not reference the original sale. A cancellation is LINKED when the same "
           "customer bought the same item earlier in the data, for at least that quantity. NO_PRIOR_SALE is "
           "expected to be highest in the first months, when the original sale may predate the dataset.")
links = pd.DataFrame(s["cancellation_links"])
if not links.empty:
    st.dataframe(links.pivot_table(index="month", columns="status", values="lines", aggfunc="sum", fill_value=0),
                 use_container_width=True)

st.subheader("Top customer countries (valid sales)")
st.dataframe(money_cols(pd.DataFrame(s["countries"]), ["amount_cents"]), hide_index=True, use_container_width=True)

with st.expander("Examples of flagged lines (first 200)"):
    st.dataframe(pd.DataFrame(s["examples"]), hide_index=True, use_container_width=True)
