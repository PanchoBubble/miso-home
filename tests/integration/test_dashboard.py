import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from miso.access import AccessJWTError
from miso.config import Settings
from miso.http import create_server
from miso.providers import ChatChunk, ProviderHealth, ProviderSet
from miso.routing import ProviderRouter
from miso.tools import InMemoryAuditLog


class FakeProvider:
    def __init__(self, name="pi-ollama", available=True):
        self._name = name
        self.available = available
        self.requests = []

    @property
    def name(self):
        return self._name

    def health(self):
        return ProviderHealth(
            self.available,
            "ready" if self.available else "not_configured",
            "fake-model",
        )

    def stream(self, request, _cancel):
        self.requests.append(request)
        content = request.messages[-1]["content"]
        if "timer" in content.casefold():
            yield ChatChunk(
                tool_call={
                    "name": "timer_create",
                    "arguments": {"duration_seconds": 60, "title": "Dashboard timer"},
                }
            )
        else:
            yield ChatChunk(text="Hello from Miso")
        yield ChatChunk(done=True)


class DashboardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            host="127.0.0.1",
            port=8090,
            database_path=root / "miso.sqlite3",
            state_dir=root,
            model_dir=root,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="qwen3:0.6b",
            provider_timeout_seconds=120,
            log_level="INFO",
        )
        self.pi_provider = FakeProvider()
        router = ProviderRouter(
            ProviderSet(
                pi=self.pi_provider,
                lan=None,
                hosted=FakeProvider("hosted-gpt", available=False),
            ),
            InMemoryAuditLog(),
        )
        self.server = create_server(self.settings, port=0, router=router)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        body = None if payload is None else json.dumps(payload)
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response, content

    def test_dashboard_assets_status_and_sensitive_config_boundary(self) -> None:
        response, content = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Local household assistant", content)
        self.assertIn(b"What can I help with?", content)
        self.assertIn(b'id="side-panel"', content)
        self.assertIn(b'id="turn-progress"', content)
        self.assertIn(b'rel="manifest"', content)
        self.assertIn(b'href="/favicon-32.png"', content)
        self.assertIn(b'id="install-app"', content)
        self.assertIn(b'http://miso.local/', content)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        response, javascript = self.request("GET", "/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b"response.body.getReader", javascript)
        self.assertIn(b"typeof secureCrypto.randomUUID", javascript)
        self.assertIn(b"secureCrypto.getRandomValues", javascript)
        self.assertIn(b"requestSubmit", javascript)
        self.assertIn(b"friendlyToolName", javascript)
        self.assertIn(b"beforeinstallprompt", javascript)
        self.assertIn(b"navigator.serviceWorker.register", javascript)

        response, content = self.request("GET", "/manifest.webmanifest")
        manifest = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"),
            "application/manifest+json; charset=utf-8",
        )
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertIn("192x192", {icon["sizes"] for icon in manifest["icons"]})
        self.assertIn("512x512", {icon["sizes"] for icon in manifest["icons"]})

        response, service_worker = self.request("GET", "/service-worker.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b'url.pathname.startsWith("/api/")', service_worker)
        self.assertIn(b'request.headers.has("Authorization")', service_worker)
        self.assertNotIn(b'caches.match(request)', service_worker)

        response, icon = self.request("GET", "/icon-192.png")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))

        response, favicon = self.request("GET", "/favicon-32.png")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertTrue(favicon.startswith(b"\x89PNG\r\n\x1a\n"))

        response, content = self.request("GET", "/api/status")
        payload = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["providers"][0]["name"], "pi-ollama")
        self.assertIn("timer_create", payload["tools"])
        self.assertIn("capture", payload["audio"])
        self.assertIn("levels", payload["audio"]["capture"])
        self.assertIn("device_losses", payload["audio"]["capture"])
        self.assertFalse(payload["wake"]["enabled"])
        self.assertEqual(payload["wake"]["state"], "disabled")
        self.assertFalse(payload["transcription"]["enabled"])
        self.assertEqual(payload["transcription"]["state"], "disabled")
        self.assertFalse(payload["speech"]["enabled"])
        self.assertEqual(payload["speech"]["state"], "disabled")
        self.assertFalse(payload["conversation"]["enabled"])
        self.assertEqual(payload["conversation"]["state"], "disabled")
        encoded = content.decode()
        self.assertNotIn(str(self.settings.database_path), encoded)
        self.assertNotIn("ollama_url", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

        response, content = self.request("GET", "/api/identity")
        identity = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(identity["actor"]["id"], "local@miso.invalid")
        self.assertEqual(identity["actor"]["source"], "web")
        self.assertEqual(identity["voice_actor"]["id"], "household:voice")

    def test_streamed_routed_tool_chat_is_searchable_and_audited(self) -> None:
        response, content = self.request(
            "POST",
            "/api/chat",
            {
                "text": "Set a timer for one minute",
                "request_id": "dashboard-test",
                "route_class": "auto",
            },
        )
        self.assertEqual(response.status, 200)
        events = [json.loads(line) for line in content.splitlines()]
        self.assertEqual(events[0]["type"], "progress")
        tool = next(item for item in events if item["type"] == "tool_result")
        self.assertTrue(tool["result"]["ok"])
        self.assertEqual(tool["result"]["tool"], "timer_create")
        completed = next(item for item in events if item["type"] == "complete")
        self.assertTrue(completed["conversation_id"])
        self.assertEqual(self.pi_provider.requests[-1].messages[0]["role"], "system")
        system_prompt = self.pi_provider.requests[-1].messages[0]["content"]
        self.assertIn("friendly local household assistant", system_prompt)
        self.assertIn("live weather is not connected", system_prompt)
        self.assertIn("reply 'pong'", system_prompt)

        response, content = self.request("GET", "/api/memory?q=timer")
        results = json.loads(content)["results"]
        self.assertTrue(any("Set a timer" in item["content"] for item in results))
        response, content = self.request("GET", "/api/activity")
        activity = json.loads(content)["events"]
        self.assertTrue(
            any(
                item.get("event") == "tool_invocation_finished"
                and item.get("tool") == "timer_create"
                and item.get("actor") == "local@miso.invalid"
                for item in activity
            )
        )
        with self.server.memory_store.connect() as connection:
            conversation = connection.execute(
                "SELECT visibility, owner_email, created_by FROM conversations "
                "WHERE id = ?",
                (completed["conversation_id"],),
            ).fetchone()
            timer = connection.execute(
                "SELECT visibility, owner_email, created_by FROM scheduled_items"
            ).fetchone()
        expected = ("private", "local@miso.invalid", "local@miso.invalid")
        self.assertEqual(tuple(conversation), expected)
        self.assertEqual(tuple(timer), expected)

    def test_developer_mode_is_visible_expiring_and_command_is_scoped(self) -> None:
        response, content = self.request(
            "POST",
            "/api/developer/command",
            {"command": ["python3", "-c", "print('should-not-run')"]},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(content)["result"]["status"], "rejected")

        response, content = self.request(
            "POST",
            "/api/developer",
            {"action": "enable", "duration_seconds": 2},
        )
        enabled = json.loads(content)["developer_mode"]
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["scope"], str(Path(self.temporary.name).resolve()))

        response, content = self.request(
            "POST",
            "/api/developer/command",
            {"command": ["python3", "-c", "print('dashboard-ok')"]},
        )
        self.assertEqual(response.status, 200)
        result = json.loads(content)["result"]
        self.assertEqual(result["output"]["stdout"].strip(), "dashboard-ok")

        response, content = self.request(
            "POST", "/api/developer", {"action": "disable"}
        )
        self.assertFalse(json.loads(content)["developer_mode"]["enabled"])


class DashboardAuthenticationTests(unittest.TestCase):
    def test_configured_token_protects_api_without_exposing_secret(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                host="127.0.0.1",
                port=8090,
                database_path=root / "miso.sqlite3",
                state_dir=root,
                model_dir=root,
                ollama_url="http://127.0.0.1:11434",
                ollama_model="qwen3:0.6b",
                provider_timeout_seconds=120,
                log_level="INFO",
                dashboard_token="dashboard-secret-at-least-32-chars",
            )
            router = ProviderRouter(
                ProviderSet(
                    pi=FakeProvider(),
                    lan=None,
                    hosted=FakeProvider("hosted-gpt", available=False),
                ),
                InMemoryAuditLog(),
            )
            server = create_server(settings, port=0, router=router)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                connection.close()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                connection.request(
                    "GET",
                    "/api/status",
                    headers={
                        "Authorization": "Bearer dashboard-secret-at-least-32-chars"
                    },
                )
                response = connection.getresponse()
                content = response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"dashboard-secret-at-least-32-chars", content)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_access_assertion_resolves_and_provisions_verified_actor(self) -> None:
        class FakeAccessVerifier:
            def verify(self, assertion: str) -> str:
                if assertion == "allowed-assertion":
                    return "member@example.com"
                if assertion == "unlisted-assertion":
                    return "outsider@example.com"
                if assertion == "malformed-identity":
                    return "not-an-email"
                raise AccessJWTError("invalid assertion")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                host="127.0.0.1",
                port=8090,
                database_path=root / "miso.sqlite3",
                state_dir=root,
                model_dir=root,
                ollama_url="http://127.0.0.1:11434",
                ollama_model="qwen3:0.6b",
                provider_timeout_seconds=120,
                log_level="INFO",
                dashboard_token="dashboard-secret-at-least-32-chars",
            )
            router = ProviderRouter(
                ProviderSet(
                    pi=FakeProvider(),
                    lan=None,
                    hosted=FakeProvider("hosted-gpt", available=False),
                ),
                InMemoryAuditLog(),
            )
            server = create_server(
                settings,
                port=0,
                router=router,
                access_verifier=FakeAccessVerifier(),  # type: ignore[arg-type]
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def identity(headers: dict[str, str]) -> tuple[int, bytes]:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=5
                    )
                    connection.request("GET", "/api/identity", headers=headers)
                    response = connection.getresponse()
                    content = response.read()
                    connection.close()
                    return response.status, content

                status, content = identity(
                    {"Cf-Access-Jwt-Assertion": "allowed-assertion"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(content)["actor"]["id"], "member@example.com"
                )
                status, content = identity(
                    {"Cf-Access-Jwt-Assertion": "unlisted-assertion"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(content)["actor"]["id"], "outsider@example.com"
                )
                with server.memory_store.connect() as connection:
                    provisioned = connection.execute(
                        "SELECT enabled FROM household_members WHERE email = ?",
                        ("outsider@example.com",),
                    ).fetchone()
                self.assertEqual(provisioned["enabled"], 1)
                with self.assertLogs("miso.http", level="WARNING") as captured:
                    status, _ = identity(
                        {"Cf-Access-Jwt-Assertion": "malformed-identity"}
                    )
                    self.assertEqual(status, 401)
                    status, _ = identity(
                        {"Cf-Access-Authenticated-User-Email": "member@example.com"}
                    )
                    self.assertEqual(status, 401)
                diagnostic = "\n".join(captured.output)
                self.assertIn("identity email is invalid", diagnostic)
                self.assertIn("invalid assertion", diagnostic)
                self.assertNotIn("unlisted-assertion", diagnostic)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
