"""Offline transports that answer QBO and Stripe requests from recorded objects.

They implement the same request contract as the real APIs (query strings,
STARTPOSITION/MAXRESULTS, limit/starting_after/has_more, ?payout= filter), so
the adapters' pagination logic is exercised for real. Objects created after the
run's as-of date are hidden, which lets two offline runs see two points in time.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..http import Response, decode_json


def _load(path: Path) -> list[dict]:
    return decode_json(path.read_text())


def _strip(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if not k.startswith("_fixture_")}


def _respond(status: int, body: Any) -> Response:
    text = json.dumps(body, default=str)
    return Response(status, decode_json(text), {}, text)


class QboFixtureTransport:
    QUERY = re.compile(r"select \* from (\w+)\s+startposition (\d+) maxresults (\d+)\s*$", re.I)

    def __init__(self, fixture_dir: Path, as_of: date):
        self.dir = fixture_dir / "qbo"
        self.as_of = as_of

    def _visible(self, obj: dict) -> bool:
        # Transactions are visible from their business date; reference data
        # (customers, payment methods) has no TxnDate and is always visible.
        txn = obj.get("TxnDate")
        return txn is None or date.fromisoformat(str(txn)[:10]) <= self.as_of

    def request(self, method, path, params=None, data=None, headers=None, json_body=None) -> Response:
        if method != "GET" or not re.fullmatch(r"/v3/company/[\w-]+/query", path):
            return _respond(400, {"Fault": {"Error": [{"Message": "offline fixtures are read-only"}]}})
        m = self.QUERY.match((params or {}).get("query", ""))
        if not m:
            return _respond(400, {"Fault": {"Error": [{"Message": "unsupported query"}]}})
        entity, start, size = m[1], int(m[2]), int(m[3])
        file = self.dir / f"{entity}.json"
        rows = [r for r in (_load(file) if file.exists() else []) if self._visible(r)]
        batch = rows[start - 1: start - 1 + size]
        qr: dict[str, Any] = {}
        if batch:
            qr = {entity: batch, "startPosition": start, "maxResults": len(batch)}
        return _respond(200, {"QueryResponse": qr, "time": f"{self.as_of.isoformat()}T06:00:00.000-08:00"})


class StripeFixtureTransport:
    RESOURCES = {"customers", "payment_intents", "charges", "refunds", "balance_transactions", "payouts"}

    def __init__(self, fixture_dir: Path, as_of: date):
        self.dir = fixture_dir / "stripe"
        self.cutoff = int(datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())

    def request(self, method, path, params=None, data=None, headers=None, json_body=None) -> Response:
        m = re.fullmatch(r"/v1/(\w+)", path)
        if method != "GET" or not m or m[1] not in self.RESOURCES:
            return _respond(404, {"error": {"type": "invalid_request_error", "message": "offline: unsupported"}})
        params = dict(params or {})
        # _fixture_created (set by seed.live_seed capture) carries the business
        # date of sandbox objects that could not be back-dated.
        objs = [o for o in _load(self.dir / f"{m[1]}.json")
                if o.get("_fixture_created", o.get("created", 0)) <= self.cutoff]
        if "payout" in params:
            objs = [o for o in objs if o.get("_fixture_payout") == params["payout"]]
        # Stripe lists are newest first.
        objs.sort(key=lambda o: (o.get("created", 0), o["id"]), reverse=True)
        limit = max(1, min(int(params.get("limit", 10)), 100))
        if params.get("starting_after"):
            ids = [o["id"] for o in objs]
            if params["starting_after"] not in ids:
                return _respond(400, {"error": {"type": "invalid_request_error", "message": "bad cursor"}})
            objs = objs[ids.index(params["starting_after"]) + 1:]
        page = [_strip(o) for o in objs[:limit]]
        return _respond(200, {"object": "list", "url": path, "has_more": len(objs) > limit, "data": page})
