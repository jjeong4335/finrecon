"""Shared helpers for the Streamlit pages. The dashboard only reads the DB."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finrecon import dashboard_data as dd  # noqa: E402
from finrecon.config import get_settings  # noqa: E402
from finrecon.db import Database  # noqa: E402
from finrecon.report import DISCLAIMER  # noqa: E402

GOOD = {"MATCHED", "VALIDATED", "BILLING_MATCHED", "ZERO_TRANSACTIONS", "NO_INVOICE_EXPECTED", "NON_STRIPE_PAYMENT",
        "succeeded"}
NEUTRAL = {"OPEN_AR", "PARTIAL", "PENDING_SYNC"}
WARN = {"STALE", "INCOMPLETE", "NEEDS_REVIEW", "BILLING_DATA_UNAVAILABLE", "incomplete"}


@st.cache_resource
def get_db() -> Database:
    # In Docker this URL uses a read-only database role.
    return Database(get_settings().database_url, autocommit=True)


@st.cache_data
def scenario_catalog() -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "expected_reconciliation.json").read_text())["scenarios"]


def setup(title: str) -> tuple[Database, dict]:
    st.set_page_config(page_title=f"{title} · Finance Reconciliation", layout="wide")
    db = get_db()
    run = dd.latest_run(db)
    st.title(title)
    if not run:
        st.error("No completed pipeline run found. Run `python -m finrecon.pipeline demo` first.")
        st.stop()
    settings = get_settings()
    st.caption(f"Run **{run['run_id']}** · mode {run['mode']} · reconciliation as of **{settings.recon_as_of}** "
               f"(fixed date, so results never drift after deployment)")
    st.info(DISCLAIMER, icon="ℹ️")
    return db, run


def dollars(cents) -> float | None:
    return None if cents is None else cents / 100


def money_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Convert *_cents columns to dollars in place, keeping column order."""
    return df.rename(columns={c: c.replace("_cents", "") for c in cols if c in df}).assign(
        **{c.replace("_cents", ""): df[c].map(dollars) for c in cols if c in df})


def money_config(df: pd.DataFrame) -> dict:
    return {c: st.column_config.NumberColumn(c.replace("_", " ").title(), format="$%.2f")
            for c in df.columns if c in {"amount", "counterpart_amount", "diff", "spend", "fee", "discount", "tax",
                                         "expected", "invoiced", "gross", "net", "refund", "payout"}}


def status_style(value: str) -> str:
    if value in GOOD:
        return "background-color:#e6f4ec;color:#1f7a4d;font-weight:600"
    if value in NEUTRAL:
        return "background-color:#eef0f4;color:#3c4656;font-weight:600"
    if value in WARN:
        return "background-color:#fff3e0;color:#a86400;font-weight:600"
    if isinstance(value, str) and value.isupper():
        return "background-color:#fdecea;color:#b3261e;font-weight:600"
    return ""


def styled(df: pd.DataFrame, status_cols=("status",)):
    cols = [c for c in status_cols if c in df.columns]
    if not cols or df.empty:
        return df
    return df.style.map(status_style, subset=cols)
