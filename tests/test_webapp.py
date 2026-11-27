"""Django web app: every page renders from the demo database.

Skipped where Django is not installed; CI installs it and runs these on SQLite
and on PostgreSQL.
"""
import os

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
django = pytest.importorskip("django")
django.setup()

from django.test import Client  # noqa: E402

from webapp import views  # noqa: E402


@pytest.fixture
def client(demo, monkeypatch):
    db, settings, run_id = demo
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    views.reset_db()
    yield Client(), db, run_id
    views.reset_db()


def get(c, url, status=200):
    r = c.get(url)
    assert r.status_code == status, (url, r.status_code, r.content[:500])
    return r.content.decode()


def test_overview(client):
    c, _, _ = client
    html = get(c, "/")
    assert "Scenario validation: 27 / 27 scenarios passed" in html
    assert "Not affiliated" in html and "MISSING_IN_QBO" in html


def test_reconciliation_filters(client):
    c, db, run_id = client
    html = get(c, "/reconciliation/")
    n_exc = db.scalar("SELECT COUNT(*) FROM reconciliation_exceptions WHERE run_id = ?", (run_id,))
    assert f"{n_exc} of" in html
    html = get(c, "/reconciliation/?exceptions=1&status=MISSING_IN_QBO")
    assert "1 of" in html and "Cash received but AR not reduced" in html
    html = get(c, "/reconciliation/?exceptions=0&status=OPEN_AR&client=Maple+Leaf+Dental+Group")
    assert "Unpaid invoice" in html and "Harbor Fitness" not in html.split("<table>")[1].split("</table>")[0]
    get(c, "/reconciliation/?date_from=not-a-date")          # bad input is ignored, not a 500


def test_record_page_for_every_record_type(client):
    c, db, run_id = client
    for t in ("invoice", "stripe_payment", "qbo_payment", "stripe_refund", "qbo_refund"):
        r = db.query("SELECT record_id, source_record_id FROM reconciliation_results WHERE run_id = ? "
                     "AND record_type = ? LIMIT 1", (run_id, t))[0]
        html = get(c, f"/record/{t}/{r['record_id']}/")
        assert r["source_record_id"] in html and "Why:" in html
        if t == "stripe_payment":
            assert "Journal-entry preview" in html and "balanced" in html


def test_record_page_404s(client):
    c, _, _ = client
    get(c, "/record/nonsense/abc/", status=404)
    get(c, "/record/invoice/00000000-0000-0000-0000-000000000000/", status=404)


def test_billing_data_quality_architecture(client):
    c, _, _ = client
    assert "BILLING_MISMATCH" in get(c, "/billing/") and "NOT_INVOICED" in get(c, "/billing/")
    assert "BILLING_MATCHED" in get(c, "/billing/?all=1")
    html = get(c, "/data-quality/")
    assert "NO_DATA" in html and "STALE" in html and "27/27" not in html  # per-scenario rows, not a total
    assert "run-01" in get(c, "/data-quality/?run=run-01-2026-11-25")
    assert "mermaid" in get(c, "/architecture/")


def test_retail_page_before_load(client):
    c, _, _ = client
    assert "has not been loaded yet" in get(c, "/retail/")


def test_retail_page_after_load(tmp_path, monkeypatch):
    from finrecon import pipeline
    from finrecon.config import get_settings
    from finrecon.db import Database
    from finrecon.ingestion import uci_retail
    from tests.test_uci_retail import make_zip

    url = f"sqlite:///{tmp_path / 'web.db'}"
    db = Database(url)
    pipeline.demo(get_settings(raw_dir=tmp_path / "raw", database_url=url), db, sleep=lambda s: None)
    uci_retail.load(db, make_zip(tmp_path), log=lambda *a: None)
    monkeypatch.setenv("DATABASE_URL", url)
    views.reset_db()
    try:
        html = get(Client(), "/retail/")
    finally:
        views.reset_db()
    assert "Rows read" in html and "control_amount_totals" in html and "<polyline" in html


def test_healthz(client):
    c, _, _ = client
    assert get(c, "/healthz") == "ok"


def test_overview_shows_prioritized_queue(client):
    c, _, _ = client
    html = get(c, "/")
    assert "Exception queue:" in html and "incidents" in html and "Cash received but not recorded" in html


def test_readyz(client, tmp_path, monkeypatch):
    c, _, _ = client
    import json as _json

    body = _json.loads(get(c, "/readyz"))
    assert body["ready"] is True and body["scenarios_passed"] == body["scenarios_total"] == 27
    from finrecon.db import Database

    empty = f"sqlite:///{tmp_path / 'empty.db'}"
    Database(empty).init_schema()
    monkeypatch.setenv("DATABASE_URL", empty)
    views.reset_db()
    get(Client(), "/readyz", status=503)
    get(Client(), "/healthz")                                       # alive, just not ready


def test_readyz_fails_when_evaluation_has_a_wrong_item(fresh_demo, monkeypatch):
    db, settings, run_id = fresh_demo
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    views.reset_db()
    get(Client(), "/readyz")
    db.execute("UPDATE evaluation_results SET outcome = 'wrong', actual = 'MATCHED' WHERE run_id = ? AND item_key = "
               "(SELECT MIN(item_key) FROM evaluation_results WHERE run_id = ?)", (run_id, run_id))
    db.commit()
    import json as _json

    body = _json.loads(get(Client(), "/readyz", status=503))
    assert body["ready"] is False and any("wrong" in r for r in body["reasons"])
    views.reset_db()


def test_readyz_fails_when_a_scenario_is_missing(fresh_demo, monkeypatch):
    db, settings, run_id = fresh_demo
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    views.reset_db()
    db.execute("DELETE FROM evaluation_results WHERE run_id = ? AND scenario = 'S19'", (run_id,))
    db.commit()
    assert "26 of 27 scenarios" in get(Client(), "/readyz", status=503)
    views.reset_db()


def test_readyz_fails_when_run_date_differs_from_recon_as_of(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv("RECON_AS_OF", "2026-12-15")
    assert "expected RECON_AS_OF 2026-12-15" in get(c, "/readyz", status=503)
