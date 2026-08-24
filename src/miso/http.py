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
from miso.audio import AudioManager
from miso.config import Settings
from miso.conversation import ConversationManager
from miso.identity import (
    Actor,
    HouseholdIdentityPolicy,
    SYSTEM_ACTOR,
    VOICE_ACTOR,
)
from miso.memory import MemoryStore
from miso.providers import ChatRequest, ProviderCancelled
from miso.routing import ProviderRouter, RoutingError, create_router
from miso.speech import PiperBackend, PiperVoice, SpeechError, SpeechManager
from miso.transcription import (
    EnergySpeechDetector,
    TranscriptionManager,
    UtteranceAssembler,
    WhisperCppTranscriber,
)
from miso.wake import OpenWakeWordModel, WakeWordManager
from miso.tools import (
    DeveloperShellController,
    HouseholdStore,
    ScheduledItemWorker,
    ToolRegistry,
    create_runtime_registry,
)

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
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/service-worker.js": "service-worker.js",
    "/favicon-32.png": "favicon-32.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/icon-maskable-512.png": "icon-maskable-512.png",
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
        conversation_manager: ConversationManager,
        identity_policy: HouseholdIdentityPolicy,
        access_verifier: AccessJWTVerifier | None,
        started_at: float,
    ) -> None:
        self.settings = settings
        self.tool_registry = tool_registry
        self.scheduled_worker = scheduled_worker
        self.router = router
        self.developer_shell = developer_shell
        self.audio_manager = audio_manager
        self.transcription_manager = transcription_manager
        self.speech_manager = speech_manager
        self.wake_manager = wake_manager
        self.conversation_manager = conversation_manager
        self.memory_store = MemoryStore(settings.database_path)
        self.identity_policy = identity_policy
        self.access_verifier = access_verifier
        self.memory_store.provision_household_members(identity_policy.allowed_emails)
        self.started_at = started_at
        self._active_requests: dict[str, tuple[threading.Event, str]] = {}
        self._active_lock = threading.Lock()
        super().__init__(server_address, request_handler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.scheduled_worker.start()
        self.audio_manager.start()
        self.wake_manager.start()
        self.transcription_manager.start()
        self.speech_manager.start()
        self.conversation_manager.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.conversation_manager.stop()
            self.speech_manager.stop()
            self.transcription_manager.stop()
            self.wake_manager.stop()
            self.audio_manager.stop()
            self.scheduled_worker.stop()

    def server_close(self) -> None:
        self.conversation_manager.stop()
        self.speech_manager.stop()
        self.transcription_manager.stop()
        self.wake_manager.stop()
        self.audio_manager.stop()
        self.scheduled_worker.stop()
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
            elif parsed.path == "/api/identity":
                self._identity()
            elif parsed.path == "/api/memory":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._memory(query)
            elif parsed.path == "/api/activity":
                raw_limit = parse_qs(parsed.query).get("limit", ["50"])[0]
                try:
                    limit = max(1, min(int(raw_limit), 100))
                except ValueError:
                    limit = 50
                self._activity(limit)
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
                    self._request_actor = self.miso.identity_policy.web_actor(email)
                    return True
                except AccessJWTError as error:
                    LOGGER.warning("Cloudflare Access assertion rejected: %s", error)
                except PermissionError:
                    LOGGER.warning(
                        "Cloudflare Access identity rejected by household allowlist"
                    )
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

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
                        "routine": ["pi-ollama", "lan-ollama", "hosted-gpt"],
                        "complex": ["lan-ollama", "hosted-gpt", "pi-ollama"],
                    },
                    "tools": list(self.miso.tool_registry.names()),
                    "audio": self.miso.audio_manager.status(),
                    "wake": self.miso.wake_manager.status(),
                    "transcription": self.miso.transcription_manager.status(),
                    "speech": self.miso.speech_manager.status(),
                    "conversation": self.miso.conversation_manager.status(),
                    "developer_mode": self.miso.developer_shell.status(),
                },
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

        def _memory(self, query: str) -> None:
            if len(query) > 500:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "query_too_long"})
                return
            try:
                results = self.miso.memory_store.search(
                    query, limit=30, actor=self._actor()
                )
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "results": [
                        {
                            "record_type": item.record_type,
                            "record_id": item.record_id,
                            "content": item.content,
                            "rank": item.rank,
                            "created_at": item.created_at,
                        }
                        for item in results
                    ]
                },
            )

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
                "style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(value)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


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
    conversation_manager: ConversationManager | None = None,
    access_verifier: AccessJWTVerifier | None = None,
) -> MisoHTTPServer:
    identity_policy = HouseholdIdentityPolicy(
        settings.household_allowed_emails, settings.dashboard_email
    )
    if access_verifier is None and settings.access_team_domain is not None:
        access_verifier = AccessJWTVerifier(
            settings.access_team_domain,
            cast(str, settings.access_audience),
        )
    registry = tool_registry or create_runtime_registry(
        settings.state_dir, settings.database_path
    )
    shell = developer_shell or DeveloperShellController(
        (settings.developer_root or settings.state_dir).resolve(),
        settings.developer_commands,
        audit_sink=registry.audit_sink,
    )
    if "developer_command" not in registry.names():
        registry.register(shell.tool_definition())
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
    memory = MemoryStore(settings.database_path)
    memory.migrate()
    conversation = conversation_manager or ConversationManager(
        enabled=settings.conversation_enabled,
        wake=wake,
        transcription=transcription,
        router=router or create_router(settings),
        tools=registry,
        speech=speech,
        memory=memory,
        audit_sink=registry.audit_sink,
        system_prompt=MISO_SYSTEM_PROMPT,
        wake_phrase=settings.wake_phrase,
        listen_timeout_seconds=settings.conversation_listen_timeout_seconds,
        checkback_timeout_seconds=settings.conversation_checkback_timeout_seconds,
        acknowledgement=settings.conversation_acknowledgement,
    )
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
        conversation_manager=conversation,
        identity_policy=identity_policy,
        access_verifier=access_verifier,
        started_at=time.monotonic(),
    )
