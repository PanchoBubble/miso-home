"""One-time local Google Calendar OAuth authorization helper."""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
import webbrowser
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from miso.identity import IdentityError, normalize_email
from miso.tools.google_calendar import (
    GoogleCalendarError,
    GoogleOAuthClient,
    GoogleOAuthSession,
    GoogleTokenStore,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Authorize one Miso user for Google Calendar"
    )
    result.add_argument("--email", required=True, help="Miso user's Google email")
    result.add_argument(
        "--client-file",
        type=Path,
        default=Path("/etc/miso/google-calendar-client.json"),
        help="Google Desktop OAuth client JSON",
    )
    result.add_argument(
        "--token-dir",
        type=Path,
        default=Path("/var/lib/miso/state/google-calendar"),
        help="local directory for restrictive per-user token files",
    )
    result.add_argument(
        "--timeout", type=int, default=300, help="browser authorization timeout"
    )
    result.add_argument(
        "--port",
        type=int,
        default=0,
        help="fixed loopback port for SSH forwarding (default: random)",
    )
    result.add_argument(
        "--no-browser", action="store_true", help="print the URL without opening it"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        email = normalize_email(arguments.email)
        if not arguments.client_file.is_absolute() or not arguments.token_dir.is_absolute():
            raise GoogleCalendarError("client and token paths must be absolute")
        if not 30 <= arguments.timeout <= 900:
            raise GoogleCalendarError("timeout must be between 30 and 900 seconds")
        if not 0 <= arguments.port <= 65535:
            raise GoogleCalendarError("port must be between 0 and 65535")
        oauth = GoogleOAuthSession(
            GoogleOAuthClient.load(arguments.client_file),
            GoogleTokenStore(arguments.token_dir),
        )
    except (GoogleCalendarError, IdentityError) as error:
        print(f"authorization setup error: {error}", file=sys.stderr)
        return 2

    result: dict[str, str] = {}
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            parameters = parse_qs(parsed.query)
            if parsed.path != "/oauth2/callback":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parameters.get("state", [None])[0] != state:
                result["error"] = "OAuth state did not match"
                self._finish(HTTPStatus.BAD_REQUEST, "Authorization could not be verified.")
                return
            error = parameters.get("error", [None])[0]
            code = parameters.get("code", [None])[0]
            if isinstance(error, str):
                result["error"] = f"Google denied authorization: {error}"
                self._finish(HTTPStatus.BAD_REQUEST, "Google Calendar was not authorized.")
                return
            if not isinstance(code, str) or not code:
                result["error"] = "Google did not return an authorization code"
                self._finish(HTTPStatus.BAD_REQUEST, "Authorization code was missing.")
                return
            result["code"] = code
            self._finish(
                HTTPStatus.OK,
                "Google Calendar is connected. You can close this tab.",
            )

        def _finish(self, status: HTTPStatus, message: str) -> None:
            body = (
                "<!doctype html><meta charset=utf-8><title>Miso Calendar</title>"
                f"<p>{message}</p>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", arguments.port), Callback)
    server.timeout = arguments.timeout
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    url = oauth.authorization_url(email, redirect_uri, state, challenge)
    print("Open this Google authorization URL in a browser on this machine:")
    print(url)
    if not arguments.no_browser:
        webbrowser.open(url, new=1, autoraise=True)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        print(
            f"authorization failed: {result.get('error', 'timed out')}",
            file=sys.stderr,
        )
        return 1
    try:
        path = oauth.tokens.path_for(email)
        oauth.exchange_code(
            email,
            result["code"],
            redirect_uri,
            verifier,
        )
    except GoogleCalendarError as error:
        print(f"authorization failed: {error}", file=sys.stderr)
        return 1
    print(f"Google Calendar authorization saved locally at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
