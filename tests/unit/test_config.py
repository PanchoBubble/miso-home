from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from miso.config import ConfigError, Settings


class SettingsTests(unittest.TestCase):
    def test_wake_defaults_match_bundled_model(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual(settings.wake_threshold, 0.999)
        self.assertEqual(settings.wake_vad_threshold, 0.5)
        self.assertEqual(settings.wake_energy_threshold_dbfs, -60)
        self.assertEqual(settings.wake_activation_frames, 1)

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
                    "MISO_LAN_OLLAMA_URL": "http://192.168.0.50:11434",
                    "MISO_LAN_OLLAMA_MODEL": "qwen3:8b",
                    "MISO_OPENAI_API_KEY": "test-secret-key",
                    "MISO_OPENAI_MODEL": "gpt-5-mini",
                    "MISO_ROUTING_HEALTH_TIMEOUT": "1.5",
                    "MISO_ROUTING_ATTEMPT_TIMEOUT": "30",
                    "MISO_AUDIO_CAPTURE_CARD": "MisoUSB",
                    "MISO_AUDIO_PLAYBACK_CARD": "USB_Speaker",
                    "MISO_AUDIO_BUFFER_MILLISECONDS": "800",
                    "MISO_WAKE_ENABLED": "true",
                    "MISO_WAKE_PHRASE": "Hola Miso",
                    "MISO_WAKE_MODEL": str(root / "models" / "hola-miso.onnx"),
                    "MISO_WAKE_THRESHOLD": "0.62",
                    "MISO_WAKE_VAD_THRESHOLD": "0.55",
                    "MISO_WAKE_ENERGY_THRESHOLD_DBFS": "-42",
                    "MISO_WAKE_ACTIVATION_FRAMES": "3",
                    "MISO_STT_ENABLED": "true",
                    "MISO_STT_EXECUTABLE": "/opt/whisper/whisper-cli",
                    "MISO_STT_MODEL": str(root / "models" / "whisper.bin"),
                    "MISO_STT_THREADS": "3",
                    "MISO_STT_VAD_THRESHOLD_DBFS": "-36",
                    "MISO_TTS_ENABLED": "true",
                    "MISO_TTS_EXECUTABLE": "/opt/piper/bin/python",
                    "MISO_TTS_ENGLISH_VOICE": "custom-en",
                    "MISO_TTS_VOLUME": "0.8",
                    "MISO_CONVERSATION_LISTEN_TIMEOUT_SECONDS": "12",
                    "MISO_CONVERSATION_CHECKBACK_TIMEOUT_SECONDS": "4",
                    "MISO_CONVERSATION_ACKNOWLEDGEMENT": "Ready?",
                }
            )
            settings.validate_runtime_paths()
            self.assertEqual(settings.port, 8091)
            self.assertEqual(settings.log_level, "DEBUG")
            self.assertEqual(settings.ollama_model, "qwen3:0.6b")
            self.assertEqual(settings.lan_ollama_model, "qwen3:8b")
            self.assertEqual(settings.openai_model, "gpt-5-mini")
            self.assertNotIn("test-secret-key", repr(settings))
            self.assertEqual(settings.routing_health_timeout_seconds, 1.5)
            self.assertEqual(settings.routing_attempt_timeout_seconds, 30)
            self.assertEqual(settings.audio_capture_card, "MisoUSB")
            self.assertEqual(settings.audio_playback_card, "USB_Speaker")
            self.assertEqual(settings.audio_buffer_milliseconds, 800)
            self.assertTrue(settings.wake_enabled)
            self.assertEqual(settings.wake_phrase, "Hola Miso")
            self.assertEqual(settings.wake_model, root / "models" / "hola-miso.onnx")
            self.assertEqual(settings.wake_threshold, 0.62)
            self.assertEqual(settings.wake_vad_threshold, 0.55)
            self.assertEqual(settings.wake_energy_threshold_dbfs, -42)
            self.assertEqual(settings.wake_activation_frames, 3)
            self.assertTrue(settings.stt_enabled)
            self.assertEqual(settings.stt_threads, 3)
            self.assertEqual(settings.stt_vad_threshold_dbfs, -36)
            self.assertEqual(settings.stt_model, root / "models" / "whisper.bin")
            self.assertTrue(settings.tts_enabled)
            self.assertEqual(settings.tts_english_voice, "custom-en")
            self.assertEqual(settings.tts_volume, 0.8)
            self.assertEqual(settings.conversation_listen_timeout_seconds, 12)
            self.assertEqual(settings.conversation_checkback_timeout_seconds, 4)
            self.assertEqual(settings.conversation_acknowledgement, "Ready?")
            self.assertEqual(settings.audio_playback_sample_rate, 22_050)

    def test_rejects_relative_database_path(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be absolute"):
            Settings.from_env({"MISO_DB_PATH": "relative.sqlite3"})

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaisesRegex(ConfigError, "between 1 and 65535"):
            Settings.from_env({"MISO_PORT": "70000"})

    def test_rejects_invalid_provider_timeout(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be numeric"):
            Settings.from_env({"MISO_PROVIDER_TIMEOUT": "slow"})

    def test_rejects_insecure_hosted_provider_url(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must use HTTPS"):
            Settings.from_env({"MISO_OPENAI_BASE_URL": "http://api.example.test/v1"})

    def test_rejects_invalid_routing_timeout(self) -> None:
        with self.assertRaisesRegex(ConfigError, "routing timeouts must be numeric"):
            Settings.from_env({"MISO_ROUTING_ATTEMPT_TIMEOUT": "eventually"})

    def test_non_loopback_dashboard_requires_secret_token(self) -> None:
        with self.assertRaisesRegex(ConfigError, "DASHBOARD_TOKEN is required"):
            Settings.from_env({"MISO_HOST": "0.0.0.0"})
        settings = Settings.from_env(
            {
                "MISO_HOST": "0.0.0.0",
                "MISO_DASHBOARD_TOKEN": "dashboard-secret-at-least-32-chars",
            }
        )
        self.assertNotIn("dashboard-secret-at-least-32-chars", repr(settings))

    def test_rejects_relative_developer_scope(self) -> None:
        with self.assertRaisesRegex(ConfigError, "DEVELOPER_ROOT must be absolute"):
            Settings.from_env({"MISO_DEVELOPER_ROOT": "relative"})

    def test_rejects_volatile_or_invalid_audio_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "stable ALSA card ID"):
            Settings.from_env({"MISO_AUDIO_CAPTURE_CARD": "hw:2"})
        with self.assertRaisesRegex(ConfigError, "audio numeric settings"):
            Settings.from_env({"MISO_AUDIO_SAMPLE_RATE": "telephone"})
        with self.assertRaisesRegex(ConfigError, "must be true or false"):
            Settings.from_env({"MISO_AUDIO_ENABLED": "sometimes"})
        with self.assertRaisesRegex(ConfigError, "fit at least one chunk"):
            Settings.from_env(
                {
                    "MISO_AUDIO_CHUNK_MILLISECONDS": "40",
                    "MISO_AUDIO_BUFFER_MILLISECONDS": "20",
                }
            )

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

    def test_rejects_invalid_stt_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "STT_EXECUTABLE must be absolute"):
            Settings.from_env({"MISO_STT_EXECUTABLE": "whisper-cli"})
        with self.assertRaisesRegex(ConfigError, "STT_THREADS must be between"):
            Settings.from_env({"MISO_STT_THREADS": "0"})
        with self.assertRaisesRegex(ConfigError, "STT_VAD_THRESHOLD_DBFS"):
            Settings.from_env({"MISO_STT_VAD_THRESHOLD_DBFS": "4"})

    def test_rejects_invalid_wake_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "WAKE_MODEL must be absolute"):
            Settings.from_env({"MISO_WAKE_MODEL": "miso.onnx"})
        with self.assertRaisesRegex(ConfigError, "WAKE_THRESHOLD"):
            Settings.from_env({"MISO_WAKE_THRESHOLD": "0"})
        with self.assertRaisesRegex(ConfigError, "requires mono"):
            Settings.from_env(
                {"MISO_WAKE_ENABLED": "true", "MISO_AUDIO_CHANNELS": "2"}
            )

    def test_rejects_invalid_tts_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "TTS_EXECUTABLE must be absolute"):
            Settings.from_env({"MISO_TTS_EXECUTABLE": "python"})
        with self.assertRaisesRegex(ConfigError, "TTS_VOLUME must be between"):
            Settings.from_env({"MISO_TTS_VOLUME": "3"})
        with self.assertRaisesRegex(ConfigError, "TTS_CHUNK_BYTES"):
            Settings.from_env({"MISO_TTS_CHUNK_BYTES": "513"})

    def test_rejects_invalid_conversation_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigError, "LISTEN_TIMEOUT_SECONDS"):
            Settings.from_env({"MISO_CONVERSATION_LISTEN_TIMEOUT_SECONDS": "0"})
        with self.assertRaisesRegex(ConfigError, "ACKNOWLEDGEMENT"):
            Settings.from_env({"MISO_CONVERSATION_ACKNOWLEDGEMENT": ""})


if __name__ == "__main__":
    unittest.main()
