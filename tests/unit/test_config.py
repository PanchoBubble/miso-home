from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from miso.config import ConfigError, Settings


class SettingsTests(unittest.TestCase):
    def test_valid_environment_and_runtime_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ("db", "state", "models"):
                (root / child).mkdir()
            settings = Settings.from_env(
                {
                    "MISO_HOST": "127.0.0.1",
                    "MISO_PORT": "8091",
                    "MISO_DB_PATH": str(root / "db" / "miso.sqlite3"),
                    "MISO_STATE_DIR": str(root / "state"),
                    "MISO_MODEL_DIR": str(root / "models"),
                    "MISO_OLLAMA_URL": "http://127.0.0.1:11434",
                    "MISO_OLLAMA_MODEL": "qwen3:0.6b",
                    "MISO_PROVIDER_TIMEOUT": "60",
                    "MISO_LOG_LEVEL": "debug",
                }
            )
            settings.validate_runtime_paths()
            self.assertEqual(settings.port, 8091)
            self.assertEqual(settings.log_level, "DEBUG")
            self.assertEqual(settings.ollama_model, "qwen3:0.6b")

    def test_rejects_relative_database_path(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be absolute"):
            Settings.from_env({"MISO_DB_PATH": "relative.sqlite3"})

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaisesRegex(ConfigError, "between 1 and 65535"):
            Settings.from_env({"MISO_PORT": "70000"})

    def test_rejects_invalid_provider_timeout(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be numeric"):
            Settings.from_env({"MISO_PROVIDER_TIMEOUT": "slow"})

    def test_reports_missing_runtime_directories(self) -> None:
        settings = Settings(
            host="127.0.0.1",
            port=8090,
            database_path=Path("/missing/db/miso.sqlite3"),
            state_dir=Path("/missing/state"),
            model_dir=Path("/missing/models"),
            ollama_url="http://127.0.0.1:11434",
            ollama_model="qwen3:0.6b",
            provider_timeout_seconds=120,
            log_level="INFO",
        )
        with self.assertRaisesRegex(ConfigError, "required directories are missing"):
            settings.validate_runtime_paths()


if __name__ == "__main__":
    unittest.main()
