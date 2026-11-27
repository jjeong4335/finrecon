"""HTTP transport abstraction with bounded exponential backoff.

Adapters talk to a `Transport`, never to `requests` directly. Live mode uses
`RequestsTransport`; offline mode swaps in a fixture or in-process transport.
Pagination, retry and parsing code therefore runs identically in both modes.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""


class TransportError(Exception):
    """Network-level failure (connection refused, timeout)."""


class ApiError(Exception):
    def __init__(self, status: int, body: Any, attempts: int):
        super().__init__(f"HTTP {status} after {attempts} attempt(s): {str(body)[:200]}")
        self.status = status
        self.body = body
        self.attempts = attempts


class Transport(Protocol):
    def request(self, method: str, path: str, params: Mapping[str, Any] | None = None,
                data: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
                json_body: Any = None) -> Response: ...


def decode_json(text: str) -> Any:
    # parse_float=Decimal: QuickBooks money must never pass through binary floats.
    return json.loads(text, parse_float=Decimal) if text else None


class RequestsTransport:
    def __init__(self, base_url: str, headers: Mapping[str, str] | None = None,
                 timeout: float = 30.0, auth_header: Callable[[], str] | None = None):
        import requests

        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.auth_header = auth_header

    def request(self, method, path, params=None, data=None, headers=None, json_body=None) -> Response:
        h = dict(self.headers)
        if self.auth_header:
            h["Authorization"] = self.auth_header()
        h.update(headers or {})
        payload = None
        if json_body is not None:
            payload = json.dumps(json_body, default=str)
            h.setdefault("Content-Type", "application/json")
        try:
            r = self._requests.request(method, self.base_url + path, params=params, data=data if payload is None else payload,
                                       headers=h, timeout=self.timeout)
        except (self._requests.ConnectionError, self._requests.Timeout) as exc:
            raise TransportError(str(exc)) from exc
        try:
            body = decode_json(r.text)
        except ValueError:
            body = r.text
        return Response(r.status_code, body, dict(r.headers), r.text)


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    sleep: Callable[[float], None] = time.sleep

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.max_delay)
            except ValueError:
                pass
        return min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)


@dataclass
class CallStats:
    requests: int = 0
    retries: int = 0


def request_with_retry(transport: Transport, method: str, path: str, *, params=None, data=None,
                       headers=None, json_body=None, policy: RetryPolicy | None = None,
                       stats: CallStats | None = None) -> Response:
    """Send a request, retrying 429/5xx/network errors with bounded backoff.

    Non-retryable 4xx responses raise immediately. After `max_attempts` the
    last error is raised; callers must treat that as "no data", never as zero.
    """
    policy = policy or RetryPolicy()
    stats = stats if stats is not None else CallStats()
    last: Response | None = None
    for attempt in range(1, policy.max_attempts + 1):
        stats.requests += 1
        try:
            resp = transport.request(method, path, params=params, data=data, headers=headers, json_body=json_body)
        except TransportError as exc:
            if attempt == policy.max_attempts:
                raise ApiError(0, str(exc), attempt) from exc
            stats.retries += 1
            policy.sleep(policy.delay(attempt, None))
            continue
        if 200 <= resp.status < 300:
            return resp
        last = resp
        if resp.status not in RETRYABLE_STATUS or attempt == policy.max_attempts:
            raise ApiError(resp.status, resp.body, attempt)
        stats.retries += 1
        retry_after = {k.lower(): v for k, v in resp.headers.items()}.get("retry-after")
        policy.sleep(policy.delay(attempt, retry_after))
    raise ApiError(last.status if last else 0, last.body if last else None, policy.max_attempts)


class InProcessTransport:
    """Calls a handler(method, path, params, headers) directly, no network."""

    def __init__(self, handler: Callable[..., tuple[int, dict, Any]]):
        self.handler = handler

    def request(self, method, path, params=None, data=None, headers=None, json_body=None) -> Response:
        status, hdrs, body = self.handler(method, path, dict(params or {}), dict(headers or {}))
        text = json.dumps(body, default=str)
        return Response(status, decode_json(text), hdrs, text)
