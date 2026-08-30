"""Local dashboard, streaming chat, and operator API."""

from __future__ import annotations

import hmac
import json
import logging
import platform
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Type, cast
from urllib.parse import parse_qs, urlsplit

from miso import __version__
from miso.access import AccessJWTError, AccessJWTVerifier
from miso.audio import AudioManager, PulseProxyBackend
from miso.calibration import (
    WakeCalibration,
    WakeCalibrationBusy,
    WakeCalibrationComplete,
    WakeCalibrationError,
)
from miso.buttons import ButtonManager, ButtonRouter
from miso.config import Settings
from miso.conversation import ConversationManager
from miso.intake import FastLane, guess_language
from miso.display import DisplayWakeNotifier
from miso.identity import (
    Actor,
    HouseholdIdentityPolicy,
    IdentityError,
    SYSTEM_ACTOR,
    VOICE_ACTOR,
)
from miso.live_events import (
    LiveAuditSink,
    LiveEventStore,
    LiveToolResultPublisher,
    conversation_caption_publisher,
    conversation_error_publisher,
    conversation_event_publisher,
    user_capture_publisher,
    user_transcript_publisher,
)
from miso.memory import MemoryStore, SearchResult, utc_now
from miso.providers import ChatRequest, ProviderCancelled
from miso.providers.ollama import OllamaProvider
from miso.routing import (
    PROVIDER_PREFERENCE,
    ProviderRouter,
    RoutingError,
    create_router,
)
from miso.speech import PiperBackend, PiperVoice, SpeechError, SpeechManager
from miso.toolpick import ToolPicker
from miso.transcription import (
    EnergySpeechDetector,
    TranscriptionManager,
    UtteranceAssembler,
    WhisperCppTranscriber,
)
from miso.wake import OpenWakeWordModel, WakeWordManager
from miso.tools import (
    DeveloperShellController,
    GoogleCalendarConfig,
    HouseholdStore,
    REFRESH_TOOL_NAME,
    ScheduledItemWorker,
    ToolDirectoryLoader,
    ToolRegistry,
    WeatherConfig,
    create_runtime_registry,
)
from miso.tools.audit import JsonlAuditLog, audit_event

MISO_SYSTEM_PROMPT = (
    "You are Miso, a friendly local household assistant. Answer ordinary questions "
    "and conversation directly, naturally, and briefly. Your available actions are "
    "exactly the tools included in the current request; use a tool when it matches "
    "the user's request. You have no live weather, web, news, location, or sensor "
    "data unless a matching tool is included. Never invent current information. If "
    "asked about weather without a weather tool, say that live weather is not "
    "connected yet. If the user sends 'ping' as a connectivity check, reply 'pong'. "
    "Never describe ordinary harmless conversation as forbidden or not permitted."
)

WEB_ROOT = Path(__file__).with_name("web")
MAX_BODY_BYTES = 65536
STATIC_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/companion": "companion.html",
    "/companion.html": "companion.html",
    "/companion.css": "companion.css",
    "/companion.js": "companion.js",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/service-worker.js": "service-worker.js",
    "/favicon-32.png": "favicon-32.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/icon-maskable-512.png": "icon-maskable-512.png",
    "/assets/miso-face.riv": "assets/miso-face.riv",
    "/vendor/rive/rive.js": "vendor/rive/rive.js",
    "/vendor/rive/rive.wasm": "vendor/rive/rive.wasm",
    "/vendor/rive/rive_fallback.wasm": "vendor/rive/rive_fallback.wasm",
}
LOGGER = logging.getLogger("miso.http")


class MisoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: Type[BaseHTTPRequestHandler],
        *,
        settings: Settings,
        tool_registry: ToolRegistry,
        scheduled_worker: ScheduledItemWorker,
        router: ProviderRouter,
        developer_shell: DeveloperShellController,
        audio_manager: AudioManager,
        transcription_manager: TranscriptionManager,
        speech_manager: SpeechManager,
        wake_manager: WakeWordManager,
        wake_calibration: WakeCalibration,
        conversation_manager: ConversationManager,
        button_manager: ButtonManager,
        live_events: LiveEventStore,
        identity_policy: HouseholdIdentityPolicy,
        access_verifier: AccessJWTVerifier | None,
        started_at: float,
        fast_lane: FastLane | None = None,
        tool_loader: ToolDirectoryLoader | None = None,
        tool_picker: ToolPicker | None = None,
    ) -> None:
        self.settings = settings
        self.tool_registry = tool_registry
        self.tool_loader = tool_loader
        self.scheduled_worker = scheduled_worker
        self.router = router
        self.developer_shell = developer_shell
        self.audio_manager = audio_manager
        self.transcription_manager = transcription_manager
        self.speech_manager = speech_manager
        self.wake_manager = wake_manager
        self.wake_calibration = wake_calibration
        self.conversation_manager = conversation_manager
        self.button_manager = button_manager
        self.fast_lane = fast_lane
        self.tool_picker = tool_picker
        self.live_events = live_events
        self.memory_store = MemoryStore(settings.database_path)
        self.household_store = HouseholdStore(settings.database_path)
        self.identity_policy = identity_policy
        self.access_verifier = access_verifier
        self._member_lock = threading.Lock()
        self._registered_members = {identity_policy.local_dashboard_email}
        self.memory_store.provision_household_members(self._registered_members)
        self.started_at = started_at
        self._active_requests: dict[str, tuple[threading.Event, str]] = {}
        self._active_lock = threading.Lock()
        super().__init__(server_address, request_handler)

    def access_actor(self, email: str) -> Actor:
        actor = self.identity_policy.web_actor(email)
        with self._member_lock:
            if actor.actor_id not in self._registered_members:
                self.memory_store.provision_household_members((actor.actor_id,))
                self._registered_members.add(actor.actor_id)
        return actor

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.scheduled_worker.start()
        self.audio_manager.start()
        self.wake_manager.start()
        self.transcription_manager.start()
        self.speech_manager.start()
        self.conversation_manager.start()
        self.button_manager.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.button_manager.stop()
            self.conversation_manager.stop()
            self.speech_manager.stop()
            self.transcription_manager.stop()
            self.wake_manager.stop()
            self.audio_manager.stop()
            self.scheduled_worker.stop()

    def server_close(self) -> None:
        self.button_manager.stop()
        self.conversation_manager.stop()
        self.speech_manager.stop()
        self.transcription_manager.stop()
        self.wake_manager.stop()
        self.audio_manager.stop()
        self.scheduled_worker.stop()
        self.live_events.close()
        super().server_close()

    def register_request(
        self, request_id: str, cancel: threading.Event, actor: Actor
    ) -> bool:
        with self._active_lock:
            if request_id in self._active_requests:
                return False
            self._active_requests[request_id] = (cancel, actor.actor_id)
            return True

    def unregister_request(self, request_id: str) -> None:
        with self._active_lock:
            self._active_requests.pop(request_id, None)

    def cancel_request(self, request_id: str, actor: Actor) -> bool:
        with self._active_lock:
            active = self._active_requests.get(request_id)
        if active is None or active[1] != actor.actor_id:
            return False
        active[0].set()
        return True


def handler_type() -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Miso"
        sys_version = ""

        @property
        def miso(self) -> MisoHTTPServer:
            return cast(MisoHTTPServer, self.server)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "miso",
                        "version": __version__,
                        "architecture": platform.machine(),
                        "uptime_seconds": round(
                            time.monotonic() - self.miso.started_at, 3
                        ),
                    },
                )
                return
            if parsed.path in STATIC_ASSETS:
                self._static(parsed.path)
                return
            if not parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                return
            if parsed.path == "/api/status":
                self._status()
            elif parsed.path == "/api/events":
                self._events(parsed.query)
            elif parsed.path == "/api/notifications":
                self._notifications(parsed.query)
            elif parsed.path == "/api/identity":
                self._identity()
            elif parsed.path == "/api/memory":
                parameters = parse_qs(parsed.query)
                self._memory(
                    parameters.get("q", [""])[0],
                    kind=parameters.get("kind", [""])[0],
                    tag=parameters.get("tag", [""])[0],
                    record_type=parameters.get("record_type", [""])[0],
                )
            elif parsed.path == "/api/memory/export":
                self._memory_export()
            elif parsed.path == "/api/activity":
                raw_limit = parse_qs(parsed.query).get("limit", ["50"])[0]
                try:
                    limit = max(1, min(int(raw_limit), 100))
                except ValueError:
                    limit = 50
                self._activity(limit)
            elif parsed.path == "/api/tools":
                self._tools()
            elif parsed.path == "/api/household":
                self._household()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if not parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                return
            try:
                payload = self._request_json()
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            if parsed.path == "/api/chat":
                self._chat(payload)
            elif parsed.path == "/api/chat/cancel":
                request_id = payload.get("request_id")
                if not isinstance(request_id, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request_id"})
                else:
                    self._json(
                        HTTPStatus.OK,
                        {"cancelled": self.miso.cancel_request(request_id, self._actor())},
                    )
            elif parsed.path == "/api/tools/refresh":
                self._tools_refresh(payload)
            elif parsed.path == "/api/developer":
                self._developer(payload)
            elif parsed.path == "/api/developer/command":
                self._developer_command(payload)
            elif parsed.path == "/api/speech":
                self._speech(payload)
            elif parsed.path == "/api/speech/cancel":
                request_id = payload.get("request_id")
                if request_id is not None and not isinstance(request_id, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request_id"})
                else:
                    self._json(
                        HTTPStatus.OK,
                        {"cancelled": self.miso.speech_manager.cancel(request_id)},
                    )
            elif parsed.path == "/api/wake-calibration":
                self._wake_calibration(payload)
            elif parsed.path == "/api/memory":
                self._memory_action(payload)
            elif parsed.path == "/api/household":
                self._household_action(payload)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def _authorized(self) -> bool:
            expected = self.miso.settings.dashboard_token
            if expected is None:
                self._request_actor = self.miso.identity_policy.local_actor
                return True
            provided = self.headers.get("Authorization", "")
            if provided.startswith("Bearer ") and hmac.compare_digest(
                provided[7:], expected
            ):
                self._request_actor = self.miso.identity_policy.local_actor
                return True
            if self.miso.access_verifier is not None:
                assertion = self.headers.get("Cf-Access-Jwt-Assertion", "")
                try:
                    email = self.miso.access_verifier.verify(assertion)
                    self._request_actor = self.miso.access_actor(email)
                    return True
                except AccessJWTError as error:
                    LOGGER.warning("Cloudflare Access assertion rejected: %s", error)
                except IdentityError:
                    LOGGER.warning("Cloudflare Access identity email is invalid")
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        def _wake_calibration(self, payload: dict[str, object]) -> None:
            if payload.get("action") != "capture" or payload.get("consent") is not True:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "wake_calibration_consent_required"},
                )
                return
            try:
                result = self.miso.wake_calibration.capture()
            except WakeCalibrationBusy as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            except WakeCalibrationComplete as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            except WakeCalibrationError as error:
                status = (
                    HTTPStatus.GATEWAY_TIMEOUT
                    if str(error) == "wake_calibration_audio_timeout"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._json(status, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, {"calibration": result})

        def _actor(self) -> Actor:
            actor = getattr(self, "_request_actor", None)
            if not isinstance(actor, Actor):
                raise RuntimeError("request identity was not resolved")
            return actor

        def _identity(self) -> None:
            self._json(
                HTTPStatus.OK,
                {
                    "actor": self._actor().public_dict(),
                    "voice_actor": VOICE_ACTOR.public_dict(),
                },
            )

        def _notifications(self, query: str) -> None:
            parameters = parse_qs(query)
            try:
                limit = _bounded_integer(
                    parameters.get("limit", ["50"])[0],
                    minimum=1,
                    maximum=200,
                    error="invalid_notification_limit",
                )
                after_values = parameters.get("after")
                if after_values:
                    after = _bounded_integer(
                        after_values[0],
                        minimum=0,
                        maximum=2**63 - 1,
                        error="invalid_event_cursor",
                    )
                    events = self.miso.live_events.after(
                        after, actor=self._actor(), limit=limit
                    )
                else:
                    events = self.miso.live_events.recent(
                        actor=self._actor(), limit=limit
                    )
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "events": [event.as_dict() for event in events],
                    "cursor": events[-1].event_id if events else 0,
                },
            )

        def _events(self, query: str) -> None:
            parameters = parse_qs(query)
            raw_cursor = parameters.get(
                "after", [self.headers.get("Last-Event-ID", "0")]
            )[0]
            try:
                cursor = _bounded_integer(
                    raw_cursor,
                    minimum=0,
                    maximum=2**63 - 1,
                    error="invalid_event_cursor",
                )
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            actor = self._actor()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 3000\n: connected\n\n")
                self.wfile.flush()
                while not self.miso.live_events.closed:
                    events = self.miso.live_events.wait_after(
                        cursor,
                        actor=actor,
                        timeout=15,
                        limit=100,
                    )
                    if not events:
                        if self.miso.live_events.closed:
                            break
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for event in events:
                        encoded = json.dumps(
                            event.as_dict(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        self.wfile.write(
                            f"id: {event.event_id}\n".encode("ascii")
                            + f"event: {event.event_type}\n".encode("ascii")
                            + b"data: "
                            + encoded
                            + b"\n\n"
                        )
                        cursor = event.event_id
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return

        def _status(self) -> None:
            self._json(
                HTTPStatus.OK,
                {
                    "service": {
                        "status": "ok",
                        "version": __version__,
                        "architecture": platform.machine(),
                        "uptime_seconds": round(
                            time.monotonic() - self.miso.started_at, 3
                        ),
                    },
                    "providers": self.miso.router.health_snapshot(),
                    "routing": {
                        "matched_tool": [
                            "pi-ollama",
                            "lan-ollama",
                            "hosted-gpt",
                        ],
                        "no_matching_tool": list(PROVIDER_PREFERENCE),
                    },
                    "tools": list(self.miso.tool_registry.names()),
                    "audio": self.miso.audio_manager.status(),
                    "wake": self.miso.wake_manager.status(),
                    "transcription": self.miso.transcription_manager.status(),
                    "speech": self.miso.speech_manager.status(),
                    "conversation": self.miso.conversation_manager.status(),
                    "buttons": self.miso.button_manager.status(),
                    "developer_mode": self.miso.developer_shell.status(),
                },
            )

        def _household(self) -> None:
            actor = self._actor()
            messages = self.miso.memory_store.search(
                "",
                limit=50,
                actor=actor,
                kinds=("explicit",),
                tag="household-message",
                record_types=("memory",),
            )
            self._json(
                HTTPStatus.OK,
                {
                    "actor": actor.public_dict(),
                    "lists": self.miso.household_store.list_shopping_lists(
                        actor=actor
                    ),
                    "timers": self.miso.household_store.list_scheduled(
                        "timer", "all", actor=actor
                    ),
                    "reminders": self.miso.household_store.list_scheduled(
                        "reminder", "all", actor=actor
                    ),
                    "messages": [
                        _memory_result_payload(item) for item in messages
                    ],
                    "refreshed_at": utc_now(),
                },
            )

        def _household_action(self, payload: dict[str, object]) -> None:
            action = payload.get("action")
            actor = self._actor()
            tool_name = ""
            arguments: dict[str, object] = {}
            output_key = ""
            try:
                if action == "shopping_add":
                    tool_name = "shopping_add"
                    output_key = "item"
                    arguments = _copy_fields(
                        payload,
                        ("list_name", "name", "quantity", "shared"),
                    )
                elif action == "shopping_update":
                    tool_name = "shopping_update"
                    output_key = "item"
                    arguments = _copy_fields(
                        payload,
                        ("id", "name", "quantity", "completed", "expected_revision"),
                    )
                elif action == "shopping_remove":
                    tool_name = "shopping_remove"
                    output_key = "item"
                    arguments = _copy_fields(
                        payload, ("id", "expected_revision")
                    )
                elif action in {"timer_create", "reminder_create"}:
                    tool_name = cast(str, action)
                    output_key = "timer" if action == "timer_create" else "reminder"
                    arguments = _copy_fields(
                        payload,
                        ("title", "duration_seconds", "due_at", "visibility"),
                    )
                elif action in {
                    "timer_update", "reminder_update",
                    "timer_cancel", "reminder_cancel",
                }:
                    tool_name = cast(str, action)
                    output_key = "timer" if str(action).startswith("timer") else "reminder"
                    arguments = _copy_fields(
                        payload,
                        ("id", "title", "duration_seconds", "due_at", "expected_revision"),
                    )
                elif action == "message_create":
                    content = payload.get("content")
                    visibility = payload.get("visibility", "shared")
                    if (
                        not isinstance(content, str)
                        or not 1 <= len(content.strip()) <= 2000
                    ):
                        raise ValueError("invalid_message_content")
                    if visibility not in {"shared", "private"}:
                        raise ValueError("invalid_visibility")
                    record_id = self.miso.memory_store.add_memory(
                        content.strip(),
                        kind="explicit",
                        importance=0.7,
                        tags=("household-message",),
                        actor=actor,
                        visibility=cast(str, visibility),
                    )
                    self.miso.tool_registry.audit_sink.record(
                        audit_event(
                            "household_message_created",
                            record_id=record_id,
                            visibility=visibility,
                            actor=actor.actor_id,
                            actor_source=actor.source,
                        )
                    )
                    self._publish_live(
                        "household_message_created",
                        {"record_id": record_id},
                        actor=actor,
                        visibility=cast(str, visibility),
                        owner_email=(
                            actor.email if visibility == "private" else None
                        ),
                    )
                    self._json(HTTPStatus.CREATED, {"record_id": record_id})
                    return
                elif action == "message_delete":
                    record_id = _record_id(payload.get("record_id"))
                    try:
                        record = self._memory_record(record_id)
                    except StopIteration:
                        self._json(
                            HTTPStatus.NOT_FOUND, {"error": "message_not_found"}
                        )
                        return
                    deleted = self.miso.memory_store.delete_records(
                        (("memory", record_id),), actor=actor
                    )
                    if deleted.records_deleted != 1:
                        self._json(
                            HTTPStatus.NOT_FOUND, {"error": "message_not_found"}
                        )
                        return
                    self.miso.tool_registry.audit_sink.record(
                        audit_event(
                            "household_message_deleted",
                            record_id=record_id,
                            actor=actor.actor_id,
                            actor_source=actor.source,
                        )
                    )
                    self._publish_live(
                        "household_message_deleted",
                        {"record_id": record_id},
                        actor=actor,
                        visibility=record.visibility,
                        owner_email=(
                            actor.email if record.visibility == "private" else None
                        ),
                    )
                    self._json(HTTPStatus.OK, {"deleted": True})
                    return
                else:
                    raise ValueError("invalid_household_action")
            except (PermissionError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return

            result = self.miso.tool_registry.invoke(
                tool_name, arguments, actor=actor
            )
            if not result.ok:
                status = (
                    HTTPStatus.CONFLICT
                    if result.error == "revision_conflict"
                    else HTTPStatus.BAD_REQUEST
                )
                self._json(status, {"error": result.error or "household_action_failed"})
                return
            output = result.output or {}
            self._json(
                HTTPStatus.CREATED if tool_name.endswith("_create") or tool_name == "shopping_add" else HTTPStatus.OK,
                {output_key: output.get(output_key)},
            )

        def _speech(self, payload: dict[str, object]) -> None:
            text = payload.get("text")
            language = payload.get("language")
            volume = payload.get("volume")
            if not isinstance(text, str) or not isinstance(language, str):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_speech_request"})
                return
            if volume is not None and not isinstance(volume, (int, float)):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_volume"})
                return
            try:
                request_id = self.miso.speech_manager.speak(
                    text, language, volume=None if volume is None else float(volume)
                )
            except SpeechError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(HTTPStatus.ACCEPTED, {"request_id": request_id})

        def _memory(
            self, query: str, *, kind: str, tag: str, record_type: str
        ) -> None:
            if len(query) > 500 or len(tag) > 64:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "query_too_long"})
                return
            kinds = () if not kind else (kind,)
            record_types = (
                ("memory", "event") if not record_type else (record_type,)
            )
            try:
                results = self.miso.memory_store.search(
                    query,
                    limit=100,
                    actor=self._actor(),
                    kinds=kinds,
                    tag=tag,
                    record_types=record_types,
                )
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search"})
                return
            self._json(
                HTTPStatus.OK,
                {"results": [_memory_result_payload(item) for item in results]},
            )

        def _memory_export(self) -> None:
            records = self.miso.memory_store.search(
                "", limit=None, actor=self._actor()
            )
            self._json(
                HTTPStatus.OK,
                {
                    "schema_version": 1,
                    "exported_at": utc_now(),
                    "actor": self._actor().public_dict(),
                    "records": [_memory_result_payload(item) for item in records],
                },
            )

        def _memory_action(self, payload: dict[str, object]) -> None:
            action = payload.get("action")
            actor = self._actor()
            try:
                if action == "remember":
                    content = payload.get("content")
                    visibility = payload.get("visibility", "private")
                    if (
                        not isinstance(content, str)
                        or not 1 <= len(content.strip()) <= 4000
                    ):
                        raise ValueError("invalid_memory_content")
                    if visibility not in {"shared", "private"}:
                        raise ValueError("invalid_visibility")
                    tags = _memory_tags(payload.get("tags", []))
                    memory_id = self.miso.memory_store.add_memory(
                        content.strip(),
                        kind="explicit",
                        importance=1.0,
                        tags=tags,
                        actor=actor,
                        visibility=cast(str, visibility),
                    )
                    record = self._memory_record(memory_id)
                    self._json(
                        HTTPStatus.CREATED,
                        {"record": _memory_result_payload(record)},
                    )
                    return
                if action == "update":
                    memory_id = _record_id(payload.get("record_id"))
                    raw_importance = payload.get("importance")
                    importance = None
                    if raw_importance is not None:
                        if isinstance(raw_importance, bool) or not isinstance(
                            raw_importance, (int, float)
                        ):
                            raise ValueError("invalid_importance")
                        importance = float(raw_importance)
                        if not 0 <= importance <= 1:
                            raise ValueError("invalid_importance")
                    tags = None
                    if "tags" in payload:
                        tags = _memory_tags(payload["tags"])
                    if importance is None and tags is None:
                        raise ValueError("no_memory_changes")
                    if not self.miso.memory_store.update_memory(
                        memory_id, importance=importance, tags=tags, actor=actor
                    ):
                        self._json(
                            HTTPStatus.NOT_FOUND, {"error": "memory_not_found"}
                        )
                        return
                    record = self._memory_record(memory_id)
                    self._json(
                        HTTPStatus.OK,
                        {"record": _memory_result_payload(record)},
                    )
                    return
                if action == "preview_prune":
                    raw_days = payload.get("older_than_days")
                    days = None
                    if raw_days is not None and raw_days != "":
                        if (
                            isinstance(raw_days, bool)
                            or not isinstance(raw_days, int)
                            or not 1 <= raw_days <= 36500
                        ):
                            raise ValueError("invalid_prune_age")
                        days = raw_days
                    topic = payload.get("topic", "")
                    if not isinstance(topic, str) or len(topic) > 500:
                        raise ValueError("invalid_prune_topic")
                    candidates, impact = self.miso.memory_store.prune_preview(
                        older_than_days=days, topic=topic, actor=actor
                    )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "candidates": [
                                _memory_result_payload(item) for item in candidates
                            ],
                            "impact": {
                                "records": impact.records_deleted,
                                "derived_memories": impact.derived_memories_deleted,
                                "embeddings": impact.embeddings_deleted,
                            },
                        },
                    )
                    return
                if action == "delete":
                    raw_records = payload.get("records")
                    if (
                        not isinstance(raw_records, list)
                        or not 1 <= len(raw_records) <= 100
                    ):
                        raise ValueError("invalid_delete_selection")
                    records: list[tuple[str, int]] = []
                    for raw_record in raw_records:
                        if not isinstance(raw_record, dict):
                            raise ValueError("invalid_delete_selection")
                        record_type = raw_record.get("record_type")
                        if record_type not in {"memory", "event"}:
                            raise ValueError("invalid_record_type")
                        records.append(
                            (
                                cast(str, record_type),
                                _record_id(raw_record.get("record_id")),
                            )
                        )
                    deleted = self.miso.memory_store.delete_records(
                        records, actor=actor
                    )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "deleted": {
                                "records": deleted.records_deleted,
                                "derived_memories": deleted.derived_memories_deleted,
                                "embeddings": deleted.embeddings_deleted,
                            }
                        },
                    )
                    return
                raise ValueError("invalid_memory_action")
            except PermissionError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "memory_not_found"})
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "memory_action_failed"})

        def _memory_record(self, memory_id: int) -> SearchResult:
            records = self.miso.memory_store.search(
                "", limit=None, actor=self._actor(), record_types=("memory",)
            )
            return next(item for item in records if item.record_id == memory_id)

        def _publish_live(
            self,
            event_type: str,
            payload: dict[str, object],
            *,
            actor: Actor,
            visibility: str,
            owner_email: str | None,
        ) -> None:
            try:
                self.miso.live_events.publish(
                    event_type,
                    payload,
                    actor=actor,
                    visibility=visibility,
                    owner_email=owner_email,
                )
            except Exception:
                LOGGER.exception("could not publish %s", event_type)

        def _activity(self, limit: int) -> None:
            audit_root = self.miso.settings.state_dir / "audit"
            events = _jsonl_tail(audit_root / "tools.jsonl", limit)
            events.extend(_jsonl_tail(audit_root / "routing.jsonl", limit))
            events = [
                event
                for event in events
                if event.get("actor", VOICE_ACTOR.actor_id)
                == self._actor().actor_id
                or (
                    event.get("visibility", "shared") == "shared"
                    and event.get("actor", VOICE_ACTOR.actor_id)
                    in {VOICE_ACTOR.actor_id, SYSTEM_ACTOR.actor_id}
                )
            ]
            events.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
            self._json(HTTPStatus.OK, {"events": events[:limit]})

        def _chat(self, payload: dict[str, object]) -> None:
            text = payload.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_text"})
                return
            conversation_id = payload.get("conversation_id")
            actor = self._actor()
            if conversation_id is not None and (
                not isinstance(conversation_id, str)
                or not self.miso.memory_store.conversation_exists(
                    conversation_id, actor=actor
                )
            ):
                self._json(HTTPStatus.NOT_FOUND, {"error": "conversation_not_found"})
                return
            if conversation_id is None:
                conversation_id = self.miso.memory_store.create_conversation(
                    actor=actor, visibility="private"
                )
            request_id = payload.get("request_id") or str(uuid.uuid4())
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request_id"})
                return
            route_class = payload.get("route_class", "auto")
            override = payload.get("provider")
            if not isinstance(route_class, str) or (
                override is not None and not isinstance(override, str)
            ):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_routing"})
                return
            cancel = threading.Event()
            if not self.miso.register_request(request_id, cancel, actor):
                self._json(HTTPStatus.CONFLICT, {"error": "request_already_active"})
                return
            self.miso.memory_store.append_event(
                conversation_id,
                kind="message",
                role="user",
                content=text.strip(),
                payload={"request_id": request_id},
                actor=actor,
            )
            fast = None
            if self.miso.fast_lane is not None:
                fast = self.miso.fast_lane.try_handle(
                    text.strip(),
                    guess_language(text),
                    cancel_event=cancel,
                    actor=actor,
                )
            if fast is None and self.miso.tool_picker is not None:
                fast = self.miso.tool_picker.try_handle(
                    text.strip(),
                    guess_language(text),
                    cancel_event=cancel,
                    actor=actor,
                )
            if fast is not None:
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type", "application/x-ndjson; charset=utf-8"
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.miso.memory_store.append_event(
                        conversation_id,
                        kind="tool",
                        role="assistant",
                        content=fast.tool,
                        payload=fast.result.as_dict(),
                        actor=actor,
                    )
                    self.miso.memory_store.append_event(
                        conversation_id,
                        kind="message",
                        role="assistant",
                        content=fast.spoken,
                        payload={
                            "request_id": request_id,
                            "fast_intent": fast.intent,
                        },
                        actor=actor,
                    )
                    self._ndjson(
                        {
                            "type": "tool_result",
                            "result": fast.result.as_dict(),
                            "provider": "fast-lane",
                            "route_id": None,
                        }
                    )
                    self._ndjson(
                        {
                            "type": "delta",
                            "text": fast.spoken,
                            "provider": "fast-lane",
                            "route_id": None,
                        }
                    )
                    self._ndjson(
                        {
                            "type": "complete",
                            "conversation_id": conversation_id,
                            "request_id": request_id,
                        }
                    )
                except (BrokenPipeError, ConnectionResetError):
                    cancel.set()
                finally:
                    self.miso.unregister_request(request_id)
                return
            history = self.miso.memory_store.events(
                conversation_id, limit=40, actor=actor
            )
            messages = (
                {"role": "system", "content": MISO_SYSTEM_PROMPT},
                *(
                    {"role": event.role, "content": event.content}
                    for event in history
                    if event.kind == "message"
                    and event.role in {"user", "assistant"}
                    and event.content
                ),
            )
            request = ChatRequest(messages=messages, tools=self.miso.tool_registry.schemas())
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            assistant_text: list[str] = []
            tool_summaries: list[tuple[str, str | None, str | None]] = []
            try:
                stream = self.miso.router.stream(
                    request,
                    cancel,
                    route_class=route_class,
                    manual_override=override,
                    actor=actor,
                )
                for chunk in stream:
                    if chunk.progress:
                        self._ndjson(
                            {
                                "type": "progress",
                                "message": chunk.progress,
                                "provider": chunk.provider,
                                "route_id": chunk.route_id,
                            }
                        )
                    if chunk.text:
                        assistant_text.append(chunk.text)
                        self._ndjson(
                            {
                                "type": "delta",
                                "text": chunk.text,
                                "provider": chunk.provider,
                                "route_id": chunk.route_id,
                            }
                        )
                    if chunk.tool_call:
                        name = chunk.tool_call.get("name")
                        arguments = chunk.tool_call.get("arguments")
                        if not isinstance(name, str) or not isinstance(arguments, dict):
                            raise RoutingError("provider returned invalid tool call")
                        result = self.miso.tool_registry.invoke(
                            name, arguments, cancel_event=cancel, actor=actor
                        )
                        if result.summary is not None:
                            tool_summaries.append(
                                (result.summary, chunk.provider, chunk.route_id)
                            )
                        encoded_result = result.as_dict()
                        self.miso.memory_store.append_event(
                            conversation_id,
                            kind="tool",
                            role="assistant",
                            content=name,
                            payload=encoded_result,
                            actor=actor,
                        )
                        self._ndjson(
                            {
                                "type": "tool_result",
                                "result": encoded_result,
                                "provider": chunk.provider,
                                "route_id": chunk.route_id,
                            }
                        )
                if not assistant_text and tool_summaries:
                    summary = " ".join(item[0] for item in tool_summaries)
                    assistant_text.append(summary)
                    self._ndjson(
                        {
                            "type": "delta",
                            "text": summary,
                            "provider": tool_summaries[-1][1],
                            "route_id": tool_summaries[-1][2],
                        }
                    )
                combined = "".join(assistant_text)
                if combined:
                    self.miso.memory_store.append_event(
                        conversation_id,
                        kind="message",
                        role="assistant",
                        content=combined,
                        payload={"request_id": request_id},
                        actor=actor,
                    )
                self._ndjson(
                    {
                        "type": "complete",
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                    }
                )
            except ProviderCancelled:
                self._ndjson({"type": "cancelled", "request_id": request_id})
            except (RoutingError, ValueError, KeyError) as error:
                self._ndjson(
                    {
                        "type": "error",
                        "error": str(error)[:500],
                        "request_id": request_id,
                    }
                )
            except (BrokenPipeError, ConnectionResetError):
                cancel.set()
            finally:
                self.miso.unregister_request(request_id)

        def _tools(self) -> None:
            registry = self.miso.tool_registry
            loader = self.miso.tool_loader
            self._json(
                HTTPStatus.OK,
                {
                    "tools": [
                        {
                            "name": schema["name"],
                            "description": schema["description"],
                            "source": registry.source_of(str(schema["name"])),
                        }
                        for schema in registry.schemas()
                    ],
                    "refresh": None if loader is None else loader.status(),
                },
            )

        def _tools_refresh(self, payload: dict[str, object]) -> None:
            if self.miso.tool_loader is None:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "tool_refresh_unavailable"},
                )
                return
            module = payload.get("module")
            arguments: dict[str, object] = {}
            if module is not None:
                arguments["module"] = module
            result = self.miso.tool_registry.invoke(
                REFRESH_TOOL_NAME, arguments, actor=self._actor()
            )
            report = result.output if isinstance(result.output, dict) else None
            rejected = bool(report is not None and not report.get("ok", True))
            status = (
                HTTPStatus.OK if result.ok and not rejected else HTTPStatus.BAD_REQUEST
            )
            self._json(status, {"result": result.as_dict()})

        def _developer(self, payload: dict[str, object]) -> None:
            action = payload.get("action")
            try:
                if action == "enable":
                    duration = payload.get("duration_seconds", 300)
                    if not isinstance(duration, int) or isinstance(duration, bool):
                        raise ValueError("duration_seconds must be an integer")
                    status = self.miso.developer_shell.enable(
                        duration,
                        approved_by=self._actor().actor_id,
                    )
                elif action == "disable":
                    status = self.miso.developer_shell.disable(
                        actor=self._actor().actor_id
                    )
                else:
                    raise ValueError("action must be enable or disable")
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, {"developer_mode": status})

        def _developer_command(self, payload: dict[str, object]) -> None:
            command = payload.get("command")
            cwd = payload.get("cwd")
            arguments: dict[str, object] = {"command": command}
            if cwd is not None:
                arguments["cwd"] = cwd
            result = self.miso.tool_registry.invoke(
                "developer_command", arguments, actor=self._actor()
            )
            status = HTTPStatus.OK if result.ok else HTTPStatus.BAD_REQUEST
            self._json(status, {"result": result.as_dict()})

        def _request_json(self) -> dict[str, object]:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("content_type_must_be_application_json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid_content_length") from error
            if not 1 <= length <= MAX_BODY_BYTES:
                raise ValueError("invalid_body_size")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid_json") from error
            if not isinstance(value, dict):
                raise ValueError("json_body_must_be_an_object")
            return value

        def _static(self, path: str) -> None:
            name = STATIC_ASSETS[path]
            file_path = WEB_ROOT / name
            if not file_path.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".webmanifest": "application/manifest+json; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".riv": "application/octet-stream",
                ".wasm": "application/wasm",
            }[file_path.suffix]
            self._bytes(HTTPStatus.OK, file_path.read_bytes(), content_type)

        def _ndjson(self, payload: dict[str, object]) -> None:
            self.wfile.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                + b"\n"
            )
            self.wfile.flush()

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._bytes(status, encoded, "application/json; charset=utf-8")

        def _bytes(self, status: HTTPStatus, value: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(value)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _memory_result_payload(item: SearchResult) -> dict[str, object]:
    return {
        "record_type": item.record_type,
        "record_id": item.record_id,
        "content": item.content,
        "rank": item.rank,
        "created_at": item.created_at,
        "kind": item.kind,
        "role": item.role,
        "conversation_id": item.conversation_id,
        "importance": item.importance,
        "tags": list(item.tags),
        "source_event_id": item.source_event_id,
        "sources": list(item.sources),
        "visibility": item.visibility,
        "created_by": item.created_by,
    }


def _memory_tags(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("invalid_memory_tags")
    tags: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 64:
            raise ValueError("invalid_memory_tags")
        tags.append(tag.strip())
    return tags


def _record_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("invalid_record_id")
    return value


def _bounded_integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
    error: str,
) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exception:
        raise ValueError(error) from exception
    if not minimum <= parsed <= maximum:
        raise ValueError(error)
    return parsed


def _copy_fields(
    payload: dict[str, object], fields: tuple[str, ...]
) -> dict[str, object]:
    return {name: payload[name] for name in fields if name in payload}


def _jsonl_tail(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        offset = max(0, size - 262144)
        handle.seek(offset)
        if offset:
            handle.readline()
        lines = handle.readlines()
    events = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def create_server(
    settings: Settings,
    port: int | None = None,
    *,
    tool_registry: ToolRegistry | None = None,
    router: ProviderRouter | None = None,
    developer_shell: DeveloperShellController | None = None,
    audio_manager: AudioManager | None = None,
    transcription_manager: TranscriptionManager | None = None,
    speech_manager: SpeechManager | None = None,
    wake_manager: WakeWordManager | None = None,
    wake_calibration: WakeCalibration | None = None,
    conversation_manager: ConversationManager | None = None,
    button_manager: ButtonManager | None = None,
    access_verifier: AccessJWTVerifier | None = None,
) -> MisoHTTPServer:
    identity_policy = HouseholdIdentityPolicy(settings.dashboard_email)
    memory = MemoryStore(settings.database_path)
    memory.migrate()
    live_events = LiveEventStore(settings.database_path)
    if access_verifier is None and settings.access_team_domain is not None:
        access_verifier = AccessJWTVerifier(
            settings.access_team_domain,
            cast(str, settings.access_audience),
        )
    calendar_config = None
    if settings.google_calendar_enabled:
        calendar_config = GoogleCalendarConfig(
            client_path=settings.google_calendar_client_path,
            token_dir=settings.google_calendar_token_dir,
            default_timezone=settings.google_calendar_default_timezone,
            default_calendar_id=settings.google_calendar_default_id,
            voice_account_email=settings.google_calendar_voice_email,
        )
    registry = tool_registry or create_runtime_registry(
        settings.state_dir,
        settings.database_path,
        calendar_config,
        WeatherConfig(default_location=settings.weather_default_location),
    )
    registry.audit_sink = LiveAuditSink(registry.audit_sink, live_events)
    registry.add_result_listener(LiveToolResultPublisher(live_events))
    shell = developer_shell or DeveloperShellController(
        (settings.developer_root or settings.state_dir).resolve(),
        settings.developer_commands,
        audit_sink=registry.audit_sink,
    )
    if "developer_command" not in registry.names():
        registry.register(shell.tool_definition())
    tool_loader = ToolDirectoryLoader(
        registry, settings.tools_dir, audit_sink=registry.audit_sink
    )
    if REFRESH_TOOL_NAME not in registry.names():
        registry.register(tool_loader.tool_definition())
    if settings.tools_dir.is_dir():
        tool_loader.refresh(actor=SYSTEM_ACTOR)
    playback_backend = (
        PulseProxyBackend(settings.audio_playback_proxy)
        if settings.audio_playback_backend == "pulse"
        else None
    )
    audio = audio_manager or AudioManager(
        enabled=settings.audio_enabled,
        capture_card=settings.audio_capture_card,
        playback_card=settings.audio_playback_card,
        device_index=settings.audio_device_index,
        sample_rate=settings.audio_sample_rate,
        playback_sample_rate=settings.audio_playback_sample_rate,
        channels=settings.audio_channels,
        chunk_milliseconds=settings.audio_chunk_milliseconds,
        buffer_milliseconds=settings.audio_buffer_milliseconds,
        reconnect_seconds=settings.audio_reconnect_seconds,
        silence_dbfs=settings.audio_silence_dbfs,
        clipping_ratio=settings.audio_clipping_ratio,
        playback_backend=playback_backend,
    )
    wake = wake_manager or WakeWordManager(
        enabled=settings.wake_enabled,
        audio=audio,
        model=OpenWakeWordModel(
            settings.wake_executable,
            settings.wake_model,
            vad_threshold=settings.wake_vad_threshold,
        ),
        phrase=settings.wake_phrase,
        threshold=settings.wake_threshold,
        energy_threshold_dbfs=settings.wake_energy_threshold_dbfs,
        activation_frames=settings.wake_activation_frames,
        cooldown_seconds=settings.wake_cooldown_seconds,
        result_capacity=settings.wake_result_capacity,
        on_activation=DisplayWakeNotifier(settings.display_wake_path).notify,
    )
    transcription = transcription_manager or TranscriptionManager(
        enabled=settings.stt_enabled,
        audio=audio,
        transcriber=WhisperCppTranscriber(
            settings.stt_executable,
            settings.stt_model,
            threads=settings.stt_threads,
            timeout_seconds=settings.stt_timeout_seconds,
            work_directory=Path("/run/miso"),
            prompt=settings.stt_prompt,
        ),
        detector=EnergySpeechDetector(settings.stt_vad_threshold_dbfs),
        assembler=UtteranceAssembler(
            audio.audio_format,
            minimum_speech_milliseconds=(
                settings.stt_vad_minimum_speech_milliseconds
            ),
            end_silence_milliseconds=settings.stt_vad_end_silence_milliseconds,
            maximum_utterance_milliseconds=(
                settings.stt_vad_maximum_utterance_milliseconds
            ),
            pre_roll_milliseconds=settings.stt_vad_pre_roll_milliseconds,
        ),
        result_capacity=settings.stt_result_capacity,
    )
    calibration = wake_calibration or WakeCalibration(
        enabled=settings.audio_enabled and settings.stt_enabled,
        audio=audio,
        transcriber=transcription.transcriber,
    )
    speech = speech_manager or SpeechManager(
        enabled=settings.tts_enabled,
        backend=PiperBackend(
            settings.tts_executable,
            (
                PiperVoice(
                    "en",
                    settings.tts_english_voice,
                    settings.tts_english_model,
                    settings.tts_english_config,
                    settings.audio_playback_sample_rate,
                ),
                PiperVoice(
                    "es",
                    settings.tts_spanish_voice,
                    settings.tts_spanish_model,
                    settings.tts_spanish_config,
                    settings.audio_playback_sample_rate,
                ),
            ),
            chunk_bytes=settings.tts_chunk_bytes,
            timeout_seconds=settings.tts_timeout_seconds,
        ),
        audio=audio,
        default_volume=settings.tts_volume,
        result_capacity=settings.tts_result_capacity,
    )
    fast_lane = FastLane(
        registry,
        registry.audit_sink,
        enabled=settings.fast_lane_enabled,
    )
    tool_picker = ToolPicker(
        registry,
        OllamaProvider(
            settings.ollama_url,
            settings.ollama_model,
            settings.provider_timeout_seconds,
        ),
        registry.audit_sink,
        enabled=settings.tool_picker_enabled,
        max_tokens=settings.tool_picker_max_tokens,
        timeout_seconds=settings.tool_picker_timeout_seconds,
    )
    conversation = conversation_manager or ConversationManager(
        enabled=settings.conversation_enabled,
        wake=wake,
        transcription=transcription,
        router=router or create_router(settings),
        tools=registry,
        speech=speech,
        memory=memory,
        fast_lane=fast_lane,
        tool_picker=tool_picker,
        audit_sink=registry.audit_sink,
        latency_sink=JsonlAuditLog(
            settings.state_dir / "audit" / "routing.jsonl"
        ),
        system_prompt=MISO_SYSTEM_PROMPT,
        wake_phrase=settings.wake_phrase,
        listen_timeout_seconds=settings.conversation_listen_timeout_seconds,
        checkback_timeout_seconds=settings.conversation_checkback_timeout_seconds,
        acknowledgement=settings.conversation_acknowledgement,
        acknowledge_wake=settings.conversation_acknowledge_wake,
        languages=settings.stt_languages,
        echo_guard_seconds=settings.conversation_echo_guard_seconds,
        echo_memory_seconds=settings.conversation_echo_memory_seconds,
    )
    buttons = button_manager or ButtonManager(
        enabled=settings.buttons_enabled,
        router=ButtonRouter(
            wake=wake,
            conversation=conversation,
            audit_sink=registry.audit_sink,
            wake_phrase=settings.wake_phrase,
            debounce_seconds=settings.button_bounce_milliseconds / 1000,
            hold_seconds=settings.button_hold_seconds,
        ),
        talk_pin=settings.button_talk_pin,
        stop_pin=settings.button_stop_pin,
        pull_up=settings.button_pull_up,
        bounce_seconds=settings.button_bounce_milliseconds / 1000,
        hold_seconds=settings.button_hold_seconds,
    )
    conversation.add_transition_listener(conversation_event_publisher(live_events))
    conversation.add_response_listener(conversation_caption_publisher(live_events))
    conversation.add_transcript_listener(user_transcript_publisher(live_events))
    conversation.add_capture_listener(user_capture_publisher(live_events))
    conversation.add_error_listener(conversation_error_publisher(live_events))
    return MisoHTTPServer(
        (settings.host, settings.port if port is None else port),
        handler_type(),
        settings=settings,
        tool_registry=registry,
        scheduled_worker=ScheduledItemWorker(
            HouseholdStore(settings.database_path), registry.audit_sink
        ),
        router=conversation.router,
        developer_shell=shell,
        audio_manager=audio,
        transcription_manager=transcription,
        speech_manager=speech,
        wake_manager=wake,
        wake_calibration=calibration,
        conversation_manager=conversation,
        button_manager=buttons,
        live_events=live_events,
        identity_policy=identity_policy,
        access_verifier=access_verifier,
        started_at=time.monotonic(),
        fast_lane=fast_lane,
        tool_loader=tool_loader,
        tool_picker=tool_picker,
    )
