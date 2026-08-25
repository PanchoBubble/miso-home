import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from miso.identity import VOICE_ACTOR, web_actor
from miso.tools import InMemoryAuditLog, ToolRegistry, ToolStatus
from miso.tools.google_calendar import (
    AUTHORIZATION_SCOPES,
    HTTPResponse,
    GoogleCalendarAdapter,
    GoogleCalendarConfig,
    GoogleCalendarError,
    GoogleOAuthClient,
    GoogleOAuthSession,
    GoogleToken,
    GoogleTokenStore,
    register_google_calendar_tools,
)


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def response(status, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    return HTTPResponse(status, body)


class GoogleCalendarToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client_path = self.root / "client.json"
        self.client_path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "123.apps.googleusercontent.com",
                        "client_secret": "client-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            )
        )
        os.chmod(self.client_path, 0o600)
        self.token_dir = self.root / "tokens"
        self.clock = [1000.0]
        self.email = "juan@example.com"
        self.tokens = GoogleTokenStore(self.token_dir)
        self.tokens.save(
            self.email,
            GoogleToken(
                "access-secret",
                "refresh-secret",
                5000,
                tuple(AUTHORIZATION_SCOPES),
            ),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def adapter(self, transport, *, voice_email=None):
        return GoogleCalendarAdapter(
            GoogleCalendarConfig(
                self.client_path,
                self.token_dir,
                default_timezone="Europe/London",
                voice_account_email=voice_email,
            ),
            transport=transport,
            now=lambda: self.clock[0],
        )

    def registry(self, transport, *, voice_email=None, audit=None):
        registry = ToolRegistry(audit)
        register_google_calendar_tools(
            registry,
            GoogleCalendarConfig(
                self.client_path,
                self.token_dir,
                default_timezone="Europe/London",
                voice_account_email=voice_email,
            ),
            transport=transport,
            now=lambda: self.clock[0],
        )
        return registry

    def invoke(self, registry, name, arguments, *, actor=None):
        result = registry.invoke(name, arguments, actor=actor or web_actor(self.email))
        self.assertEqual(result.status, ToolStatus.SUCCESS, result.error)
        return result.output

    def test_registers_bilingual_validated_tool_family(self):
        registry = self.registry(FakeTransport())
        self.assertEqual(
            registry.names(),
            (
                "calendar_event_create",
                "calendar_event_delete",
                "calendar_event_list",
                "calendar_event_update",
                "calendar_list",
            ),
        )
        self.assertIn("crea", registry.get("calendar_event_create").description)
        invalid = registry.invoke(
            "calendar_event_delete",
            {"event_id": "abc", "unexpected": True},
            actor=web_actor(self.email),
        )
        self.assertEqual(invalid.status, ToolStatus.REJECTED)

    def test_lists_calendars_without_returning_credentials(self):
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "items": [
                            {
                                "id": self.email,
                                "summary": "Juan",
                                "timeZone": "Europe/London",
                                "primary": True,
                                "accessRole": "owner",
                            },
                            {"id": "deleted", "deleted": True},
                        ]
                    },
                )
            ]
        )
        audit = InMemoryAuditLog()
        output = self.invoke(
            self.registry(transport, audit=audit),
            "calendar_list",
            {},
        )
        self.assertEqual(output["calendars"][0]["id"], self.email)
        self.assertTrue(output["calendars"][0]["writable"])
        serialized = json.dumps({"output": output, "audit": audit.events()})
        self.assertNotIn("access-secret", serialized)
        self.assertNotIn("refresh-secret", serialized)
        self.assertEqual(
            transport.calls[0]["headers"]["Authorization"],
            "Bearer access-secret",
        )

    def test_creates_timezone_aware_recurring_event(self):
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "id": "event-1",
                        "summary": "Weekly café",
                        "start": {"dateTime": "2026-08-25T18:00:00"},
                        "end": {"dateTime": "2026-08-25T19:00:00"},
                        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
                    },
                )
            ]
        )
        output = self.invoke(
            self.registry(transport),
            "calendar_event_create",
            {
                "summary": "Weekly café",
                "start": "2026-08-25T18:00:00",
                "end": "2026-08-25T19:00:00",
                "time_zone": "Europe/Madrid",
                "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
            },
        )
        self.assertEqual(output["event"]["id"], "event-1")
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent["start"]["timeZone"], "Europe/Madrid")
        self.assertEqual(sent["end"]["dateTime"], "2026-08-25T19:00:00")
        self.assertEqual(sent["recurrence"], ["RRULE:FREQ=WEEKLY;COUNT=4"])

    def test_reads_expanded_events_and_validates_time_range(self):
        transport = FakeTransport(
            [response(200, {"timeZone": "Europe/London", "items": [{"id": "one"}]})]
        )
        output = self.invoke(
            self.registry(transport),
            "calendar_event_list",
            {
                "time_min": "2026-08-25T00:00:00+01:00",
                "time_max": "2026-08-26T00:00:00+01:00",
            },
        )
        query = parse_qs(urlsplit(transport.calls[0]["url"]).query)
        self.assertEqual(query["singleEvents"], ["true"])
        self.assertEqual(query["orderBy"], ["startTime"])
        self.assertEqual(output["events"], [{"id": "one"}])

        invalid = self.registry(FakeTransport()).invoke(
            "calendar_event_list",
            {
                "time_min": "2026-08-26T00:00:00+01:00",
                "time_max": "2026-08-25T00:00:00+01:00",
            },
            actor=web_actor(self.email),
        )
        self.assertEqual(invalid.status, ToolStatus.REJECTED)

    def test_updates_with_fresh_event_and_etag_then_deletes(self):
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "id": "event-1",
                        "etag": '"revision-1"',
                        "summary": "Old",
                        "start": {"dateTime": "2026-08-25T18:00:00+01:00"},
                        "end": {"dateTime": "2026-08-25T19:00:00+01:00"},
                        "reminders": {"useDefault": True},
                    },
                ),
                response(200, {"id": "event-1", "summary": "New"}),
                response(204),
            ]
        )
        registry = self.registry(transport)
        updated = self.invoke(
            registry,
            "calendar_event_update",
            {"event_id": "event-1", "summary": "New"},
        )
        self.assertEqual(updated["event"]["summary"], "New")
        self.assertEqual(transport.calls[1]["method"], "PUT")
        self.assertEqual(transport.calls[1]["headers"]["If-Match"], '"revision-1"')
        sent = json.loads(transport.calls[1]["body"])
        self.assertEqual(sent["summary"], "New")
        self.assertEqual(sent["reminders"], {"useDefault": True})
        deleted = self.invoke(
            registry,
            "calendar_event_delete",
            {"event_id": "event-1"},
        )
        self.assertTrue(deleted["deleted"])

    def test_expired_access_token_refreshes_and_is_saved(self):
        self.tokens.save(
            self.email,
            GoogleToken(
                "expired-secret",
                "refresh-secret",
                900,
                tuple(AUTHORIZATION_SCOPES),
            ),
        )
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "fresh-secret",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": " ".join(AUTHORIZATION_SCOPES),
                    },
                ),
                response(200, {"items": []}),
            ]
        )
        self.invoke(self.registry(transport), "calendar_list", {})
        form = parse_qs(transport.calls[0]["body"].decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["refresh-secret"])
        self.assertEqual(
            transport.calls[1]["headers"]["Authorization"], "Bearer fresh-secret"
        )
        self.assertEqual(self.tokens.load(self.email).access_token, "fresh-secret")

    def test_api_unauthorized_response_forces_one_refresh_and_retry(self):
        transport = FakeTransport(
            [
                response(401, {"error": {"code": 401}}),
                response(
                    200,
                    {
                        "access_token": "retry-secret",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": " ".join(AUTHORIZATION_SCOPES),
                    },
                ),
                response(200, {"items": []}),
            ]
        )
        output = self.invoke(self.registry(transport), "calendar_list", {})
        self.assertEqual(output, {"calendars": []})
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(
            transport.calls[2]["headers"]["Authorization"],
            "Bearer retry-secret",
        )

    def test_revoked_refresh_token_requires_reauthorization_and_is_removed(self):
        self.tokens.save(
            self.email,
            GoogleToken(
                "expired-secret",
                "refresh-secret",
                900,
                tuple(AUTHORIZATION_SCOPES),
            ),
        )
        registry = self.registry(
            FakeTransport([response(400, {"error": "invalid_grant"})])
        )
        result = registry.invoke("calendar_list", {}, actor=web_actor(self.email))
        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertIn("reconnect", result.error)
        self.assertFalse(self.tokens.path_for(self.email).exists())

    def test_voice_requires_explicit_account_mapping(self):
        unavailable = self.registry(FakeTransport()).invoke(
            "calendar_list", {}, actor=VOICE_ACTOR
        )
        self.assertEqual(unavailable.status, ToolStatus.REJECTED)
        transport = FakeTransport([response(200, {"items": []})])
        output = self.invoke(
            self.registry(transport, voice_email=self.email),
            "calendar_list",
            {},
            actor=VOICE_ACTOR,
        )
        self.assertEqual(output, {"calendars": []})

    def test_token_and_client_files_enforce_restrictive_permissions(self):
        os.chmod(self.tokens.path_for(self.email), 0o644)
        with self.assertRaisesRegex(GoogleCalendarError, "permissions"):
            self.tokens.load(self.email)
        os.chmod(self.client_path, 0o644)
        with self.assertRaisesRegex(GoogleCalendarError, "other users"):
            GoogleOAuthClient.load(self.client_path)

    def test_authorization_url_uses_pkce_offline_scopes_without_secret(self):
        oauth = GoogleOAuthSession(
            GoogleOAuthClient.load(self.client_path), self.tokens
        )
        url = oauth.authorization_url(
            self.email,
            "http://127.0.0.1:12345/oauth2/callback",
            "state-value",
            "challenge-value",
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(set(query["scope"][0].split()), set(AUTHORIZATION_SCOPES))
        self.assertNotIn("client-secret", url)

    def test_accepts_google_desktop_client_legacy_authorization_metadata(self):
        payload = json.loads(self.client_path.read_text())
        payload["installed"]["auth_uri"] = "https://accounts.google.com/o/oauth2/auth"
        self.client_path.write_text(json.dumps(payload))
        os.chmod(self.client_path, 0o600)
        client = GoogleOAuthClient.load(self.client_path)
        self.assertEqual(client.client_id, "123.apps.googleusercontent.com")


if __name__ == "__main__":
    unittest.main()
