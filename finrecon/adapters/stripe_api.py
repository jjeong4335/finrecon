"""Stripe API adapter (sandbox / test keys only).

Lists use cursor pagination (limit + starting_after, until has_more is false).
Amounts arrive as integer minor units. Writes are form-encoded and always carry
an Idempotency-Key so a re-run seed cannot create duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..http import CallStats, RequestsTransport, RetryPolicy, Transport, request_with_retry
from .qbo import Page

API_BASE = "https://api.stripe.com"


def form_encode(data: dict, prefix: str = "") -> dict[str, str]:
    """Stripe's bracket notation: {"metadata": {"a": 1}} -> {"metadata[a]": "1"}."""
    out: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out.update(form_encode(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.update(form_encode(item, f"{name}[{i}]"))
                else:
                    out[f"{name}[{i}]"] = str(item)
        elif isinstance(value, bool):
            out[name] = "true" if value else "false"
        elif value is not None:
            out[name] = str(value)
    return out


@dataclass
class StripeClient:
    transport: Transport
    page_size: int = 100
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    stats: CallStats = field(default_factory=CallStats)

    def list_all(self, resource: str, params: dict | None = None) -> tuple[list[dict], list[Page]]:
        items: list[dict] = []
        pages: list[Page] = []
        cursor = None
        n = 0
        while True:
            q = {"limit": self.page_size, **(params or {})}
            if cursor:
                q["starting_after"] = cursor
            resp = request_with_retry(self.transport, "GET", f"/v1/{resource}", params=q,
                                      policy=self.policy, stats=self.stats)
            n += 1
            suffix = "_".join(f"{k}-{v}" for k, v in (params or {}).items())
            pages.append(Page(f"{resource}{'_' + suffix if suffix else ''}_p{n}", resp.body))
            data = resp.body.get("data", [])
            items.extend(data)
            if not resp.body.get("has_more") or not data:
                return items, pages
            cursor = data[-1]["id"]

    def create(self, resource: str, data: dict, idempotency_key: str) -> dict:
        resp = request_with_retry(self.transport, "POST", f"/v1/{resource}", data=form_encode(data),
                                  headers={"Idempotency-Key": idempotency_key,
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  policy=self.policy, stats=self.stats)
        return resp.body

    def get(self, resource: str, obj_id: str, params: dict | None = None) -> Any:
        return request_with_retry(self.transport, "GET", f"/v1/{resource}/{obj_id}", params=params,
                                  policy=self.policy, stats=self.stats).body


def live_client(settings) -> StripeClient:
    if not settings.stripe_secret_key:
        raise ValueError("STRIPE_SECRET_KEY (sk_test_...) is required in live mode")
    key = settings.stripe_secret_key
    return StripeClient(RequestsTransport(API_BASE, auth_header=lambda: f"Bearer {key}"))
