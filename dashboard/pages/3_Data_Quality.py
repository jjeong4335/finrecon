"""Page 3 — Data quality and pipeline health."""
import pandas as pd
import streamlit as st

from common import dd, scenario_catalog, setup, styled

db, run = setup("Data Quality & Pipeline Health")
run_id = run["run_id"]

st.subheader("Data status per source and location")
st.caption("ZERO_TRANSACTIONS means a complete answer of zero. NO_DATA, INCOMPLETE and STALE are never shown as $0; "
           "a failed or partial pull never overwrites the last validated data.")
ds = pd.DataFrame(dd.data_status(db))
st.dataframe(styled(ds), hide_index=True, use_container_width=True)

st.subheader("Ingestion runs")
hist = pd.DataFrame(dd.ingestion_history(db))
runs = sorted(hist["run_id"].unique(), reverse=True)
pick = st.selectbox("Run", runs)
h = hist[hist["run_id"] == pick].drop(columns=["run_id"])
st.dataframe(styled(h), hide_index=True, use_container_width=True)
st.caption("records_expected comes from the source's own totals; it must equal records_received for a pull to load.")

st.subheader("Validation checks (latest run)")
q = pd.DataFrame(dd.quality_findings(db, run_id))
fails = q[~q["passed"]]
st.metric("Failed checks", len(fails), help=f"{len(q)} checks evaluated")
st.dataframe(fails, hide_index=True, use_container_width=True)
with st.expander("All checks"):
    st.dataframe(q, hide_index=True, use_container_width=True)

st.subheader("Ground-truth evaluation")
board = pd.DataFrame(dd.scenario_scoreboard(db, run_id, scenario_catalog()))
st.dataframe(board[["scenario", "title", "interview_question", "correct", "items", "passed"]], hide_index=True,
             use_container_width=True)
ev = pd.DataFrame(dd.evaluation_rows(db, run_id))
bad = ev[ev["outcome"] != "correct"]
st.write(f"{len(ev)} items evaluated · {len(bad)} not correct")
if not bad.empty:
    st.dataframe(bad, hide_index=True, use_container_width=True)
