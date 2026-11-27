"""Client for the mock advertising platform API (page-number pagination)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..http import CallStats, RetryPolicy, Transport, request_with_retry
from .qbo import Page


@dataclass
class SpendExtract:
    rows: list[dict]
    pages: list[Page]
    total_pages_reported: int
    pages_received: int
    total_records_reported: int
    summary_total_cents: int
    summary_record_count: int


@dataclass
class MockAdClient:
    transport: Transport
    page_size: int = 50
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    headers: dict = field(default_factory=dict)

    def _get(self, path: str, params: dict, stats: CallStats):
        return request_with_retry(self.transport, "GET", path, params=params, headers=self.headers,
                                  policy=self.policy, stats=stats).body

    def list_locations(self, client_id: str, stats: CallStats) -> list[dict]:
        out, page = [], 1
        while True:
            body = self._get(f"/clients/{client_id}/locations", {"page": page, "page_size": self.page_size}, stats)
            out.extend(body["data"])
            if page >= body["total_pages"]:
                return out
            page += 1

    def extract_spend(self, client_id: str, location_id: str, start: str, end: str,
                      stats: CallStats) -> SpendExtract:
        base = f"/clients/{client_id}/locations/{location_id}"
        summary = self._get(f"{base}/summary", {"start": start, "end": end}, stats)
        pages: list[Page] = [Page(f"summary_{location_id}_{start}_{end}", summary)]
        rows: list[dict] = []
        page, total_pages, total_records, received = 1, 1, 0, 0
        while page <= total_pages:
            body = self._get(f"{base}/spend", {"start": start, "end": end, "page": page,
                                               "page_size": self.page_size}, stats)
            pages.append(Page(f"spend_{location_id}_{start}_{end}_p{page}", body))
            total_pages = int(body["total_pages"])
            total_records = int(body["total_records"])
            rows.extend(body["data"])
            received += 1
            page += 1
        return SpendExtract(rows, pages, total_pages, received, total_records,
                            int(summary["total_amount_cents"]), int(summary["record_count"]))
