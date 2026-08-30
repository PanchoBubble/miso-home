"""Construct configured providers without coupling callers to implementations."""

from __future__ import annotations

from dataclasses import dataclass

from miso.config import Settings
from miso.providers.base import ModelProvider
from miso.providers.codex import CodexCliProvider
from miso.providers.ollama import LanOllamaProvider, OllamaProvider
from miso.providers.openai import OpenAIResponsesProvider


@dataclass(frozen=True, slots=True)
class ProviderSet:
    pi: ModelProvider
    lan: ModelProvider | None
    hosted: ModelProvider
    codex: ModelProvider | None = None

    def configured(self) -> tuple[ModelProvider, ...]:
        return tuple(
            provider
            for provider in (self.pi, self.lan, self.codex, self.hosted)
            if provider is not None
        )


def create_provider_set(settings: Settings) -> ProviderSet:
    timeout = settings.provider_timeout_seconds
    return ProviderSet(
        pi=OllamaProvider(settings.ollama_url, settings.ollama_model, timeout),
        lan=(
            LanOllamaProvider(
                settings.lan_ollama_url,
                settings.lan_ollama_model,
                timeout,
            )
            if settings.lan_ollama_url is not None
            else None
        ),
        hosted=OpenAIResponsesProvider(
            settings.openai_api_key,
            settings.openai_model,
            timeout,
            base_url=settings.openai_base_url,
        ),
        codex=(
            CodexCliProvider(
                settings.codex_cli_binary,
                settings.codex_cli_model,
                timeout,
            )
            if settings.codex_cli_enabled
            else None
        ),
    )
