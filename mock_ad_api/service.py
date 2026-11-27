"""Mock advertising platform: the "internal platform" data source.

Framework-free so the exact same logic is served by FastAPI (deployment), by a
stdlib HTTP server (tests over real HTTP) and in-process (offline pipeline).

Failures are deterministic and configured per ad location (failures.json):
  rate_limit_first_request  first spend/summary request returns 429 (Retry-After)
  always_fail               every location request returns 503
  fail_on_runs              503 only on the listed pipeline run numbers
  missing_page_on_runs      page 2 (or the last page) silently returns no rows
  summary_mismatch_on_runs  summary total disagrees with the detail rows
  duplicate_rows            each page repeats the last row of the previous page
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class MockAdService:
    def __init__(self, dataset: dict, failures: dict | None = None, run_no: int = 1,
                 page_size: int = DEFAULT_PAGE_SIZE):
        self.clients = dataset["clients"]
        self.locations = dataset["locations"]
        self.spend_by_loc: dict[str, list[dict]] = {}
        for row in dataset["spend"]:
            self.spend_by_loc.setdefault(row["location_id"], []).append(row)
        for rows in self.spend_by_loc.values():
            rows.sort(key=lambda r: (r["date"], r["campaign"], r["id"]))
        self.failures = failures or {}
        self.run_no = run_no
        self.page_size = page_size
        self._rate_limited: set[str] = set()

    @classmethod
    def from_dir(cls, path: Path, run_no: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> "MockAdService":
        dataset = json.loads((path / "dataset.json").read_text())
        failures = json.loads((path / "failures.json").read_text())
        return cls(dataset, failures, run_no, page_size)

    # ------------------------------------------------------------------ routing
    def handle(self, method: str, path: str, params: dict, headers: dict | None = None) -> tuple[int, dict, Any]:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        run_no = int(headers.get("x-demo-run", self.run_no))
        if method.upper() != "GET":
            return 405, {}, {"error": "method not allowed"}
        if path in ("/", "/health"):
            return 200, {}, {"status": "ok", "run_no": run_no}
        if path == "/clients":
            return self._paged(self.clients, params)
        m = re.fullmatch(r"/clients/([\w-]+)/locations", path)
        if m:
            if not any(c["id"] == m[1] for c in self.clients):
                return 404, {}, {"error": f"unknown client {m[1]}"}
            return self._paged([l for l in self.locations if l["client_id"] == m[1]], params)
        m = re.fullmatch(r"/clients/([\w-]+)/locations/([\w-]+)/(spend|summary)", path)
        if m:
            return self._location(m[1], m[2], m[3], params, run_no)
        return 404, {}, {"error": "not found"}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _page_args(params: dict) -> tuple[int, int] | None:
        try:
            page = int(params.get("page", 1))
            size = int(params.get("page_size", DEFAULT_PAGE_SIZE))
        except ValueError:
            return None
        if page < 1 or not 1 <= size <= MAX_PAGE_SIZE:
            return None
        return page, size

    def _paged(self, items: list, params: dict):
        args = self._page_args({"page_size": self.page_size, **params})
        if args is None:
            return 400, {}, {"error": "invalid pagination"}
        page, size = args
        total_pages = max(1, math.ceil(len(items) / size))
        return 200, {}, {"data": items[(page - 1) * size: page * size], "page": page, "page_size": size,
                         "total_pages": total_pages, "total_records": len(items)}

    def _location(self, client_id: str, loc_id: str, kind: str, params: dict, run_no: int):
        loc = next((l for l in self.locations if l["id"] == loc_id and l["client_id"] == client_id), None)
        if loc is None:
            return 404, {}, {"error": f"unknown location {loc_id}"}
        try:
            start = date.fromisoformat(params["start"])
            end = date.fromisoformat(params["end"])
        except (KeyError, ValueError):
            return 400, {}, {"error": "start and end (YYYY-MM-DD) are required"}
        if end < start:
            return 400, {}, {"error": "end before start"}

        f = self.failures.get(loc_id, {})
        if f.get("always_fail") or run_no in f.get("fail_on_runs", []):
            return 503, {}, {"error": "reporting backend unavailable for this location"}
        if f.get("rate_limit_first_request") and (run_no, loc_id) not in self._rate_limited:
            self._rate_limited.add((run_no, loc_id))
            return 429, {"Retry-After": "1"}, {"error": "rate limit exceeded"}

        rows = [r for r in self.spend_by_loc.get(loc_id, []) if start.isoformat() <= r["date"] <= end.isoformat()]
        if kind == "summary":
            total = sum(r["amount_cents"] for r in rows)
            if run_no in f.get("summary_mismatch_on_runs", []):
                total += 12345
            return 200, {}, {"location_id": loc_id, "start": start.isoformat(), "end": end.isoformat(),
                             "record_count": len(rows), "total_amount_cents": total, "currency": "USD"}

        args = self._page_args({"page_size": self.page_size, **params})
        if args is None:
            return 400, {}, {"error": "invalid pagination"}
        page, size = args
        total_pages = max(1, math.ceil(len(rows) / size))
        data = rows[(page - 1) * size: page * size]
        if f.get("duplicate_rows") and page > 1 and data:
            data = [rows[(page - 1) * size - 1]] + data       # overlapping pages
        if run_no in f.get("missing_page_on_runs", []) and total_pages > 1 and page == min(2, total_pages):
            data = []                                          # silently dropped page
        return 200, {}, {"data": data, "page": page, "page_size": size,
                         "total_pages": total_pages, "total_records": len(rows)}
