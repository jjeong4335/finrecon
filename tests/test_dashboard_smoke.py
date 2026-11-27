"""Execute every Streamlit page against a recording stub of the Streamlit API.

Streamlit is not needed for the test suite. The stub returns realistic values
(first option, default toggles, a selected table row) so every query and every
branch of the page code actually runs, including the drill-down panel.
"""
from __future__ import annotations

import runpy
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent.parent / "dashboard"
PAGES = ["Home.py", "pages/1_Reconciliation.py", "pages/2_Billing_and_Payouts.py", "pages/3_Data_Quality.py",
         "pages/4_Architecture.py"]


class _Stop(Exception):
    pass


class _Box:
    """A container (column, expander, tab) that accepts any Streamlit call."""

    def __init__(self, st):
        self._st = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(self._st, name)


def make_stub(select_row: int | None, toggles: bool | None):
    st = types.ModuleType("streamlit")
    st.calls = []

    def record(name, ret=None):
        def fn(*args, **kwargs):
            st.calls.append((name, args, kwargs))
            return ret(*args, **kwargs) if callable(ret) else ret
        return fn

    def cache(fn=None, **_):
        return fn if fn else (lambda f: f)

    def dataframe(data, *a, **kw):
        st.calls.append(("dataframe", (data,), kw))
        frame = getattr(data, "data", data)       # unwrap a pandas Styler
        rows = [select_row] if (kw.get("on_select") and select_row is not None and len(frame) > select_row) else []
        return types.SimpleNamespace(selection=types.SimpleNamespace(rows=rows))

    def stop():
        raise _Stop()

    st.cache_resource = cache
    st.cache_data = cache
    st.stop = stop
    st.dataframe = dataframe
    st.columns = record("columns", lambda spec, **k: [_Box(st) for _ in range(spec if isinstance(spec, int) else len(spec))])
    st.tabs = record("tabs", lambda labels: [_Box(st) for _ in labels])
    st.expander = record("expander", lambda *a, **k: _Box(st))
    st.multiselect = record("multiselect", lambda label, options, default=None, **k: list(default or []))
    st.selectbox = record("selectbox", lambda label, options, **k: list(options)[0])
    st.toggle = record("toggle", lambda label, value=False, **k: value if toggles is None else toggles)
    st.date_input = record("date_input", lambda label, value=None, **k: value)
    st.column_config = types.SimpleNamespace(NumberColumn=lambda *a, **k: None)
    for name in ("set_page_config", "title", "caption", "info", "error", "success", "metric", "subheader",
                 "markdown", "write", "divider", "table", "json", "graphviz_chart"):
        setattr(st, name, record(name))
    return st


@contextmanager
def streamlit_stub(monkeypatch, db_url: str, select_row=None, toggles=None):
    stub = make_stub(select_row, toggles)
    monkeypatch.setitem(sys.modules, "streamlit", stub)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.syspath_prepend(str(DASH))
    sys.modules.pop("common", None)
    try:
        yield stub
    finally:
        sys.modules.pop("common", None)


@pytest.mark.parametrize("page", PAGES)
def test_page_runs(page, demo, monkeypatch):
    db, settings, _ = demo
    with streamlit_stub(monkeypatch, settings.database_url) as st:
        runpy.run_path(str(DASH / page), run_name="__main__")
    assert any(c[0] == "title" for c in st.calls)
    assert any(c[0] == "info" and "Not affiliated" in c[1][0] for c in st.calls)


def test_reconciliation_drill_down_for_every_record_type(demo, monkeypatch):
    db, settings, run_id = demo
    from finrecon.dashboard_data import results

    rows = results(db, run_id)
    first_of = {}
    for i, r in enumerate(rows):
        first_of.setdefault(r["record_type"], i)
    for record_type, idx in first_of.items():
        with streamlit_stub(monkeypatch, settings.database_url, select_row=idx, toggles=False) as st:
            runpy.run_path(str(DASH / "pages/1_Reconciliation.py"), run_name="__main__")
        subheaders = [c[1][0] for c in st.calls if c[0] == "subheader"]
        assert any(rows[idx]["source_record_id"] in s for s in subheaders), record_type
        if record_type == "stripe_payment":
            assert any(c[0] == "markdown" and "balanced" in c[1][0] for c in st.calls)
