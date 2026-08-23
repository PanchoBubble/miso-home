"""Validated environment configuration for the Miso service."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when Miso configuration is unsafe or invalid."""


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


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
    dashboard_token: str | None = field(default=None, repr=False)
    developer_root: Path | None = None
    developer_commands: tuple[str, ...] = ("python3", "git", "rg", "ls")
    audio_enabled: bool = True
    audio_capture_card: str | None = None
    audio_playback_card: str | None = None
    audio_device_index: int = 0
    audio_sample_rate: int = 16_000
    audio_channels: int = 1
    audio_chunk_milliseconds: int = 20
    audio_buffer_milliseconds: int = 1_000
    audio_reconnect_seconds: float = 1.0
    audio_silence_dbfs: float = -50.0
    audio_clipping_ratio: float = 0.98

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Settings":
        source = environ if values is None else values
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
        except ValueError as error:
            raise ConfigError("MISO routing timeouts must be numeric") from error
        try:
            audio_device_index = int(source.get("MISO_AUDIO_DEVICE_INDEX", "0"))
            audio_sample_rate = int(source.get("MISO_AUDIO_SAMPLE_RATE", "16000"))
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
        except ValueError as error:
            raise ConfigError("MISO audio numeric settings are invalid") from error

        settings = cls(
            host=source.get("MISO_HOST", "127.0.0.1"),
            port=port,
            database_path=Path(
                source.get("MISO_DB_PATH", "/var/lib/miso/db/miso.sqlite3")
            ),
            state_dir=Path(source.get("MISO_STATE_DIR", "/var/lib/miso/state")),
            model_dir=Path(source.get("MISO_MODEL_DIR", "/var/lib/miso/models")),
            ollama_url=source.get("MISO_OLLAMA_URL", "http://127.0.0.1:11434"),
            ollama_model=source.get("MISO_OLLAMA_MODEL", "qwen3:0.6b"),
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
            dashboard_token=source.get("MISO_DASHBOARD_TOKEN", "").strip() or None,
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
            audio_device_index=audio_device_index,
            audio_sample_rate=audio_sample_rate,
            audio_channels=audio_channels,
            audio_chunk_milliseconds=audio_chunk_milliseconds,
            audio_buffer_milliseconds=audio_buffer_milliseconds,
            audio_reconnect_seconds=audio_reconnect_seconds,
            audio_silence_dbfs=audio_silence_dbfs,
            audio_clipping_ratio=audio_clipping_ratio,
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
        if self.host not in {"127.0.0.1", "::1", "localhost"} and not self.dashboard_token:
            raise ConfigError("MISO_DASHBOARD_TOKEN is required for a non-loopback host")
        if self.dashboard_token is not None and len(self.dashboard_token) < 32:
            raise ConfigError("MISO_DASHBOARD_TOKEN must contain at least 32 characters")
        if self.developer_root is not None and not self.developer_root.is_absolute():
            raise ConfigError("MISO_DEVELOPER_ROOT must be absolute")
        if not self.developer_commands:
            raise ConfigError("MISO_DEVELOPER_COMMANDS must not be empty")
        if any(Path(command).name != command for command in self.developer_commands):
            raise ConfigError("MISO_DEVELOPER_COMMANDS must contain command names")
        for name, card_id in (
            ("MISO_AUDIO_CAPTURE_CARD", self.audio_capture_card),
            ("MISO_AUDIO_PLAYBACK_CARD", self.audio_playback_card),
        ):
            if card_id is not None and (
                len(card_id) > 32
                or not all(
                    character.isalnum() or character in "_-" for character in card_id
                )
            ):
                raise ConfigError(f"{name} must be a stable ALSA card ID")
        if not 0 <= self.audio_device_index <= 255:
            raise ConfigError("MISO_AUDIO_DEVICE_INDEX must be between 0 and 255")
        if not 8_000 <= self.audio_sample_rate <= 192_000:
            raise ConfigError("MISO_AUDIO_SAMPLE_RATE must be between 8000 and 192000")
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
