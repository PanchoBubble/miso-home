import unittest

from miso.providers import ChatChunk, ChatRequest, ProviderHealth
from miso.tools import ToolDefinition, ToolRegistry


class BoundaryTests(unittest.TestCase):
    def test_provider_value_objects(self) -> None:
        request = ChatRequest(messages=({"role": "user", "content": "hola"},))
        health = ProviderHealth(available=True, detail="ready", model="test")
        chunk = ChatChunk(text="hola", done=True)
        self.assertEqual(request.messages[0]["content"], "hola")
        self.assertTrue(health.available)
        self.assertTrue(chunk.done)

    def test_tool_registry_is_allowlisted_and_rejects_duplicates(self) -> None:
        registry = ToolRegistry()
        tool = ToolDefinition(
            name="echo",
            description="Echo a value",
            input_schema={"type": "object", "additionalProperties": False},
            handler=lambda value, _context: value,
        )
        registry.register(tool)
        self.assertEqual(registry.names(), ("echo",))
        self.assertIs(registry.get("echo"), tool)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(tool)
        with self.assertRaisesRegex(KeyError, "not allowlisted"):
            registry.get("shell")


if __name__ == "__main__":
    unittest.main()
