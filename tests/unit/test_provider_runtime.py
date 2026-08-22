from pathlib import Path
import unittest

from miso.config import Settings
from miso.providers import create_provider_set


class ProviderRuntimeTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "host": "127.0.0.1",
            "port": 8090,
            "database_path": Path("/tmp/miso-test.sqlite3"),
            "state_dir": Path("/tmp"),
            "model_dir": Path("/tmp"),
            "ollama_url": "http://127.0.0.1:11434",
            "ollama_model": "qwen3:0.6b",
            "provider_timeout_seconds": 30,
            "log_level": "INFO",
        }
        values.update(overrides)
        return Settings(**values)

    def test_lan_is_absent_until_configured_and_hosted_is_offline(self) -> None:
        providers = create_provider_set(self.settings())
        self.assertIsNone(providers.lan)
        self.assertEqual(providers.pi.name, "pi-ollama")
        self.assertFalse(providers.hosted.health().available)
        self.assertEqual(providers.hosted.health().detail, "not_configured")

    def test_lan_and_hosted_configuration_stays_provider_neutral(self) -> None:
        providers = create_provider_set(
            self.settings(
                lan_ollama_url="http://192.168.0.50:11434",
                lan_ollama_model="qwen3:8b",
                openai_api_key="secret",
            )
        )
        self.assertEqual(providers.lan.name, "lan-ollama")
        self.assertTrue(providers.hosted.configured)
        self.assertEqual(
            [provider.name for provider in providers.configured()],
            ["pi-ollama", "lan-ollama", "hosted-gpt"],
        )


if __name__ == "__main__":
    unittest.main()
