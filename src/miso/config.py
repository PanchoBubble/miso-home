"""Validated environment configuration for the Miso service."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when Miso configuration is unsafe or invalid."""


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
        for name, path in (
            ("MISO_STATE_DIR", self.state_dir),
            ("MISO_MODEL_DIR", self.model_dir),
        ):
            if not path.is_absolute():
                raise ConfigError(f"{name} must be absolute")

    def validate_runtime_paths(self) -> None:
        paths = (self.database_path.parent, self.state_dir, self.model_dir)
        missing = [str(path) for path in paths if not path.is_dir()]
        if missing:
            raise ConfigError(f"required directories are missing: {', '.join(missing)}")
