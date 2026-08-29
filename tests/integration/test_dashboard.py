import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from miso.access import AccessJWTError
from miso.config import Settings
from miso.http import create_server
from miso.identity import VOICE_ACTOR, web_actor
from miso.providers import ChatChunk, ProviderHealth, ProviderSet
from miso.routing import ProviderRouter
from miso.tools import InMemoryAuditLog, ToolDefinition


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
        if "summary timer" in content.casefold():
            yield ChatChunk(
                tool_call={"name": "timer_summary", "arguments": {}}
            )
        elif "timer" in content.casefold():
            yield ChatChunk(
                tool_call={
                    "name": "timer_create",
                    "arguments": {"duration_seconds": 60, "title": "Dashboard timer"},
                }
            )
        else:
            yield ChatChunk(text="Hello from Miso")
        yield ChatChunk(done=True)


class FakeWakeCalibration:
    def __init__(self):
        self.calls = 0

    def capture(self):
        self.calls += 1
        return {
            "recognized_text": "Me so.",
            "language": "en",
            "confidence": 0.8,
            "duration_milliseconds": 5000,
            "peak_dbfs": -20.0,
            "rms_dbfs": -30.0,
            "raw_audio_retained": False,
        }


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
        self.hosted_provider = FakeProvider("hosted-gpt", available=False)
        self.wake_calibration = FakeWakeCalibration()
        router = ProviderRouter(
            ProviderSet(
                pi=self.pi_provider,
                lan=None,
                hosted=self.hosted_provider,
            ),
            InMemoryAuditLog(),
        )
        self.server = create_server(
            self.settings,
            port=0,
            router=router,
            wake_calibration=self.wake_calibration,
        )
        self.server.tool_registry.register(
            ToolDefinition(
                name="timer_summary",
                description="Return a speakable timer summary.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=lambda _arguments, _context: {
                    "summary": "The weather is cloudy and 18 degrees."
                },
            )
        )
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
        self.assertIn(b'id="remember-form"', content)
        self.assertIn(b'id="prune-form"', content)
        self.assertIn(b'id="export-memory"', content)
        self.assertIn(b'id="household-view"', content)
        self.assertIn(b'id="shopping-form"', content)
        self.assertIn(b'id="reminder-form"', content)
        self.assertIn(b'id="message-form"', content)
        self.assertIn(b'id="notification-inbox"', content)
        self.assertIn(b'id="start-wake-calibration"', content)
        self.assertIn(b'http://miso.local/', content)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIn(
            "script-src 'self' 'wasm-unsafe-eval'",
            response.getheader("Content-Security-Policy"),
        )
        response, javascript = self.request("GET", "/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b"response.body.getReader", javascript)
        self.assertIn(b"typeof secureCrypto.randomUUID", javascript)
        self.assertIn(b"secureCrypto.getRandomValues", javascript)
        self.assertIn(b"requestSubmit", javascript)
        self.assertIn(b"friendlyToolName", javascript)
        self.assertIn(b"beforeinstallprompt", javascript)
        self.assertIn(b"navigator.serviceWorker.register", javascript)
        self.assertIn(b"preview_prune", javascript)
        self.assertIn(b"deleteSelectedMemory", javascript)
        self.assertIn(b"/api/events?after=", javascript)
        self.assertIn(b"/api/notifications?limit=100", javascript)
        self.assertNotIn(b"setInterval", javascript)

        response, content = self.request("GET", "/manifest.webmanifest")
        manifest = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"),
            "application/manifest+json; charset=utf-8",
        )
        self.assertEqual(manifest["display"], "fullscreen")
        self.assertEqual(manifest["display_override"][0], "fullscreen")
        self.assertEqual(manifest["start_url"], "/")
        self.assertIn("192x192", {icon["sizes"] for icon in manifest["icons"]})
        self.assertIn("512x512", {icon["sizes"] for icon in manifest["icons"]})

        response, service_worker = self.request("GET", "/service-worker.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b'url.pathname.startsWith("/api/")', service_worker)
        self.assertIn(b'request.headers.has("Authorization")', service_worker)
        self.assertIn(b'miso-shell-v12', service_worker)
        self.assertIn(b'"/companion"', service_worker)
        self.assertIn(b'"/assets/miso-face.riv"', service_worker)
        self.assertIn(b'"/vendor/rive/rive.wasm"', service_worker)
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

    def test_companion_face_assets_are_local_and_privacy_bounded(self) -> None:
        response, content = self.request("GET", "/companion")
        self.assertEqual(response.status, 200)
        self.assertIn(b'id="rive-face"', content)
        self.assertIn(b'id="fallback-face"', content)
        self.assertIn(b'/vendor/rive/rive.js', content)
        self.assertIn(b'/companion.js?v=12', content)
        self.assertIn(b'/companion.css?v=12', content)
        self.assertIn(b'id="companion-caption"', content)
        self.assertNotIn(b'https://', content)

        response, javascript = self.request("GET", "/companion.js")
        self.assertEqual(response.status, 200)
        self.assertIn(b'RuntimeLoader.setWasmUrl("/vendor/rive/rive.wasm")', javascript)
        self.assertIn(b'RuntimeLoader.setWasmFallbackUrl', javascript)
        self.assertIn(b"RIVE_MAX_RENDER_PIXELS", javascript)
        self.assertIn(b'stateMachineInputs(RIVE_STATE_MACHINE)', javascript)
        self.assertIn(b'/api/events?after=', javascript)
        self.assertIn(b'assistant_caption', javascript)
        self.assertIn(b'captionCopy.textContent = normalized', javascript)
        self.assertIn(b'caption.dataset.captionState', javascript)

        response, companion = self.request("GET", "/companion")
        self.assertEqual(response.status, 200)
        self.assertIn(b'href="/"', companion)

        response, dashboard = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b'href="/companion"', dashboard)
        self.assertNotIn(b'innerHTML', javascript)
        self.assertNotIn(b"setInterval", javascript)

        response, asset = self.request("GET", "/assets/miso-face.riv")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(asset.startswith(b"RIVE"))

        response, wasm = self.request("GET", "/vendor/rive/rive.wasm")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/wasm")
        self.assertTrue(wasm.startswith(b"\x00asm"))

    def test_live_events_replay_missed_notifications_and_stream_without_polling(
        self,
    ) -> None:
        first = self.server.live_events.publish(
            "assistant_state", {"state": "listening"}, actor=VOICE_ACTOR
        )
        second = self.server.live_events.publish(
            "scheduled_item_due",
            {"kind": "timer", "title": "Tea"},
            actor=VOICE_ACTOR,
        )

        response, content = self.request(
            "GET", f"/api/notifications?after={first.event_id}&limit=10"
        )
        payload = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [event["id"] for event in payload["events"]], [second.event_id]
        )
        self.assertEqual(payload["events"][0]["payload"]["title"], "Tea")

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        connection.request("GET", f"/api/events?after={first.event_id}")
        stream = connection.getresponse()
        self.assertEqual(stream.status, 200)
        self.assertEqual(
            stream.getheader("Content-Type"), "text/event-stream; charset=utf-8"
        )
        lines = []
        while len(lines) < 8:
            line = stream.readline().decode("utf-8")
            lines.append(line)
            if line == "\n" and any(value.startswith("data:") for value in lines):
                break
        connection.close()
        self.assertIn(f"id: {second.event_id}\n", lines)
        data = next(value for value in lines if value.startswith("data:"))
        streamed = json.loads(data.removeprefix("data:").strip())
        self.assertEqual(streamed["type"], "scheduled_item_due")

    def test_wake_calibration_requires_consent_and_returns_no_retained_audio(self):
        response, content = self.request(
            "POST",
            "/api/wake-calibration",
            {"action": "capture", "consent": False},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            json.loads(content)["error"], "wake_calibration_consent_required"
        )

        response, content = self.request(
            "POST",
            "/api/wake-calibration",
            {"action": "capture", "consent": True},
        )
        payload = json.loads(content)["calibration"]
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["recognized_text"], "Me so.")
        self.assertFalse(payload["raw_audio_retained"])
        self.assertEqual(self.wake_calibration.calls, 1)

    def test_household_and_tool_mutations_publish_durable_safe_events(self) -> None:
        response, _ = self.request(
            "POST",
            "/api/household",
            {
                "action": "message_create",
                "content": "Private medical appointment",
                "visibility": "private",
            },
        )
        self.assertEqual(response.status, 201)
        response, _ = self.request(
            "POST",
            "/api/household",
            {
                "action": "timer_create",
                "title": "Tea",
                "duration_seconds": 60,
                "visibility": "shared",
            },
        )
        self.assertEqual(response.status, 201)

        response, content = self.request("GET", "/api/notifications?limit=20")
        events = json.loads(content)["events"]
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [event["type"] for event in events],
            [
                "household_message_created",
                "tool_outcome",
                "household_changed",
            ],
        )
        encoded = json.dumps(events)
        self.assertNotIn("Private medical appointment", encoded)
        self.assertNotIn("output", events[1]["payload"])

    def test_household_views_share_live_state_and_reject_stale_edits(self) -> None:
        response, content = self.request(
            "POST",
            "/api/household",
            {
                "action": "shopping_add",
                "list_name": "Shopping",
                "name": "Coffee",
                "quantity": 2,
                "shared": True,
            },
        )
        self.assertEqual(response.status, 201)
        created = json.loads(content)["item"]
        self.assertEqual(created["added_by"], "local@miso.invalid")

        voice = self.server.tool_registry.invoke(
            "shopping_add",
            {"list_name": "Shopping", "name": "Bread"},
            actor=VOICE_ACTOR,
        )
        self.assertTrue(voice.ok)

        response, content = self.request("GET", "/api/household")
        state = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(state["actor"]["id"], "local@miso.invalid")
        self.assertEqual(
            {item["added_by"] for item in state["lists"][0]["items"]},
            {"local@miso.invalid", "household:voice"},
        )

        response, content = self.request(
            "POST",
            "/api/household",
            {
                "action": "shopping_update",
                "id": created["id"],
                "name": "Decaf coffee",
                "expected_revision": created["revision"],
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(content)["item"]["revision"], 2)
        response, content = self.request(
            "POST",
            "/api/household",
            {
                "action": "shopping_update",
                "id": created["id"],
                "name": "Stale coffee",
                "expected_revision": created["revision"],
            },
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(content)["error"], "revision_conflict")

        response, content = self.request(
            "POST",
            "/api/household",
            {
                "action": "reminder_create",
                "title": "Put the bins out",
                "due_at": "2030-08-25T19:00:00+01:00",
                "visibility": "private",
            },
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(json.loads(content)["reminder"]["visibility"], "private")

        response, _ = self.request(
            "POST",
            "/api/household",
            {
                "action": "message_create",
                "content": "Dinner is at seven",
                "visibility": "shared",
            },
        )
        self.assertEqual(response.status, 201)
        response, content = self.request("GET", "/api/household")
        refreshed = json.loads(content)
        self.assertEqual(refreshed["messages"][0]["content"], "Dinner is at seven")
        self.assertEqual(refreshed["reminders"][0]["title"], "Put the bins out")

        response, content = self.request("GET", "/api/activity")
        activity = json.loads(content)["events"]
        self.assertTrue(
            any(
                item.get("tool") == "shopping_update"
                and item.get("actor") == "local@miso.invalid"
                for item in activity
            )
        )

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

    def test_speakable_tool_summary_is_streamed_and_persisted(self) -> None:
        response, content = self.request(
            "POST",
            "/api/chat",
            {
                "text": "Run the summary timer tool",
                "request_id": "tool-summary-test",
                "route_class": "auto",
            },
        )
        self.assertEqual(response.status, 200)
        events = [json.loads(line) for line in content.splitlines()]
        tool = next(item for item in events if item["type"] == "tool_result")
        delta = next(item for item in events if item["type"] == "delta")
        self.assertEqual(tool["result"]["tool"], "timer_summary")
        self.assertEqual(delta["text"], "The weather is cloudy and 18 degrees.")
        completed = next(item for item in events if item["type"] == "complete")
        messages = self.server.memory_store.events(
            completed["conversation_id"], actor=web_actor(self.settings.dashboard_email)
        )
        self.assertEqual(messages[-1].content, delta["text"])

    def test_chat_without_matching_tool_uses_hosted_provider(self) -> None:
        self.hosted_provider.available = True
        response, content = self.request(
            "POST",
            "/api/chat",
            {
                "text": "Explain why the sky is blue",
                "request_id": "hosted-routing-test",
                "route_class": "auto",
            },
        )
        self.assertEqual(response.status, 200)
        events = [json.loads(line) for line in content.splitlines()]
        delta = next(item for item in events if item["type"] == "delta")
        self.assertEqual(delta["provider"], "hosted-gpt")
        self.assertEqual(len(self.hosted_provider.requests), 1)
        self.assertEqual(self.hosted_provider.requests[0].tools, ())
        self.assertEqual(self.pi_provider.requests, [])

    def test_memory_management_api_remembers_exports_previews_and_deletes(self) -> None:
        response, content = self.request(
            "POST",
            "/api/memory",
            {
                "action": "remember",
                "content": "Keep the spare key in the blue drawer",
                "tags": ["Household", "Key"],
                "visibility": "private",
            },
        )
        created = json.loads(content)["record"]
        self.assertEqual(response.status, 201)
        self.assertEqual(created["kind"], "explicit")
        self.assertEqual(created["importance"], 1.0)
        self.assertEqual(created["tags"], ["household", "key"])
        self.assertEqual(created["visibility"], "private")

        response, content = self.request("GET", "/api/memory?kind=explicit")
        results = json.loads(content)["results"]
        self.assertEqual(response.status, 200)
        self.assertEqual([item["record_id"] for item in results], [created["record_id"]])

        response, content = self.request(
            "POST",
            "/api/memory",
            {
                "action": "update",
                "record_id": created["record_id"],
                "importance": 0.75,
                "tags": ["household", "security"],
            },
        )
        updated = json.loads(content)["record"]
        self.assertEqual(response.status, 200)
        self.assertEqual(updated["importance"], 0.75)
        self.assertEqual(updated["tags"], ["household", "security"])

        response, content = self.request("GET", "/api/memory/export")
        exported = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(len(exported["records"]), 1)
        self.assertEqual(exported["actor"]["id"], "local@miso.invalid")

        response, content = self.request(
            "POST",
            "/api/memory",
            {"action": "preview_prune", "topic": "drawer"},
        )
        preview = json.loads(content)
        self.assertEqual(response.status, 200)
        self.assertEqual(preview["impact"]["records"], 1)
        self.assertEqual(preview["candidates"][0]["record_id"], created["record_id"])

        response, content = self.request(
            "POST",
            "/api/memory",
            {
                "action": "delete",
                "records": [
                    {
                        "record_type": "memory",
                        "record_id": created["record_id"],
                    }
                ],
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(content)["deleted"]["records"], 1)
        response, content = self.request("GET", "/api/memory?q=drawer")
        self.assertEqual(json.loads(content)["results"], [])

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
