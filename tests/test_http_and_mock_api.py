"""Retry/backoff and the mock ad API over real HTTP (stdlib server)."""
import json

import pytest

from finrecon.adapters.mock_ad import MockAdClient
from finrecon.config import ROOT
from finrecon.http import ApiError, CallStats, InProcessTransport, RequestsTransport, RetryPolicy, request_with_retry
from mock_ad_api.service import MockAdService
from mock_ad_api.stdlib_server import serve_in_thread

AD_DIR = ROOT / "fixtures" / "offline" / "ad_platform"


@pytest.fixture
def service():
    return MockAdService.from_dir(AD_DIR, run_no=2, page_size=10)


def test_rate_limit_over_real_http_is_retried_with_retry_after(service):
    server, url = serve_in_thread(service)
    slept = []
    try:
        client = MockAdClient(RequestsTransport(url), page_size=10, policy=RetryPolicy(sleep=slept.append))
        stats = CallStats()
        ex = client.extract_spend("adv-101", "adv-101-01", "2026-11-01", "2026-11-30", stats)
    finally:
        server.shutdown()
    assert stats.retries == 1 and slept == [1.0]          # honored Retry-After: 1
    assert len(ex.rows) == ex.total_records_reported == 60
    assert sum(r["amount_cents"] for r in ex.rows) == ex.summary_total_cents


def test_retries_are_bounded_and_then_raise(service):
    slept = []
    client = MockAdClient(InProcessTransport(service.handle), policy=RetryPolicy(max_attempts=3, sleep=slept.append))
    with pytest.raises(ApiError) as err:
        client.extract_spend("adv-103", "adv-103-02", "2026-11-01", "2026-11-30", CallStats())
    assert err.value.status == 503 and err.value.attempts == 3
    assert slept == [0.5, 1.0]                             # exponential backoff


def test_non_retryable_errors_fail_fast(service):
    stats = CallStats()
    with pytest.raises(ApiError) as err:
        request_with_retry(InProcessTransport(service.handle), "GET", "/clients/nope/locations",
                           policy=RetryPolicy(sleep=lambda s: None), stats=stats)
    assert err.value.status == 404 and stats.requests == 1


def test_missing_page_is_silent_in_the_api_but_detectable(service):
    client = MockAdClient(InProcessTransport(service.handle), page_size=10, policy=RetryPolicy(sleep=lambda s: None))
    ex = client.extract_spend("adv-102", "adv-102-03", "2026-11-22", "2026-11-30", CallStats())
    assert ex.pages_received == ex.total_pages_reported      # every page "succeeded" with HTTP 200 ...
    assert len(ex.rows) < ex.total_records_reported          # ... yet records are missing


def test_validation_errors(service):
    status, _, body = service.handle("GET", "/clients/adv-101/locations/adv-101-02/spend", {"start": "x", "end": "y"})
    assert status == 400
    status, _, _ = service.handle("GET", "/clients/adv-101/locations/adv-101-02/spend",
                                  {"start": "2026-11-01", "end": "2026-11-02", "page_size": "5000"})
    assert status == 400


def test_fastapi_app_routes_match_service():
    """The FastAPI wrapper is only importable where FastAPI is installed (Docker)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mock_ad_api.app import app

    r = TestClient(app).get("/clients")
    assert r.status_code == 200 and json.loads(r.text)["total_records"] == 3
