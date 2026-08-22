import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from miso.tools import (
    DeveloperShellController,
    InMemoryAuditLog,
    JsonlAuditLog,
    MCPToolAdapter,
    SchemaError,
    ToolDefinition,
    ToolRegistry,
    ToolStatus,
)


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "minLength": 1}},
    "required": ["value"],
    "additionalProperties": False,
}


class ToolRegistryTests(unittest.TestCase):
    def test_invalid_input_is_audited_and_never_executes(self) -> None:
        calls = []
        audit = InMemoryAuditLog()
        registry = ToolRegistry(audit)
        registry.register(
            ToolDefinition(
                "echo",
                "Echo a string",
                ECHO_SCHEMA,
                lambda arguments, _context: calls.append(arguments) or arguments,
                redact_fields=frozenset({"value"}),
            )
        )

        result = registry.invoke("echo", {"value": "secret", "extra": True})

        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        events = audit.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["arguments"]["value"], "[REDACTED]")
        self.assertEqual(events[1]["status"], "rejected")

    def test_success_returns_structured_result(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "echo",
                "Echo a string",
                ECHO_SCHEMA,
                lambda arguments, context: {
                    "value": arguments["value"],
                    "invocation_id": context.invocation_id,
                },
            )
        )
        result = registry.invoke("echo", {"value": "hola"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["value"], "hola")
        self.assertEqual(result.output["invocation_id"], result.invocation_id)
        self.assertEqual(result.as_dict()["status"], "success")

    def test_non_json_handler_output_is_an_error(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                "bad_output",
                "Return an invalid output",
                {"type": "object", "additionalProperties": False},
                lambda _arguments, _context: {"value": object()},
            )
        )
        result = registry.invoke("bad_output", {})
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertIn("not JSON serializable", result.error)

    def test_deadline_and_cancellation_are_bounded(self) -> None:
        registry = ToolRegistry()
        executed = Event()

        def wait(_arguments, context):
            executed.set()
            while not context.cancelled():
                time.sleep(0.005)
            context.raise_if_cancelled()
            return {}

        registry.register(
            ToolDefinition(
                "wait",
                "Wait cooperatively",
                {"type": "object", "additionalProperties": False},
                wait,
                timeout_seconds=0.2,
            )
        )
        started = time.monotonic()
        timed_out = registry.invoke("wait", {}, timeout_seconds=0.03)
        self.assertEqual(timed_out.status, ToolStatus.TIMEOUT)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(executed.is_set())

        cancelled = Event()
        cancelled.set()
        executed.clear()
        result = registry.invoke("wait", {}, cancel_event=cancelled)
        self.assertEqual(result.status, ToolStatus.CANCELLED)
        self.assertFalse(executed.is_set())

    def test_unknown_tool_and_unsafe_schema_are_rejected(self) -> None:
        registry = ToolRegistry()
        self.assertEqual(registry.invoke("shell", {}).status, ToolStatus.REJECTED)
        with self.assertRaisesRegex(SchemaError, "additionalProperties"):
            registry.register(
                ToolDefinition("loose", "Loose", {"type": "object"}, lambda _a, _c: {})
            )

    def test_jsonl_audit_log_is_private_and_valid(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit" / "tools.jsonl"
            registry = ToolRegistry(JsonlAuditLog(path))
            registry.invoke("missing", {})
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([event["event"] for event in events], [
                "tool_invocation_started", "tool_invocation_finished"
            ])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class FakeMCPClient:
    def __init__(self) -> None:
        self.calls = []

    def call_tool(self, server, tool, arguments, context):
        self.calls.append((server, tool, arguments, context.invocation_id))
        return {"content": [{"type": "text", "text": "done"}]}


class MCPAdapterTests(unittest.TestCase):
    def test_only_approved_mcp_servers_can_be_registered(self) -> None:
        client = FakeMCPClient()
        adapter = MCPToolAdapter(client, frozenset({"home"}))
        with self.assertRaisesRegex(ValueError, "not approved"):
            adapter.definition(
                local_name="bad",
                server="internet",
                remote_name="fetch",
                description="Fetch",
                input_schema=ECHO_SCHEMA,
            )
        registry = ToolRegistry()
        registry.register(
            adapter.definition(
                local_name="home_echo",
                server="home",
                remote_name="echo",
                description="Echo through home MCP",
                input_schema=ECHO_SCHEMA,
            )
        )
        result = registry.invoke("home_echo", {"value": "hola"})
        self.assertTrue(result.ok)
        self.assertEqual(client.calls[0][:3], ("home", "echo", {"value": "hola"}))


class DeveloperShellTests(unittest.TestCase):
    def test_mode_is_disabled_scoped_visible_and_expires(self) -> None:
        with TemporaryDirectory() as directory:
            clock = [10.0]
            controller = DeveloperShellController(
                Path(directory),
                ["printf"],
                monotonic=lambda: clock[0],
                now=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
            )
            registry = ToolRegistry()
            registry.register(controller.tool_definition(timeout_seconds=1))
            command = {"command": ["printf", "%s", "hello; exit 99"]}

            self.assertFalse(controller.status()["enabled"])
            self.assertEqual(
                registry.invoke("developer_command", command).status,
                ToolStatus.REJECTED,
            )
            enabled = controller.enable(5, approved_by="dashboard:test-user")
            self.assertTrue(enabled["enabled"])
            self.assertEqual(enabled["scope"], str(Path(directory).resolve()))
            result = registry.invoke("developer_command", command)
            self.assertTrue(result.ok)
            self.assertEqual(result.output["stdout"], "hello; exit 99")

            escaped = registry.invoke(
                "developer_command", {"command": ["printf", "x"], "cwd": ".."}
            )
            self.assertEqual(escaped.status, ToolStatus.REJECTED)
            clock[0] = 16.0
            self.assertFalse(controller.status()["enabled"])
            self.assertEqual(
                registry.invoke("developer_command", command).status,
                ToolStatus.REJECTED,
            )


if __name__ == "__main__":
    unittest.main()
