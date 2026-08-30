"""Validated environment configuration for the Miso service."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from miso.identity import IdentityError, normalize_email


class ConfigError(ValueError):
    """Raised when Miso configuration is unsafe or invalid."""


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _email(value: str, name: str) -> str:
    try:
        return normalize_email(value)
    except IdentityError as error:
        raise ConfigError(f"{name} must be a valid email address") from error


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    database_path: Path
    state_dir: Path
    model_dir: Path
    ollama_url: str
    ollama_model: str
    provider_timeout_seconds: float
    log_level: str
    lan_ollama_url: str | None = None
    lan_ollama_model: str = "qwen3:8b"
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    routing_health_timeout_seconds: float = 2.0
    routing_attempt_timeout_seconds: float = 45.0
    routing_stream_timeout_seconds: float = 300.0
    routing_health_cache_seconds: float = 20.0
    fast_lane_enabled: bool = True
    dashboard_token: str | None = field(default=None, repr=False)
    dashboard_email: str = "local@miso.invalid"
    access_team_domain: str | None = None
    access_audience: str | None = None
    google_calendar_enabled: bool = False
    google_calendar_client_path: Path = Path("/etc/miso/google-calendar-client.json")
    google_calendar_token_dir: Path = Path(
        "/var/lib/miso/state/google-calendar"
    )
    google_calendar_default_timezone: str = "Europe/London"
    google_calendar_default_id: str = "primary"
    google_calendar_voice_email: str | None = None
    weather_default_location: str | None = None
    developer_root: Path | None = None
    developer_commands: tuple[str, ...] = ("python3", "git", "rg", "ls")
    audio_enabled: bool = True
    audio_capture_card: str | None = None
    audio_playback_card: str | None = None
    audio_playback_backend: str = "alsa"
    audio_playback_proxy: Path = Path("/run/miso-audio/playback.sock")
    audio_device_index: int = 0
    audio_sample_rate: int = 16_000
    audio_playback_sample_rate: int = 22_050
    audio_channels: int = 1
    audio_chunk_milliseconds: int = 20
    audio_buffer_milliseconds: int = 1_000
    audio_reconnect_seconds: float = 1.0
    audio_silence_dbfs: float = -50.0
    audio_clipping_ratio: float = 0.98
    wake_enabled: bool = False
    wake_phrase: str = "Miso"
    wake_executable: Path = Path("/opt/miso/openwakeword/bin/python")
    wake_model: Path = Path("/var/lib/miso/models/openwakeword/miso.onnx")
    wake_threshold: float = 0.999
    wake_vad_threshold: float = 0.5
    wake_energy_threshold_dbfs: float = -60.0
    wake_activation_frames: int = 1
    wake_cooldown_seconds: float = 2.0
    wake_result_capacity: int = 16
    display_wake_path: Path | None = None
    stt_enabled: bool = False
    stt_executable: Path = Path("/usr/local/bin/whisper-cli")
    stt_model: Path = Path("/var/lib/miso/models/whisper/ggml-tiny.bin")
    stt_threads: int = 4
    stt_timeout_seconds: float = 45.0
    # Whole example commands, not a vocabulary list: whisper conditions on this
    # text as if it preceded the utterance, so phrasing primes far better than
    # loose keywords. See docs/miso-transcription-benchmark.md.
    stt_prompt: str = (
        "Miso. Comandos de casa en español e inglés. "
        "Pon un temporizador de ocho minutos. "
        "¿Cuánto tiempo queda en el temporizador? "
        "Añade leche a la lista de la compra. Enciende la luz de la cocina. "
        "Set a timer for twenty seconds. Add bread to the shopping list."
    )
    stt_result_capacity: int = 16
    stt_vad_threshold_dbfs: float = -38.0
    stt_vad_minimum_speech_milliseconds: int = 250
    stt_vad_end_silence_milliseconds: int = 600
    stt_vad_maximum_utterance_milliseconds: int = 15_000
    stt_vad_pre_roll_milliseconds: int = 200
    tts_enabled: bool = False
    tts_executable: Path = Path("/opt/miso/piper/bin/python")
    tts_english_voice: str = "en_GB-cori-medium"
    tts_english_model: Path = Path(
        "/var/lib/miso/models/piper/en_GB-cori-medium.onnx"
    )
    tts_english_config: Path = Path(
        "/var/lib/miso/models/piper/en_GB-cori-medium.onnx.json"
    )
    tts_spanish_voice: str = "es_ES-davefx-medium"
    tts_spanish_model: Path = Path(
        "/var/lib/miso/models/piper/es_ES-davefx-medium.onnx"
    )
    tts_spanish_config: Path = Path(
        "/var/lib/miso/models/piper/es_ES-davefx-medium.onnx.json"
    )
    tts_volume: float = 1.0
    tts_chunk_bytes: int = 4_096
    tts_timeout_seconds: float = 60.0
    tts_result_capacity: int = 16
    conversation_enabled: bool = True
    conversation_listen_timeout_seconds: float = 8.0
    conversation_checkback_timeout_seconds: float = 5.0
    conversation_acknowledgement: str = "Yes?"
    conversation_echo_guard_seconds: float = 0.6
    conversation_echo_memory_seconds: float = 12.0

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Settings":
        source = environ if values is None else values
        model_dir = Path(source.get("MISO_MODEL_DIR", "/var/lib/miso/models"))
        try:
            port = int(source.get("MISO_PORT", "8090"))
        except ValueError as error:
            raise ConfigError("MISO_PORT must be an integer") from error
        try:
            provider_timeout = float(source.get("MISO_PROVIDER_TIMEOUT", "120"))
        except ValueError as error:
            raise ConfigError("MISO_PROVIDER_TIMEOUT must be numeric") from error
        try:
            routing_health_timeout = float(
                source.get("MISO_ROUTING_HEALTH_TIMEOUT", "2")
            )
            routing_attempt_timeout = float(
                source.get("MISO_ROUTING_ATTEMPT_TIMEOUT", "45")
            )
            routing_stream_timeout = float(
                source.get("MISO_ROUTING_STREAM_TIMEOUT", "300")
            )
            routing_health_cache = float(
                source.get("MISO_ROUTING_HEALTH_CACHE_SECONDS", "20")
            )
        except ValueError as error:
            raise ConfigError("MISO routing timeouts must be numeric") from error
        try:
            audio_device_index = int(source.get("MISO_AUDIO_DEVICE_INDEX", "0"))
            audio_sample_rate = int(source.get("MISO_AUDIO_SAMPLE_RATE", "16000"))
            audio_playback_sample_rate = int(
                source.get("MISO_AUDIO_PLAYBACK_SAMPLE_RATE", "22050")
            )
            audio_channels = int(source.get("MISO_AUDIO_CHANNELS", "1"))
            audio_chunk_milliseconds = int(
                source.get("MISO_AUDIO_CHUNK_MILLISECONDS", "20")
            )
            audio_buffer_milliseconds = int(
                source.get("MISO_AUDIO_BUFFER_MILLISECONDS", "1000")
            )
            audio_reconnect_seconds = float(
                source.get("MISO_AUDIO_RECONNECT_SECONDS", "1")
            )
            audio_silence_dbfs = float(
                source.get("MISO_AUDIO_SILENCE_DBFS", "-50")
            )
            audio_clipping_ratio = float(
                source.get("MISO_AUDIO_CLIPPING_RATIO", "0.98")
            )
            wake_threshold = float(source.get("MISO_WAKE_THRESHOLD", "0.999"))
            wake_vad_threshold = float(
                source.get("MISO_WAKE_VAD_THRESHOLD", "0.5")
            )
            wake_energy_threshold_dbfs = float(
                source.get("MISO_WAKE_ENERGY_THRESHOLD_DBFS", "-60")
            )
            wake_activation_frames = int(
                source.get("MISO_WAKE_ACTIVATION_FRAMES", "1")
            )
            wake_cooldown_seconds = float(
                source.get("MISO_WAKE_COOLDOWN_SECONDS", "2")
            )
            wake_result_capacity = int(
                source.get("MISO_WAKE_RESULT_CAPACITY", "16")
            )
            stt_threads = int(source.get("MISO_STT_THREADS", "4"))
            stt_timeout_seconds = float(source.get("MISO_STT_TIMEOUT_SECONDS", "45"))
            stt_result_capacity = int(source.get("MISO_STT_RESULT_CAPACITY", "16"))
            stt_vad_threshold_dbfs = float(
                source.get("MISO_STT_VAD_THRESHOLD_DBFS", "-38")
            )
            stt_vad_minimum_speech_milliseconds = int(
                source.get("MISO_STT_VAD_MINIMUM_SPEECH_MILLISECONDS", "250")
            )
            stt_vad_end_silence_milliseconds = int(
                source.get("MISO_STT_VAD_END_SILENCE_MILLISECONDS", "600")
            )
            stt_vad_maximum_utterance_milliseconds = int(
                source.get("MISO_STT_VAD_MAXIMUM_UTTERANCE_MILLISECONDS", "15000")
            )
            stt_vad_pre_roll_milliseconds = int(
                source.get("MISO_STT_VAD_PRE_ROLL_MILLISECONDS", "200")
            )
            tts_volume = float(source.get("MISO_TTS_VOLUME", "1"))
            tts_chunk_bytes = int(source.get("MISO_TTS_CHUNK_BYTES", "4096"))
            tts_timeout_seconds = float(source.get("MISO_TTS_TIMEOUT_SECONDS", "60"))
            tts_result_capacity = int(source.get("MISO_TTS_RESULT_CAPACITY", "16"))
            conversation_listen_timeout_seconds = float(
                source.get("MISO_CONVERSATION_LISTEN_TIMEOUT_SECONDS", "8")
            )
            conversation_checkback_timeout_seconds = float(
                source.get("MISO_CONVERSATION_CHECKBACK_TIMEOUT_SECONDS", "5")
            )
            conversation_echo_guard_seconds = float(
                source.get("MISO_CONVERSATION_ECHO_GUARD_SECONDS", "0.6")
            )
            conversation_echo_memory_seconds = float(
                source.get("MISO_CONVERSATION_ECHO_MEMORY_SECONDS", "12")
            )
        except ValueError as error:
            raise ConfigError("MISO audio numeric settings are invalid") from error

        settings = cls(
            host=source.get("MISO_HOST", "127.0.0.1"),
            port=port,
            database_path=Path(
                source.get("MISO_DB_PATH", "/var/lib/miso/db/miso.sqlite3")
            ),
            state_dir=Path(source.get("MISO_STATE_DIR", "/var/lib/miso/state")),
            model_dir=model_dir,
            ollama_url=source.get("MISO_OLLAMA_URL", "http://127.0.0.1:11434"),
            ollama_model=source.get("MISO_OLLAMA_MODEL", "qwen3:1.7b"),
            provider_timeout_seconds=provider_timeout,
            log_level=source.get("MISO_LOG_LEVEL", "INFO").upper(),
            lan_ollama_url=source.get("MISO_LAN_OLLAMA_URL", "").strip() or None,
            lan_ollama_model=source.get("MISO_LAN_OLLAMA_MODEL", "qwen3:8b"),
            openai_api_key=source.get("MISO_OPENAI_API_KEY", "").strip() or None,
            openai_model=source.get("MISO_OPENAI_MODEL", "gpt-5-mini"),
            openai_base_url=source.get(
                "MISO_OPENAI_BASE_URL", "https://api.openai.com/v1"
            ),
            routing_health_timeout_seconds=routing_health_timeout,
            routing_attempt_timeout_seconds=routing_attempt_timeout,
            routing_stream_timeout_seconds=routing_stream_timeout,
            routing_health_cache_seconds=routing_health_cache,
            fast_lane_enabled=_boolean(
                source.get("MISO_FAST_LANE_ENABLED", "true"),
                "MISO_FAST_LANE_ENABLED",
            ),
            dashboard_token=source.get("MISO_DASHBOARD_TOKEN", "").strip() or None,
            dashboard_email=_email(
                source.get("MISO_DASHBOARD_EMAIL", "local@miso.invalid"),
                "MISO_DASHBOARD_EMAIL",
            ),
            access_team_domain=(
                source.get("MISO_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/") or None
            ),
            access_audience=source.get("MISO_ACCESS_AUDIENCE", "").strip() or None,
            google_calendar_enabled=_boolean(
                source.get("MISO_GOOGLE_CALENDAR_ENABLED", "false"),
                "MISO_GOOGLE_CALENDAR_ENABLED",
            ),
            google_calendar_client_path=Path(
                source.get(
                    "MISO_GOOGLE_CALENDAR_CLIENT_PATH",
                    "/etc/miso/google-calendar-client.json",
                )
            ),
            google_calendar_token_dir=Path(
                source.get(
                    "MISO_GOOGLE_CALENDAR_TOKEN_DIR",
                    "/var/lib/miso/state/google-calendar",
                )
            ),
            google_calendar_default_timezone=source.get(
                "MISO_GOOGLE_CALENDAR_DEFAULT_TIMEZONE", "Europe/London"
            ).strip(),
            google_calendar_default_id=source.get(
                "MISO_GOOGLE_CALENDAR_DEFAULT_ID", "primary"
            ).strip(),
            google_calendar_voice_email=(
                _email(
                    source["MISO_GOOGLE_CALENDAR_VOICE_EMAIL"],
                    "MISO_GOOGLE_CALENDAR_VOICE_EMAIL",
                )
                if source.get("MISO_GOOGLE_CALENDAR_VOICE_EMAIL", "").strip()
                else None
            ),
            weather_default_location=(
                source.get("MISO_WEATHER_DEFAULT_LOCATION", "").strip() or None
            ),
            developer_root=(
                Path(source["MISO_DEVELOPER_ROOT"])
                if source.get("MISO_DEVELOPER_ROOT")
                else None
            ),
            developer_commands=tuple(
                command.strip()
                for command in source.get(
                    "MISO_DEVELOPER_COMMANDS", "python3,git,rg,ls"
                ).split(",")
                if command.strip()
            ),
            audio_enabled=_boolean(
                source.get("MISO_AUDIO_ENABLED", "true"), "MISO_AUDIO_ENABLED"
            ),
            audio_capture_card=(
                source.get("MISO_AUDIO_CAPTURE_CARD", "").strip() or None
            ),
            audio_playback_card=(
                source.get("MISO_AUDIO_PLAYBACK_CARD", "").strip() or None
            ),
            audio_playback_backend=source.get(
                "MISO_AUDIO_PLAYBACK_BACKEND", "alsa"
            ).strip().casefold(),
            audio_playback_proxy=Path(
                source.get(
                    "MISO_AUDIO_PLAYBACK_PROXY",
                    "/run/miso-audio/playback.sock",
                )
            ),
            audio_device_index=audio_device_index,
            audio_sample_rate=audio_sample_rate,
            audio_playback_sample_rate=audio_playback_sample_rate,
            audio_channels=audio_channels,
            audio_chunk_milliseconds=audio_chunk_milliseconds,
            audio_buffer_milliseconds=audio_buffer_milliseconds,
            audio_reconnect_seconds=audio_reconnect_seconds,
            audio_silence_dbfs=audio_silence_dbfs,
            audio_clipping_ratio=audio_clipping_ratio,
            wake_enabled=_boolean(
                source.get("MISO_WAKE_ENABLED", "false"), "MISO_WAKE_ENABLED"
            ),
            wake_phrase=source.get("MISO_WAKE_PHRASE", "Miso").strip(),
            wake_executable=Path(
                source.get(
                    "MISO_WAKE_EXECUTABLE", "/opt/miso/openwakeword/bin/python"
                )
            ),
            wake_model=Path(
                source.get(
                    "MISO_WAKE_MODEL",
                    str(model_dir / "openwakeword" / "miso.onnx"),
                )
            ),
            wake_threshold=wake_threshold,
            wake_vad_threshold=wake_vad_threshold,
            wake_energy_threshold_dbfs=wake_energy_threshold_dbfs,
            wake_activation_frames=wake_activation_frames,
            wake_cooldown_seconds=wake_cooldown_seconds,
            wake_result_capacity=wake_result_capacity,
            display_wake_path=(
                Path(source["MISO_DISPLAY_WAKE_PATH"])
                if source.get("MISO_DISPLAY_WAKE_PATH", "").strip()
                else None
            ),
            stt_enabled=_boolean(
                source.get("MISO_STT_ENABLED", "false"), "MISO_STT_ENABLED"
            ),
            stt_executable=Path(
                source.get("MISO_STT_EXECUTABLE", "/usr/local/bin/whisper-cli")
            ),
            stt_model=Path(
                source.get(
                    "MISO_STT_MODEL",
                    str(model_dir / "whisper" / "ggml-tiny.bin"),
                )
            ),
            stt_threads=stt_threads,
            stt_timeout_seconds=stt_timeout_seconds,
            stt_prompt=source.get(
                "MISO_STT_PROMPT",
                "Miso. Comandos de casa en español e inglés. "
                "Pon un temporizador de ocho minutos. "
                "¿Cuánto tiempo queda en el temporizador? "
                "Añade leche a la lista de la compra. Enciende la luz de la cocina. "
                "Set a timer for twenty seconds. Add bread to the shopping list.",
            ).strip(),
            stt_result_capacity=stt_result_capacity,
            stt_vad_threshold_dbfs=stt_vad_threshold_dbfs,
            stt_vad_minimum_speech_milliseconds=(
                stt_vad_minimum_speech_milliseconds
            ),
            stt_vad_end_silence_milliseconds=stt_vad_end_silence_milliseconds,
            stt_vad_maximum_utterance_milliseconds=(
                stt_vad_maximum_utterance_milliseconds
            ),
            stt_vad_pre_roll_milliseconds=stt_vad_pre_roll_milliseconds,
            tts_enabled=_boolean(
                source.get("MISO_TTS_ENABLED", "false"), "MISO_TTS_ENABLED"
            ),
            tts_executable=Path(
                source.get("MISO_TTS_EXECUTABLE", "/opt/miso/piper/bin/python")
            ),
            tts_english_voice=source.get(
                "MISO_TTS_ENGLISH_VOICE", "en_GB-cori-medium"
            ).strip(),
            tts_english_model=Path(
                source.get(
                    "MISO_TTS_ENGLISH_MODEL",
                    str(model_dir / "piper" / "en_GB-cori-medium.onnx"),
                )
            ),
            tts_english_config=Path(
                source.get(
                    "MISO_TTS_ENGLISH_CONFIG",
                    str(model_dir / "piper" / "en_GB-cori-medium.onnx.json"),
                )
            ),
            tts_spanish_voice=source.get(
                "MISO_TTS_SPANISH_VOICE", "es_ES-davefx-medium"
            ).strip(),
            tts_spanish_model=Path(
                source.get(
                    "MISO_TTS_SPANISH_MODEL",
                    str(model_dir / "piper" / "es_ES-davefx-medium.onnx"),
                )
            ),
            tts_spanish_config=Path(
                source.get(
                    "MISO_TTS_SPANISH_CONFIG",
                    str(model_dir / "piper" / "es_ES-davefx-medium.onnx.json"),
                )
            ),
            tts_volume=tts_volume,
            tts_chunk_bytes=tts_chunk_bytes,
            tts_timeout_seconds=tts_timeout_seconds,
            tts_result_capacity=tts_result_capacity,
            conversation_enabled=_boolean(
                source.get("MISO_CONVERSATION_ENABLED", "true"),
                "MISO_CONVERSATION_ENABLED",
            ),
            conversation_listen_timeout_seconds=(
                conversation_listen_timeout_seconds
            ),
            conversation_checkback_timeout_seconds=(
                conversation_checkback_timeout_seconds
            ),
            conversation_acknowledgement=source.get(
                "MISO_CONVERSATION_ACKNOWLEDGEMENT", "Yes?"
            ).strip(),
            conversation_echo_guard_seconds=conversation_echo_guard_seconds,
            conversation_echo_memory_seconds=conversation_echo_memory_seconds,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.host or any(character.isspace() for character in self.host):
            raise ConfigError("MISO_HOST must be a non-empty address without spaces")
        if not 1 <= self.port <= 65535:
            raise ConfigError("MISO_PORT must be between 1 and 65535")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("MISO_LOG_LEVEL is invalid")
        if not self.database_path.is_absolute():
            raise ConfigError("MISO_DB_PATH must be absolute")
        if self.database_path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            raise ConfigError("MISO_DB_PATH must name a SQLite database")
        if not self.ollama_url.startswith(("http://", "https://")):
            raise ConfigError("MISO_OLLAMA_URL must be an HTTP URL")
        if not self.ollama_model.strip():
            raise ConfigError("MISO_OLLAMA_MODEL must not be empty")
        if self.lan_ollama_url is not None and not self.lan_ollama_url.startswith(
            ("http://", "https://")
        ):
            raise ConfigError("MISO_LAN_OLLAMA_URL must be an HTTP URL")
        if not self.lan_ollama_model.strip():
            raise ConfigError("MISO_LAN_OLLAMA_MODEL must not be empty")
        if not self.openai_model.strip():
            raise ConfigError("MISO_OPENAI_MODEL must not be empty")
        if not self.openai_base_url.startswith("https://"):
            raise ConfigError("MISO_OPENAI_BASE_URL must use HTTPS")
        if not 0 < self.provider_timeout_seconds <= 600:
            raise ConfigError("MISO_PROVIDER_TIMEOUT must be between 0 and 600 seconds")
        if not 0 < self.routing_health_timeout_seconds <= 30:
            raise ConfigError("MISO_ROUTING_HEALTH_TIMEOUT must be between 0 and 30")
        if not 0 < self.routing_attempt_timeout_seconds <= 600:
            raise ConfigError("MISO_ROUTING_ATTEMPT_TIMEOUT must be between 0 and 600")
        if not 0 < self.routing_stream_timeout_seconds <= 3_600:
            raise ConfigError("MISO_ROUTING_STREAM_TIMEOUT must be between 0 and 3600")
        if self.routing_stream_timeout_seconds < self.routing_attempt_timeout_seconds:
            raise ConfigError(
                "MISO_ROUTING_STREAM_TIMEOUT must not be below MISO_ROUTING_ATTEMPT_TIMEOUT"
            )
        if not 0 <= self.routing_health_cache_seconds <= 300:
            raise ConfigError(
                "MISO_ROUTING_HEALTH_CACHE_SECONDS must be between 0 and 300"
            )
        if self.host not in {"127.0.0.1", "::1", "localhost"} and not self.dashboard_token:
            raise ConfigError("MISO_DASHBOARD_TOKEN is required for a non-loopback host")
        if self.dashboard_token is not None and len(self.dashboard_token) < 32:
            raise ConfigError("MISO_DASHBOARD_TOKEN must contain at least 32 characters")
        if self.dashboard_email != _email(self.dashboard_email, "MISO_DASHBOARD_EMAIL"):
            raise ConfigError("MISO_DASHBOARD_EMAIL must be a normalized email address")
        if (self.access_team_domain is None) != (self.access_audience is None):
            raise ConfigError(
                "MISO_ACCESS_TEAM_DOMAIN and MISO_ACCESS_AUDIENCE must be set together"
            )
        if self.access_team_domain is not None:
            parsed_team_domain = urlsplit(self.access_team_domain)
            if (
                parsed_team_domain.scheme != "https"
                or not parsed_team_domain.hostname
                or not parsed_team_domain.hostname.endswith(".cloudflareaccess.com")
                or parsed_team_domain.username is not None
                or parsed_team_domain.password is not None
                or parsed_team_domain.port is not None
                or parsed_team_domain.path not in {"", "/"}
                or parsed_team_domain.query
                or parsed_team_domain.fragment
            ):
                raise ConfigError(
                    "MISO_ACCESS_TEAM_DOMAIN must be an HTTPS cloudflareaccess.com origin"
                )
            if not self.access_audience or not (
                16 <= len(self.access_audience) <= 128
                and all(
                    character.isalnum() or character in "_-"
                    for character in self.access_audience
                )
            ):
                raise ConfigError("MISO_ACCESS_AUDIENCE is invalid")
        for name, path in (
            ("MISO_GOOGLE_CALENDAR_CLIENT_PATH", self.google_calendar_client_path),
            ("MISO_GOOGLE_CALENDAR_TOKEN_DIR", self.google_calendar_token_dir),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")
        try:
            ZoneInfo(self.google_calendar_default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ConfigError(
                "MISO_GOOGLE_CALENDAR_DEFAULT_TIMEZONE must be a valid IANA timezone"
            ) from error
        if (
            not self.google_calendar_default_id
            or len(self.google_calendar_default_id) > 1024
            or any(ord(character) < 32 for character in self.google_calendar_default_id)
        ):
            raise ConfigError("MISO_GOOGLE_CALENDAR_DEFAULT_ID is invalid")
        if self.weather_default_location is not None and (
            len(self.weather_default_location) > 120
            or any(ord(character) < 32 for character in self.weather_default_location)
        ):
            raise ConfigError(
                "MISO_WEATHER_DEFAULT_LOCATION must be at most 120 printable characters"
            )
        if self.developer_root is not None and not self.developer_root.is_absolute():
            raise ConfigError("MISO_DEVELOPER_ROOT must be absolute")
        if not self.developer_commands:
            raise ConfigError("MISO_DEVELOPER_COMMANDS must not be empty")
        if any(Path(command).name != command for command in self.developer_commands):
            raise ConfigError("MISO_DEVELOPER_COMMANDS must contain command names")
        if self.audio_capture_card is not None and (
            len(self.audio_capture_card) > 32
            or not all(
                character.isalnum() or character in "_-"
                for character in self.audio_capture_card
            )
        ):
            raise ConfigError("MISO_AUDIO_CAPTURE_CARD must be a stable ALSA card ID")
        if self.audio_playback_backend not in {"alsa", "pulse"}:
            raise ConfigError("MISO_AUDIO_PLAYBACK_BACKEND must be alsa or pulse")
        if self.audio_playback_proxy.is_absolute() is False:
            raise ConfigError("MISO_AUDIO_PLAYBACK_PROXY must be absolute")
        if self.audio_playback_backend == "pulse":
            sink = self.audio_playback_card
            if sink is None:
                raise ConfigError(
                    "MISO_AUDIO_PLAYBACK_CARD must name a Pulse sink when Pulse playback is enabled"
                )
            if len(sink) > 255 or not all(
                character.isalnum() or character in "_.:-" for character in sink
            ):
                raise ConfigError("MISO_AUDIO_PLAYBACK_CARD is not a valid Pulse sink")
        elif self.audio_playback_card is not None and (
            len(self.audio_playback_card) > 32
            or not all(
                character.isalnum() or character in "_-"
                for character in self.audio_playback_card
            )
        ):
            raise ConfigError("MISO_AUDIO_PLAYBACK_CARD must be a stable ALSA card ID")
        if not 0 <= self.audio_device_index <= 255:
            raise ConfigError("MISO_AUDIO_DEVICE_INDEX must be between 0 and 255")
        if not 8_000 <= self.audio_sample_rate <= 192_000:
            raise ConfigError("MISO_AUDIO_SAMPLE_RATE must be between 8000 and 192000")
        if not 8_000 <= self.audio_playback_sample_rate <= 192_000:
            raise ConfigError(
                "MISO_AUDIO_PLAYBACK_SAMPLE_RATE must be between 8000 and 192000"
            )
        if not 1 <= self.audio_channels <= 8:
            raise ConfigError("MISO_AUDIO_CHANNELS must be between 1 and 8")
        if not 5 <= self.audio_chunk_milliseconds <= 1_000:
            raise ConfigError("MISO_AUDIO_CHUNK_MILLISECONDS must be between 5 and 1000")
        if not (
            self.audio_chunk_milliseconds
            <= self.audio_buffer_milliseconds
            <= 60_000
        ):
            raise ConfigError(
                "MISO_AUDIO_BUFFER_MILLISECONDS must fit at least one chunk and be at most 60000"
            )
        if not 0.05 <= self.audio_reconnect_seconds <= 60:
            raise ConfigError("MISO_AUDIO_RECONNECT_SECONDS must be between 0.05 and 60")
        if not -120 <= self.audio_silence_dbfs <= 0:
            raise ConfigError("MISO_AUDIO_SILENCE_DBFS must be between -120 and 0")
        if not 0.5 <= self.audio_clipping_ratio <= 1:
            raise ConfigError("MISO_AUDIO_CLIPPING_RATIO must be between 0.5 and 1")
        for name, path in (
            ("MISO_WAKE_EXECUTABLE", self.wake_executable),
            ("MISO_WAKE_MODEL", self.wake_model),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")
        if not self.wake_phrase or len(self.wake_phrase) > 80:
            raise ConfigError("MISO_WAKE_PHRASE must contain 1 to 80 characters")
        if not 0 < self.wake_threshold <= 1:
            raise ConfigError("MISO_WAKE_THRESHOLD must be between 0 and 1")
        if not 0 <= self.wake_vad_threshold <= 1:
            raise ConfigError("MISO_WAKE_VAD_THRESHOLD must be between 0 and 1")
        if not -120 <= self.wake_energy_threshold_dbfs <= 0:
            raise ConfigError(
                "MISO_WAKE_ENERGY_THRESHOLD_DBFS must be between -120 and 0"
            )
        if not 1 <= self.wake_activation_frames <= 20:
            raise ConfigError("MISO_WAKE_ACTIVATION_FRAMES must be between 1 and 20")
        if not 0 <= self.wake_cooldown_seconds <= 60:
            raise ConfigError("MISO_WAKE_COOLDOWN_SECONDS must be between 0 and 60")
        if not 1 <= self.wake_result_capacity <= 1_000:
            raise ConfigError("MISO_WAKE_RESULT_CAPACITY must be between 1 and 1000")
        if (
            self.display_wake_path is not None
            and not self.display_wake_path.is_absolute()
        ):
            raise ConfigError("MISO_DISPLAY_WAKE_PATH must be absolute")
        if self.wake_enabled and (
            self.audio_sample_rate != 16_000 or self.audio_channels != 1
        ):
            raise ConfigError("openWakeWord requires mono MISO_AUDIO_SAMPLE_RATE=16000")
        for name, path in (
            ("MISO_STT_EXECUTABLE", self.stt_executable),
            ("MISO_STT_MODEL", self.stt_model),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")
        if not 1 <= self.stt_threads <= 32:
            raise ConfigError("MISO_STT_THREADS must be between 1 and 32")
        if not 1 <= self.stt_timeout_seconds <= 600:
            raise ConfigError("MISO_STT_TIMEOUT_SECONDS must be between 1 and 600")
        if len(self.stt_prompt) > 500:
            raise ConfigError("MISO_STT_PROMPT must be at most 500 characters")
        if not 1 <= self.stt_result_capacity <= 1_000:
            raise ConfigError("MISO_STT_RESULT_CAPACITY must be between 1 and 1000")
        if not -120 <= self.stt_vad_threshold_dbfs <= 0:
            raise ConfigError("MISO_STT_VAD_THRESHOLD_DBFS must be between -120 and 0")
        if not 20 <= self.stt_vad_minimum_speech_milliseconds <= 10_000:
            raise ConfigError(
                "MISO_STT_VAD_MINIMUM_SPEECH_MILLISECONDS must be between 20 and 10000"
            )
        if not 20 <= self.stt_vad_end_silence_milliseconds <= 10_000:
            raise ConfigError(
                "MISO_STT_VAD_END_SILENCE_MILLISECONDS must be between 20 and 10000"
            )
        if not (
            self.stt_vad_minimum_speech_milliseconds
            <= self.stt_vad_maximum_utterance_milliseconds
            <= 120_000
        ):
            raise ConfigError(
                "MISO_STT_VAD_MAXIMUM_UTTERANCE_MILLISECONDS must fit minimum speech and be at most 120000"
            )
        if not 0 <= self.stt_vad_pre_roll_milliseconds <= 5_000:
            raise ConfigError(
                "MISO_STT_VAD_PRE_ROLL_MILLISECONDS must be between 0 and 5000"
            )
        for name, path in (
            ("MISO_TTS_EXECUTABLE", self.tts_executable),
            ("MISO_TTS_ENGLISH_MODEL", self.tts_english_model),
            ("MISO_TTS_ENGLISH_CONFIG", self.tts_english_config),
            ("MISO_TTS_SPANISH_MODEL", self.tts_spanish_model),
            ("MISO_TTS_SPANISH_CONFIG", self.tts_spanish_config),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")
        if not self.tts_english_voice or not self.tts_spanish_voice:
            raise ConfigError("MISO TTS voice names must not be empty")
        if not 0 <= self.tts_volume <= 2:
            raise ConfigError("MISO_TTS_VOLUME must be between 0 and 2")
        if not 256 <= self.tts_chunk_bytes <= 1_048_576 or self.tts_chunk_bytes % 2:
            raise ConfigError("MISO_TTS_CHUNK_BYTES must be even and between 256 and 1048576")
        if not 1 <= self.tts_timeout_seconds <= 600:
            raise ConfigError("MISO_TTS_TIMEOUT_SECONDS must be between 1 and 600")
        if not 1 <= self.tts_result_capacity <= 1_000:
            raise ConfigError("MISO_TTS_RESULT_CAPACITY must be between 1 and 1000")
        if not 1 <= self.conversation_listen_timeout_seconds <= 120:
            raise ConfigError(
                "MISO_CONVERSATION_LISTEN_TIMEOUT_SECONDS must be between 1 and 120"
            )
        if not 1 <= self.conversation_checkback_timeout_seconds <= 120:
            raise ConfigError(
                "MISO_CONVERSATION_CHECKBACK_TIMEOUT_SECONDS must be between 1 and 120"
            )
        if not 0 <= self.conversation_echo_memory_seconds <= 120:
            raise ConfigError(
                "MISO_CONVERSATION_ECHO_MEMORY_SECONDS must be between 0 and 120"
            )
        if not 0 <= self.conversation_echo_guard_seconds <= 10:
            raise ConfigError(
                "MISO_CONVERSATION_ECHO_GUARD_SECONDS must be between 0 and 10"
            )
        if not 1 <= len(self.conversation_acknowledgement) <= 100:
            raise ConfigError(
                "MISO_CONVERSATION_ACKNOWLEDGEMENT must contain 1 to 100 characters"
            )
        for name, path in (
            ("MISO_STATE_DIR", self.state_dir),
            ("MISO_MODEL_DIR", self.model_dir),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")

    def validate_runtime_paths(self) -> None:
        paths = (self.database_path.parent, self.state_dir, self.model_dir)
        if self.developer_root is not None:
            paths += (self.developer_root,)
        missing = [str(path) for path in paths if not path.is_dir()]
        if missing:
            raise ConfigError(f"required directories are missing: {', '.join(missing)}")
        if self.google_calendar_enabled and not self.google_calendar_client_path.is_file():
            raise ConfigError(
                "MISO_GOOGLE_CALENDAR_CLIENT_PATH is missing while Calendar is enabled"
            )
        if self.google_calendar_enabled:
            from miso.tools.google_calendar import (
                GoogleCalendarError,
                GoogleOAuthClient,
            )

            try:
                GoogleOAuthClient.load(self.google_calendar_client_path)
            except GoogleCalendarError as error:
                raise ConfigError(
                    f"Google Calendar OAuth client is invalid: {error}"
                ) from error
