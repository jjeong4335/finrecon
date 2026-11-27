"""FastAPI wrapper around MockAdService (used in Docker deployment).

Run:  uvicorn mock_ad_api.app:app --port 8001
Env:  MOCK_AD_DATA_DIR (default fixtures/offline/ad_platform), MOCK_RUN_NO (default 2)
The X-Demo-Run request header overrides the run number per request.
This service is internal only; it is not exposed through the public proxy.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .service import MockAdService

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MOCK_AD_DATA_DIR", ROOT / "fixtures" / "offline" / "ad_platform"))
service = MockAdService.from_dir(DATA_DIR, run_no=int(os.environ.get("MOCK_RUN_NO", "2")))

app = FastAPI(title="Mock Advertising Platform API",
              description="Synthetic data. Deterministic failures for pipeline testing.")


def _respond(request: Request, path: str) -> JSONResponse:
    status, headers, body = service.handle("GET", path, dict(request.query_params), dict(request.headers))
    return JSONResponse(body, status_code=status, headers=headers)


@app.get("/health")
def health(request: Request):
    return _respond(request, "/health")


@app.get("/clients")
def clients(request: Request, page: int = 1, page_size: int = 50):
    return _respond(request, "/clients")


@app.get("/clients/{client_id}/locations")
def locations(request: Request, client_id: str, page: int = 1, page_size: int = 50):
    return _respond(request, f"/clients/{client_id}/locations")


@app.get("/clients/{client_id}/locations/{location_id}/spend")
def spend(request: Request, client_id: str, location_id: str, start: str, end: str,
          page: int = 1, page_size: int = 50):
    return _respond(request, f"/clients/{client_id}/locations/{location_id}/spend")


@app.get("/clients/{client_id}/locations/{location_id}/summary")
def summary(request: Request, client_id: str, location_id: str, start: str, end: str):
    return _respond(request, f"/clients/{client_id}/locations/{location_id}/summary")
