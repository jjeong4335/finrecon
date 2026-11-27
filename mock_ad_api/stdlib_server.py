"""Dependency-free HTTP server for MockAdService.

Used by the test suite to exercise the live HTTP path (real sockets, real 429
responses, real retries) without FastAPI installed.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from .service import MockAdService


def make_server(service: MockAdService, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            status, headers, body = service.handle("GET", url.path, dict(parse_qsl(url.query)), dict(self.headers))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(service: MockAdService) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"
