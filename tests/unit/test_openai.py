import io
import json
from threading import Event
import unittest
from unittest.mock import patch

from miso.providers import (
    ChatRequest,
    OpenAIResponsesProvider,
    ProviderCancelled,
    ProviderError,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def sse(*events):
    return FakeResponse(
        b"".join(
            b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
            for event in events
        )
    )


class OpenAIResponsesProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenAIResponsesProvider("test-secret", "gpt-5-mini")

    def test_unconfigured_provider_is_offline_without_dispatch(self) -> None:
        provider = OpenAIResponsesProvider(None, "gpt-5-mini")
        self.assertFalse(provider.health().available)
        self.assertEqual(provider.health().detail, "not_configured")
        with self.assertRaisesRegex(ProviderError, "not configured"):
            list(provider.stream(ChatRequest(messages=()), Event()))

    @patch("miso.providers.openai.urlopen")
    def test_health_checks_configured_model(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse(
            json.dumps({"id": "gpt-5-mini"}).encode()
        )
        health = self.provider.health()
        self.assertTrue(health.available)
        self.assertEqual(health.detail, "ready")
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertTrue(request.full_url.endswith("/models/gpt-5-mini"))

    @patch("miso.providers.openai.urlopen")
    def test_streams_text_and_strict_function_call(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = sse(
            {"type": "response.output_text.delta", "delta": "Hola "},
            {
                "type": "response.function_call_arguments.done",
                "name": "timer_create",
                "arguments": '{"duration_seconds":5}',
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        request = ChatRequest(
            messages=({"role": "user", "content": "hola"},),
            tools=(
                {
                    "name": "timer_create",
                    "description": "Create timer",
                    "input_schema": {
                        "type": "object",
                        "properties": {"duration_seconds": {"type": "integer"}},
                        "required": ["duration_seconds"],
                        "additionalProperties": False,
                    },
                },
            ),
        )
        output = list(self.provider.stream(request, Event()))
        self.assertEqual(output[0].text, "Hola ")
        self.assertEqual(output[1].tool_call["name"], "timer_create")
        self.assertEqual(output[1].tool_call["arguments"], {"duration_seconds": 5})
        self.assertTrue(output[2].done)

        http_request = mocked_urlopen.call_args.args[0]
        payload = json.loads(http_request.data)
        self.assertFalse(payload["store"])
        self.assertNotIn("test-secret", http_request.data.decode())
        self.assertTrue(payload["tools"][0]["strict"])
        self.assertEqual(payload["tools"][0]["parameters"]["type"], "object")

    def test_pre_cancelled_request_never_dispatches(self) -> None:
        cancel = Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            list(self.provider.stream(ChatRequest(messages=()), cancel))

    @patch("miso.providers.openai.urlopen")
    def test_protocol_failure_is_bounded_and_does_not_leak_key(
        self, mocked_urlopen
    ) -> None:
        mocked_urlopen.return_value = sse(
            {"type": "response.failed", "response": {"error": "test-secret"}}
        )
        with self.assertRaises(ProviderError) as raised:
            list(self.provider.stream(ChatRequest(messages=()), Event()))
        self.assertNotIn("test-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
