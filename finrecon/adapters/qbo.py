"""QuickBooks Online Accounting API adapter (sandbox).

Reads use the query endpoint with STARTPOSITION/MAXRESULTS pagination:
    GET /v3/company/{realmId}/query?query=select * from Invoice STARTPOSITION 1 MAXRESULTS 100
Writes (seeding only) POST to /v3/company/{realmId}/{entity}.

OAuth 2.0: access tokens are short-lived and refresh tokens rotate, so every
refresh persists the newest refresh token to QBO_TOKEN_FILE (gitignored; use
AWS Parameter Store in deployment). Credentials are never logged or archived.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..http import ApiError, CallStats, RequestsTransport, RetryPolicy, Transport, request_with_retry

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_MAX_RESULTS = 1000


class QboTokenManager:
    def __init__(self, client_id: str, client_secret: str, token_file: Path):
        if not (client_id and client_secret):
            raise ValueError("QBO_CLIENT_ID and QBO_CLIENT_SECRET are required in live mode")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self._tokens = json.loads(token_file.read_text()) if token_file.exists() else {}
        if "refresh_token" not in self._tokens:
            raise ValueError(f"{token_file} must contain a refresh_token (get one from the Intuit OAuth Playground)")

    def access_token(self, force_refresh: bool = False) -> str:
        if force_refresh or time.time() > self._tokens.get("expires_at", 0) - 60:
            self._refresh()
        return self._tokens["access_token"]

    def _refresh(self) -> None:
        import requests

        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = requests.post(TOKEN_URL, timeout=30, headers={
            "Authorization": f"Basic {basic}", "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self._tokens["refresh_token"]})
        if r.status_code != 200:
            # Intuit's error body names the cause (no secrets in it):
            #   401 invalid_client -> QBO_CLIENT_ID / QBO_CLIENT_SECRET wrong, or from a different app
            #   400 invalid_grant  -> refresh token wrong, expired, already rotated, or issued to another app
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:300]
            hint = {401: "check QBO_CLIENT_ID / QBO_CLIENT_SECRET (Development keys of the same app)",
                    400: "get a new refresh token from the OAuth Playground for this app"}.get(r.status_code, "")
            raise ApiError(r.status_code, f"token refresh failed: {detail} -> {hint}", 1)
        body = r.json()
        self._tokens = {"access_token": body["access_token"], "refresh_token": body["refresh_token"],
                        "expires_at": time.time() + int(body.get("expires_in", 3600))}
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(self._tokens))
        self.token_file.chmod(0o600)


@dataclass
class Page:
    request_key: str
    body: Any


@dataclass
class QboClient:
    transport: Transport
    realm_id: str
    minor_version: str = "75"
    page_size: int = 100
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    stats: CallStats = field(default_factory=CallStats)

    def query_all(self, entity: str) -> tuple[list[dict], list[Page]]:
        """Fetch every row of an entity, following STARTPOSITION pagination."""
        rows: list[dict] = []
        pages: list[Page] = []
        start = 1
        size = min(self.page_size, QBO_MAX_RESULTS)
        while True:
            q = f"select * from {entity} STARTPOSITION {start} MAXRESULTS {size}"
            resp = request_with_retry(self.transport, "GET", f"/v3/company/{self.realm_id}/query",
                                      params={"query": q, "minorversion": self.minor_version},
                                      headers={"Accept": "application/json"}, policy=self.policy, stats=self.stats)
            pages.append(Page(f"{entity}_start{start}", resp.body))
            batch = (resp.body or {}).get("QueryResponse", {}).get(entity, [])
            rows.extend(batch)
            if len(batch) < size:
                return rows, pages
            start += size

    def create(self, entity: str, body: dict) -> dict:
        resp = request_with_retry(self.transport, "POST", f"/v3/company/{self.realm_id}/{entity.lower()}",
                                  params={"minorversion": self.minor_version}, json_body=body,
                                  headers={"Accept": "application/json"}, policy=self.policy, stats=self.stats)
        return resp.body[entity]


class _QboAuthTransport:
    """Adds a bearer token and retries once with a fresh token on HTTP 401."""

    def __init__(self, inner: RequestsTransport, tokens: QboTokenManager):
        self.inner = inner
        self.tokens = tokens

    def request(self, method, path, params=None, data=None, headers=None, json_body=None):
        h = dict(headers or {})
        h["Authorization"] = f"Bearer {self.tokens.access_token()}"
        resp = self.inner.request(method, path, params=params, data=data, headers=h, json_body=json_body)
        if resp.status == 401:
            h["Authorization"] = f"Bearer {self.tokens.access_token(force_refresh=True)}"
            resp = self.inner.request(method, path, params=params, data=data, headers=h, json_body=json_body)
        return resp


def live_client(settings) -> QboClient:
    if not settings.qbo_realm_id:
        raise ValueError("QBO_REALM_ID is required in live mode")
    tokens = QboTokenManager(settings.qbo_client_id, settings.qbo_client_secret, settings.qbo_token_file)
    transport = _QboAuthTransport(RequestsTransport(settings.qbo_base_url), tokens)
    return QboClient(transport, settings.qbo_realm_id, settings.qbo_minor_version)
