import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from threading import Event

from miso.config import ConfigError, Settings
from miso.providers import (
    ChatChunk,
    ChatRequest,
    CodexCliProvider,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderSet,
    create_provider_set,
)
from miso.routing import PROVIDER_PREFERENCE, ProviderRouter
from miso.tools import InMemoryAuditLog


FAKE_CODEX = '''#!/usr/bin/env python3
"""Stand-in for the real codex CLI so tests never need a login."""
import json
import os
import sys

record = os.environ.get("FAKE_CODEX_RECORD")
mode = os.environ.get("FAKE_CODEX_MODE", "stream")

if sys.argv[1:3] == ["login", "status"]:
    if os.environ.get("FAKE_CODEX_AUTHENTICATED") == "1":
        print("Logged in using ChatGPT")
        raise SystemExit(0)
    print("Not logged in", file=sys.stderr)
    raise SystemExit(1)

prompt = sys.stdin.read()
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}, handle)


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


if mode == "stream":
    emit({"id": "0", "msg": {"type": "task_started"}})
    emit({"id": "0", "msg": {"type": "agent_reasoning_delta", "delta": "thinking"}})
    emit({"id": "0", "msg": {"type": "agent_message_delta", "delta": "Madrid "}})
    emit({"id": "0", "msg": {"type": "agent_message_delta", "delta": "is warm."}})
    emit({"id": "0", "msg": {"type": "agent_message", "message": "Madrid is warm."}})
    tokens = {"type": "token_count", "input_tokens": 12, "output_tokens": 5}
    emit({"id": "0", "msg": tokens})
    done = {"type": "task_complete", "last_agent_message": "Madrid is warm."}
    emit({"id": "0", "msg": done})
elif mode == "final_only":
    emit({"type": "thread.started", "thread_id": "t1"})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Two."}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2}})
elif mode == "banner":
    sys.stdout.write("Reading prompt from stdin...\\n")
    sys.stdout.flush()
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Ok."}})
    emit({"type": "turn.completed"})
elif mode == "error_event":
    emit({"id": "0", "msg": {"type": "error", "message": "model overloaded"}})
    raise SystemExit(1)
elif mode == "crash":
    print("codex: unexpected failure", file=sys.stderr)
    raise SystemExit(3)
elif mode == "truncated":
    emit({"id": "0", "msg": {"type": "agent_message_delta", "delta": "half"}})
raise SystemExit(0)
'''


class FakeCodexBinary:
    """Installs a scripted codex stand-in on PATH for the duration of a test."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "codex"
        self.path.write_text(FAKE_CODEX, encoding="utf-8")
        self.path.chmod(0o755)
        self.record = directory / "record.json"

    def install(self, environment: dict, *, authenticated=True, mode="stream") -> None:
        existing = environment.get("PATH", "")
        environment["PATH"] = f"{self.directory}{os.pathsep}{existing}"
        environment["FAKE_CODEX_MODE"] = mode
        environment["FAKE_CODEX_RECORD"] = str(self.record)
        if authenticated:
            environment["FAKE_CODEX_AUTHENTICATED"] = "1"

    def invocation(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))


class CodexProviderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.mkdtemp(prefix="miso-fake-codex-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.binary = FakeCodexBinary(Path(directory))
        self.saved_environment = dict(os.environ)
        self.addCleanup(self._restore_environment)

    def _restore_environment(self) -> None:
        os.environ.clear()
        os.environ.update(self.saved_environment)

    def install(self, **kwargs) -> None:
        self.binary.install(os.environ, **kwargs)

    def isolate_path(self) -> None:
        """Empty PATH so the provider cannot find any codex binary."""
        os.environ["PATH"] = str(Path(self.binary.directory) / "empty")

    @staticmethod
    def request(content="what is the weather", tools=()):
        return ChatRequest(
            messages=({"role": "user", "content": content},),
            tools=tools,
        )

    def collect(self, provider, request=None):
        return list(provider.stream(request or self.request(), Event()))


class CodexHealthTests(CodexProviderTestCase):
    def test_health_is_ready_when_binary_is_present_and_authenticated(self) -> None:
        self.install()
        health = CodexCliProvider(timeout=10).health()
        self.assertEqual(health, ProviderHealth(True, "ready", None))

    def test_health_reports_missing_binary_without_raising(self) -> None:
        self.isolate_path()
        health = CodexCliProvider(timeout=10).health()
        self.assertFalse(health.available)
        self.assertEqual(health.detail, "binary_not_found")

    def test_health_reports_missing_login_without_raising(self) -> None:
        self.install(authenticated=False)
        health = CodexCliProvider(timeout=10).health()
        self.assertFalse(health.available)
        self.assertEqual(health.detail, "not_authenticated")

    def test_health_reports_unusable_absolute_path(self) -> None:
        health = CodexCliProvider("/nonexistent/codex", timeout=10).health()
        self.assertEqual(health.detail, "binary_not_found")


class CodexStreamTests(CodexProviderTestCase):
    def test_healthy_stream_yields_deltas_then_completion(self) -> None:
        self.install()
        chunks = self.collect(CodexCliProvider(timeout=30))
        self.assertEqual(
            [chunk.text for chunk in chunks if chunk.text],
            ["Madrid ", "is warm."],
        )
        self.assertTrue(chunks[-1].done)
        metrics = chunks[-1].metrics
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.prompt_tokens, 12)
        self.assertEqual(metrics.generated_tokens, 5)

    def test_run_is_pinned_to_a_read_only_sandbox_in_a_bare_directory(self) -> None:
        self.install()
        self.collect(CodexCliProvider("codex", "gpt-5-codex", timeout=30))
        invocation = self.binary.invocation()
        argv = invocation["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5-codex")
        self.assertEqual(argv[-1], "-")
        workdir = Path(argv[argv.index("--cd") + 1])
        self.assertEqual(Path(invocation["cwd"]).resolve(), workdir.resolve())
        self.assertFalse(workdir.exists(), "working directory outlived the request")

    def test_prompt_is_answer_only_and_carries_the_question(self) -> None:
        self.install()
        self.collect(
            CodexCliProvider(timeout=30),
            ChatRequest(
                messages=(
                    {"role": "system", "content": "Household assistant."},
                    {"role": "user", "content": "how tall is Ben Nevis"},
                )
            ),
        )
        prompt = self.binary.invocation()["prompt"]
        self.assertIn("how tall is Ben Nevis", prompt)
        self.assertIn("Household assistant.", prompt)
        self.assertIn("spoken answer only", prompt)
        self.assertIn("Never edit files", prompt)

    def test_final_message_is_used_when_the_cli_streams_no_deltas(self) -> None:
        self.install(mode="final_only")
        chunks = self.collect(CodexCliProvider(timeout=30))
        self.assertEqual([chunk.text for chunk in chunks if chunk.text], ["Two."])
        self.assertTrue(chunks[-1].done)
        self.assertEqual(chunks[-1].metrics.generated_tokens, 2)

    def test_non_json_banner_lines_are_ignored(self) -> None:
        self.install(mode="banner")
        chunks = self.collect(CodexCliProvider(timeout=30))
        self.assertEqual([chunk.text for chunk in chunks if chunk.text], ["Ok."])
        self.assertIsNone(chunks[-1].metrics)

    def test_missing_binary_fails_the_attempt_without_crashing(self) -> None:
        self.isolate_path()
        with self.assertRaises(ProviderError) as raised:
            self.collect(CodexCliProvider(timeout=30))
        self.assertIn("not installed", str(raised.exception))

    def test_unauthenticated_cli_never_reaches_the_stream(self) -> None:
        self.install(authenticated=False)
        provider = CodexCliProvider(timeout=10)
        self.assertFalse(provider.health().available)

    def test_error_event_becomes_a_provider_error(self) -> None:
        self.install(mode="error_event")
        with self.assertRaises(ProviderError) as raised:
            self.collect(CodexCliProvider(timeout=30))
        self.assertIn("model overloaded", str(raised.exception))

    def test_non_zero_exit_reports_the_stderr_tail(self) -> None:
        self.install(mode="crash")
        with self.assertRaises(ProviderError) as raised:
            self.collect(CodexCliProvider(timeout=30))
        self.assertIn("unexpected failure", str(raised.exception))

    def test_stream_without_completion_event_is_rejected(self) -> None:
        self.install(mode="truncated")
        with self.assertRaises(ProviderError):
            self.collect(CodexCliProvider(timeout=30))

    def test_cancelled_request_is_not_dispatched(self) -> None:
        self.install()
        cancel = Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            list(CodexCliProvider(timeout=30).stream(self.request(), cancel))
        self.assertFalse(self.binary.record.exists())


class CodexConfigurationTests(unittest.TestCase):
    def settings(self, **overrides) -> Settings:
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

    def test_provider_is_absent_until_the_flag_is_set(self) -> None:
        providers = create_provider_set(self.settings())
        self.assertIsNone(providers.codex)
        self.assertNotIn("codex-cli", [p.name for p in providers.configured()])

    def test_flag_adds_the_provider_between_lan_and_hosted(self) -> None:
        providers = create_provider_set(
            self.settings(
                codex_cli_enabled=True,
                lan_ollama_url="http://192.168.0.50:11434",
            )
        )
        self.assertEqual(providers.codex.name, "codex-cli")
        self.assertEqual(
            [provider.name for provider in providers.configured()],
            ["pi-ollama", "lan-ollama", "codex-cli", "hosted-gpt"],
        )

    def test_environment_defaults_keep_the_provider_off(self) -> None:
        base = {
            "MISO_DB_PATH": "/tmp/miso-test.sqlite3",
            "MISO_STATE_DIR": "/tmp",
            "MISO_MODEL_DIR": "/tmp",
        }
        self.assertFalse(Settings.from_env(base).codex_cli_enabled)
        enabled = Settings.from_env(
            {**base, "MISO_CODEX_CLI_ENABLED": "true", "MISO_CODEX_CLI_MODEL": "gpt-5"}
        )
        self.assertTrue(enabled.codex_cli_enabled)
        self.assertEqual(enabled.codex_cli_binary, "codex")
        self.assertEqual(enabled.codex_cli_model, "gpt-5")

    def test_empty_binary_is_rejected_when_enabled(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            self.settings(codex_cli_enabled=True, codex_cli_binary="  ").validate()
        self.assertIn("MISO_CODEX_CLI_BINARY", str(raised.exception))


class UnavailableProvider:
    def __init__(self, name: str) -> None:
        self._name = name
        self.stream_calls = 0

    @property
    def name(self) -> str:
        return self._name

    def health(self) -> ProviderHealth:
        return ProviderHealth(False, "not_configured", None)

    def stream(self, _request, _cancel):
        self.stream_calls += 1
        raise ProviderError("must not be used")
        yield  # pragma: no cover


class LocalProvider:
    def __init__(self, name: str) -> None:
        self._name = name
        self.stream_calls = 0

    @property
    def name(self) -> str:
        return self._name

    def health(self) -> ProviderHealth:
        return ProviderHealth(True, "ready", None)

    def stream(self, _request, _cancel):
        self.stream_calls += 1
        yield ChatChunk(text=f"from {self._name}")
        yield ChatChunk(done=True)


class CodexRoutingOrderTests(CodexProviderTestCase):
    def test_codex_is_preferred_over_the_local_models(self) -> None:
        self.assertEqual(
            PROVIDER_PREFERENCE,
            ("hosted-gpt", "codex-cli", "lan-ollama", "pi-ollama"),
        )

    def test_untooled_question_answers_via_codex_when_hosted_is_unconfigured(
        self,
    ) -> None:
        self.install()
        pi = LocalProvider("pi-ollama")
        lan = LocalProvider("lan-ollama")
        router = ProviderRouter(
            ProviderSet(
                pi=pi,
                lan=lan,
                hosted=UnavailableProvider("hosted-gpt"),
                codex=CodexCliProvider(timeout=30),
            ),
            InMemoryAuditLog(),
            health_timeout_seconds=10,
        )
        decision = router.plan(self.request())
        self.assertEqual(
            decision.candidates,
            ("hosted-gpt", "codex-cli", "lan-ollama", "pi-ollama"),
        )
        chunks = list(router.stream(self.request(), Event()))
        answered = [chunk for chunk in chunks if chunk.text]
        self.assertEqual(
            "".join(chunk.text for chunk in answered), "Madrid is warm."
        )
        self.assertEqual({chunk.provider for chunk in answered}, {"codex-cli"})
        self.assertEqual(pi.stream_calls, 0)
        self.assertEqual(lan.stream_calls, 0)

    def test_router_falls_through_to_local_when_codex_is_unavailable(self) -> None:
        self.isolate_path()
        lan = LocalProvider("lan-ollama")
        router = ProviderRouter(
            ProviderSet(
                pi=LocalProvider("pi-ollama"),
                lan=lan,
                hosted=UnavailableProvider("hosted-gpt"),
                codex=CodexCliProvider(timeout=30),
            ),
            InMemoryAuditLog(),
            health_timeout_seconds=10,
        )
        chunks = list(router.stream(self.request(), Event()))
        answered = [chunk for chunk in chunks if chunk.text]
        self.assertEqual(
            "".join(chunk.text for chunk in answered), "from lan-ollama"
        )
        self.assertEqual(lan.stream_calls, 1)


if __name__ == "__main__":
    unittest.main()
