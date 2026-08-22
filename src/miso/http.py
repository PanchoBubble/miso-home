"""Minimal HTTP surface for service health and deployment testing."""

from __future__ import annotations

import json
import platform
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Type
from urllib.parse import urlsplit

from miso import __version__
from miso.config import Settings


class MisoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_type(started_at: float) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Miso"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if urlsplit(self.path).path != "/healthz":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "miso",
                    "version": __version__,
                    "architecture": platform.machine(),
                    "uptime_seconds": round(time.monotonic() - started_at, 3),
                },
            )

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def create_server(settings: Settings, port: int | None = None) -> MisoHTTPServer:
    return MisoHTTPServer(
        (settings.host, settings.port if port is None else port),
        handler_type(time.monotonic()),
    )
