import io
import json
from urllib.error import HTTPError
from threading import Event
import unittest
from unittest.mock import patch

from miso.providers import (
    ChatRequest,
    LanOllamaProvider,
    OllamaProvider,
    ProviderCancelled,
    ProviderError,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class OllamaProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OllamaProvider("http://127.0.0.1:11434", "qwen3:0.6b")

    @patch("miso.providers.ollama.urlopen")
    def test_health_reports_installed_model(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse(
            json.dumps({"models": [{"name": "qwen3:0.6b"}]}).encode()
        )
        health = self.provider.health()
        self.assertTrue(health.available)
        self.assertEqual(health.detail, "ready")

    @patch("miso.providers.ollama.urlopen")
    def test_streams_text_and_validated_tool_candidate(self, mocked_urlopen) -> None:
        chunks = [
            {"message": {"content": "Hola "}, "done": False},
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "timer_create", "arguments": {"seconds": 5}}}
                    ],
                },
                "done": False,
            },
            {"message": {"content": ""}, "done": True},
        ]
        mocked_urlopen.return_value = FakeResponse(
            b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)
        )
        output = list(
            self.provider.stream(
                ChatRequest(
                    messages=({"role": "user", "content": "hola"},),
                    tools=(
                        {
                            "name": "timer_create",
                            "description": "Create timer",
                            "input_schema": {
                                "type": "object",
                                "additionalProperties": False,
                            },
                        },
                    ),
                ),
                Event(),
            )
        )
        self.assertEqual(output[0].text, "Hola ")
        self.assertEqual(output[1].tool_call["name"], "timer_create")
        self.assertTrue(output[-1].done)
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"], {"temperature": 0, "seed": 0})
        function = payload["tools"][0]["function"]
        self.assertEqual(function["name"], "timer_create")
        self.assertEqual(function["parameters"]["type"], "object")

    def test_pre_cancelled_request_never_dispatches(self) -> None:
        cancel = Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            list(self.provider.stream(ChatRequest(messages=()), cancel))

    def test_lan_provider_has_distinct_identity(self) -> None:
        provider = LanOllamaProvider("http://192.168.0.50:11434", "qwen3:8b")
        self.assertEqual(provider.name, "lan-ollama")


class OllamaThinkFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OllamaProvider("http://127.0.0.1:11434", "qwen3:1.7b")

    def _stream(self, contents):
        chunks = [{"message": {"content": item}, "done": False} for item in contents]
        chunks.append({"message": {"content": ""}, "done": True})
        payload = b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)
        with patch("miso.providers.ollama.urlopen") as mocked_urlopen:
            mocked_urlopen.return_value = FakeResponse(payload)
            output = list(
                self.provider.stream(
                    ChatRequest(messages=({"role": "user", "content": "hi"},)),
                    Event(),
                )
            )
        return "".join(chunk.text for chunk in output if chunk.text)

    def test_reasoning_span_is_never_spoken(self) -> None:
        self.assertEqual(
            self._stream(["<think>weigh the options</think>", "It is sunny."]),
            "It is sunny.",
        )

    def test_reasoning_span_split_across_chunks_is_removed(self) -> None:
        self.assertEqual(
            self._stream(["<thi", "nk>hmm</thi", "nk>Ready.", " Done."]),
            "Ready. Done.",
        )

    def test_visible_text_is_not_withheld_waiting_for_a_tag(self) -> None:
        self.assertEqual(self._stream(["Hola ", "Juan"]), "Hola Juan")

    def test_unterminated_reasoning_span_yields_no_speech(self) -> None:
        self.assertEqual(self._stream(["<think>still reasoning"]), "")


class OllamaThinkRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OllamaProvider("http://127.0.0.1:11434", "qwen3:1.7b")

    def test_rejected_think_field_is_retried_without_it(self) -> None:
        chunks = [
            {"message": {"content": "Hi there."}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
        body = b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            if len(sent) == 1:
                raise HTTPError(
                    request.full_url, 400, "does not support thinking", {}, None
                )
            return FakeResponse(body)

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            output = list(
                self.provider.stream(
                    ChatRequest(messages=({"role": "user", "content": "hi"},)),
                    Event(),
                )
            )

        self.assertEqual(len(sent), 2)
        self.assertIs(sent[0]["think"], False)
        self.assertNotIn("think", sent[1])
        self.assertEqual(
            "".join(chunk.text for chunk in output if chunk.text), "Hi there."
        )

    def test_other_http_errors_are_not_retried(self) -> None:
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            raise HTTPError(request.full_url, 500, "boom", {}, None)

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            with self.assertRaises(ProviderError):
                list(
                    self.provider.stream(
                        ChatRequest(messages=({"role": "user", "content": "hi"},)),
                        Event(),
                    )
                )
        self.assertEqual(len(calls), 1)


class OllamaJsonCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OllamaProvider("http://127.0.0.1:11434", "qwen3:1.7b", 120)

    def test_constrained_request_is_capped_and_non_streaming(self) -> None:
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append((json.loads(request.data), timeout))
            return FakeResponse(
                json.dumps(
                    {"message": {"content": '{"tool":"timer_list","arguments":{}}'}}
                ).encode()
            )

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            output = self.provider.complete_json(
                "pick a tool", "how long left", max_tokens=40, timeout_seconds=6
            )

        payload, timeout = sent[0]
        self.assertEqual(output, '{"tool":"timer_list","arguments":{}}')
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"]["num_predict"], 40)
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(timeout, 6)

    def test_inline_reasoning_is_stripped_from_the_selection(self) -> None:
        def fake_urlopen(request, timeout=None):
            return FakeResponse(
                json.dumps(
                    {
                        "message": {
                            "content": '<think>hmm</think>{"tool":null}',
                        }
                    }
                ).encode()
            )

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            self.assertEqual(
                self.provider.complete_json("pick", "hello"), '{"tool":null}'
            )

    def test_rejected_think_field_is_retried_without_it(self) -> None:
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            if len(sent) == 1:
                raise HTTPError(
                    request.full_url, 400, "does not support thinking", {}, None
                )
            return FakeResponse(json.dumps({"message": {"content": "{}"}}).encode())

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            self.assertEqual(self.provider.complete_json("pick", "hello"), "{}")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("think", sent[1])

    def test_transport_failure_is_a_bounded_provider_error(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise OSError("connection refused")

        with patch("miso.providers.ollama.urlopen", fake_urlopen):
            with self.assertRaises(ProviderError):
                self.provider.complete_json("pick", "hello")

    def test_pre_cancelled_selection_never_dispatches(self) -> None:
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(ProviderCancelled):
            self.provider.complete_json("pick", "hello", cancel=cancelled)
