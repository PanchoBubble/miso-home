import io
import json
from threading import Event
import unittest
from unittest.mock import patch

from miso.providers import ChatRequest, OllamaProvider, ProviderCancelled


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
                ChatRequest(messages=({"role": "user", "content": "hola"},)), Event()
            )
        )
        self.assertEqual(output[0].text, "Hola ")
        self.assertEqual(output[1].tool_call["name"], "timer_create")
        self.assertTrue(output[-1].done)

    def test_pre_cancelled_request_never_dispatches(self) -> None:
        cancel = Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            list(self.provider.stream(ChatRequest(messages=()), cancel))
