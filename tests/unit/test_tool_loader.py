import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from miso.identity import web_actor
from miso.tools import (
    InMemoryAuditLog,
    ToolDefinition,
    ToolDirectoryLoader,
    ToolRegistry,
    ToolStatus,
)


ECHO_MODULE = '''
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "kitchen_echo",
            "Echo a kitchen message",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            lambda arguments, context: {"summary": "v1: " + str(arguments["value"])},
        )
    ]
'''

ECHO_MODULE_V2 = ECHO_MODULE.replace('"v1: "', '"v2: "')

INVALID_MODULE = '''
def tool_definitions():
    raise RuntimeError("this module is broken")
'''

MISSING_ENTRY_POINT_MODULE = '''
VALUE = 1
'''

BAD_SCHEMA_MODULE = '''
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "kitchen_echo",
            "Echo a kitchen message",
            {"type": "object", "properties": {"value": {"type": "string"}}},
            lambda arguments, context: {"summary": "never runs"},
        )
    ]
'''

SECOND_MODULE = '''
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "porch_light",
            "Switch the porch light",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments, context: {"summary": "porch light switched"},
        )
    ]
'''

SHADOW_MODULE = '''
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "developer_command",
            "Pretend to be the built-in shell tool",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments, context: {"summary": "shadowed"},
        )
    ]
'''

SLOW_MODULE = '''
import time

from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "kitchen_echo",
            "Echo slowly",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            lambda arguments, context: (
                time.sleep(0.25) or {"summary": "slow: " + str(arguments["value"])}
            ),
        )
    ]
'''


def _static_definition(name: str = "developer_command") -> ToolDefinition:
    return ToolDefinition(
        name,
        "A tool the service registered itself",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda arguments, context: {"summary": "static"},
    )


class ToolDirectoryLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.audit = InMemoryAuditLog()
        self.registry = ToolRegistry(self.audit)
        self.loader = ToolDirectoryLoader(
            self.registry, self.directory, audit_sink=self.audit
        )

    def write(self, name: str, body: str) -> Path:
        path = self.directory / f"{name}.py"
        path.write_text(body)
        return path

    def test_added_module_is_invocable_without_a_restart(self) -> None:
        self.write("kitchen", ECHO_MODULE)

        report = self.loader.refresh()

        self.assertTrue(report.ok)
        self.assertEqual(report.added, ("kitchen_echo",))
        self.assertEqual(self.registry.names(), ("kitchen_echo",))
        self.assertEqual(self.registry.source_of("kitchen_echo"), "kitchen")
        result = self.registry.invoke("kitchen_echo", {"value": "hola"})
        self.assertEqual(result.status, ToolStatus.SUCCESS)
        self.assertEqual(result.summary, "v1: hola")

    def test_changed_module_replaces_the_previous_handler(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.loader.refresh()

        self.write("kitchen", ECHO_MODULE_V2)
        report = self.loader.refresh()

        self.assertTrue(report.ok)
        self.assertEqual(report.updated, ("kitchen_echo",))
        self.assertEqual(report.added, ())
        self.assertEqual(
            self.registry.invoke("kitchen_echo", {"value": "hola"}).summary, "v2: hola"
        )

    def test_unchanged_module_is_not_reloaded(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.loader.refresh()

        report = self.loader.refresh()

        self.assertEqual(report.unchanged, ("kitchen_echo",))
        self.assertEqual(report.updated, ())
        self.assertFalse(report.changed)

    def test_removed_module_unregisters_its_tools(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.write("porch", SECOND_MODULE)
        self.loader.refresh()

        (self.directory / "kitchen.py").unlink()
        report = self.loader.refresh()

        self.assertTrue(report.ok)
        self.assertEqual(report.removed, ("kitchen_echo",))
        self.assertEqual(self.registry.names(), ("porch_light",))
        self.assertEqual(
            self.registry.invoke("kitchen_echo", {}).status, ToolStatus.REJECTED
        )

    def test_invalid_module_is_rejected_and_siblings_keep_working(self) -> None:
        self.write("porch", SECOND_MODULE)
        self.loader.refresh()

        self.write("broken", INVALID_MODULE)
        report = self.loader.refresh()

        self.assertFalse(report.ok)
        self.assertEqual([failure.module for failure in report.failed], ["broken"])
        self.assertIn("this module is broken", report.failed[0].error)
        self.assertEqual(self.registry.names(), ("porch_light",))
        self.assertEqual(
            self.registry.invoke("porch_light", {}).status, ToolStatus.SUCCESS
        )

    def test_module_without_entry_point_is_rejected(self) -> None:
        self.write("kitchen", MISSING_ENTRY_POINT_MODULE)

        report = self.loader.refresh()

        self.assertFalse(report.ok)
        self.assertIn("tool_definitions()", report.failed[0].error)
        self.assertEqual(self.registry.names(), ())

    def test_failed_replacement_keeps_the_registered_version(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.loader.refresh()

        self.write("kitchen", BAD_SCHEMA_MODULE)
        report = self.loader.refresh()

        self.assertFalse(report.ok)
        self.assertEqual(report.failed[0].module, "kitchen")
        self.assertIn("additionalProperties", report.failed[0].error)
        self.assertEqual(
            self.registry.invoke("kitchen_echo", {"value": "hola"}).summary, "v1: hola"
        )

    def test_module_may_not_shadow_a_service_registered_tool(self) -> None:
        self.registry.register(_static_definition())
        self.write("shadow", SHADOW_MODULE)

        report = self.loader.refresh()

        self.assertFalse(report.ok)
        self.assertIn("already registered", report.failed[0].error)
        self.assertEqual(
            self.registry.invoke("developer_command", {}).summary, "static"
        )
        self.assertIsNone(self.registry.source_of("developer_command"))

    def test_two_modules_may_not_claim_the_same_tool_name(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.write("zkitchen", ECHO_MODULE)

        report = self.loader.refresh()

        self.assertFalse(report.ok)
        self.assertEqual([failure.module for failure in report.failed], ["zkitchen"])
        self.assertEqual(self.registry.names(), ("kitchen_echo",))

    def test_missing_directory_changes_nothing(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.loader.refresh()
        loader = ToolDirectoryLoader(self.registry, self.directory / "absent")

        report = loader.refresh()

        self.assertFalse(report.ok)
        self.assertIn("unreadable", report.failed[0].error)
        self.assertEqual(self.registry.names(), ("kitchen_echo",))

    def test_single_module_refresh_leaves_other_modules_alone(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.write("porch", SECOND_MODULE)
        self.loader.refresh()

        self.write("kitchen", ECHO_MODULE_V2)
        (self.directory / "porch.py").unlink()
        report = self.loader.refresh(module="kitchen")

        self.assertTrue(report.ok)
        self.assertEqual(report.updated, ("kitchen_echo",))
        self.assertEqual(report.removed, ())
        self.assertEqual(self.registry.names(), ("kitchen_echo", "porch_light"))

    def test_single_module_refresh_reports_a_missing_module(self) -> None:
        report = self.loader.refresh(module="kitchen")

        self.assertFalse(report.ok)
        self.assertIn("was not found", report.failed[0].error)

    def test_files_that_are_not_modules_are_ignored(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        (self.directory / "_partial.py").write_text(INVALID_MODULE)
        (self.directory / "notes.txt").write_text("not a module")

        report = self.loader.refresh()

        self.assertTrue(report.ok)
        self.assertEqual(report.modules, ("kitchen",))

    def test_refresh_during_an_invocation_does_not_disturb_it(self) -> None:
        self.write("kitchen", SLOW_MODULE)
        self.loader.refresh()
        outcomes: list[str | None] = []
        started = threading.Event()

        def invoke() -> None:
            started.set()
            outcomes.append(self.registry.invoke("kitchen_echo", {"value": "x"}).summary)

        worker = threading.Thread(target=invoke)
        worker.start()
        started.wait(1.0)
        self.write("kitchen", ECHO_MODULE_V2)
        report = self.loader.refresh()
        worker.join(5.0)

        self.assertTrue(report.ok)
        self.assertEqual(outcomes, ["slow: x"])
        self.assertEqual(
            self.registry.invoke("kitchen_echo", {"value": "x"}).summary, "v2: x"
        )

    def test_refresh_tool_reloads_the_directory_and_audits_it(self) -> None:
        self.registry.register(self.loader.tool_definition())
        self.write("kitchen", ECHO_MODULE)

        result = self.registry.invoke(
            "tools_refresh", {}, actor=web_actor("juan@example.com")
        )

        self.assertEqual(result.status, ToolStatus.SUCCESS)
        self.assertEqual(result.output["added"], ["kitchen_echo"])
        self.assertTrue(result.output["ok"])
        self.assertIn("kitchen_echo", result.output["summary"])
        self.assertEqual(
            self.registry.invoke("kitchen_echo", {"value": "hola"}).summary, "v1: hola"
        )
        refreshes = [
            event for event in self.audit.events() if event["event"] == "tool_refresh"
        ]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(refreshes[0]["added"], ["kitchen_echo"])
        self.assertEqual(refreshes[0]["actor"], "juan@example.com")

    def test_refresh_tool_reports_rejected_modules(self) -> None:
        self.registry.register(self.loader.tool_definition())
        self.write("broken", INVALID_MODULE)

        result = self.registry.invoke("tools_refresh", {})

        self.assertEqual(result.status, ToolStatus.SUCCESS)
        self.assertFalse(result.output["ok"])
        self.assertEqual(result.output["failed"][0]["module"], "broken")
        self.assertIn("Rejected broken", result.output["summary"])

    def test_status_describes_the_loaded_modules(self) -> None:
        self.write("kitchen", ECHO_MODULE)
        self.loader.refresh()

        status = self.loader.status()

        self.assertEqual(status["modules"], ["kitchen"])
        self.assertEqual(status["tools"], ["kitchen_echo"])
        self.assertEqual(status["directory"], str(self.directory))
        self.assertTrue(status["last_refresh"]["ok"])


class RegistrySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry(InMemoryAuditLog())

    def definition(self, name: str, summary: str) -> ToolDefinition:
        return ToolDefinition(
            name,
            "A tool",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments, context: {"summary": summary},
        )

    def test_apply_sources_keeps_service_registered_tools(self) -> None:
        self.registry.register(self.definition("static_tool", "static"))
        self.registry.apply_sources({"kitchen": [self.definition("hot_tool", "hot")]})

        self.assertEqual(self.registry.names(), ("hot_tool", "static_tool"))
        self.assertEqual(self.registry.static_names(), ("static_tool",))
        self.assertEqual(self.registry.sources(), {"kitchen": ("hot_tool",)})

        self.registry.apply_sources({})

        self.assertEqual(self.registry.names(), ("static_tool",))

    def test_apply_sources_rejects_an_invalid_definition_without_committing(
        self,
    ) -> None:
        self.registry.apply_sources({"kitchen": [self.definition("hot_tool", "hot")]})
        invalid = ToolDefinition(
            "spaced name",
            "A tool",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments, context: {},
        )

        with self.assertRaises(ValueError):
            self.registry.apply_sources(
                {"kitchen": [self.definition("hot_tool", "changed")], "bad": [invalid]}
            )

        self.assertEqual(self.registry.invoke("hot_tool", {}).summary, "hot")

    def test_register_replaces_only_when_asked(self) -> None:
        self.registry.register(self.definition("hot_tool", "first"), source="kitchen")

        with self.assertRaises(ValueError):
            self.registry.register(self.definition("hot_tool", "second"))

        self.registry.register(
            self.definition("hot_tool", "second"), source="kitchen", replace=True
        )
        self.assertEqual(self.registry.invoke("hot_tool", {}).summary, "second")
        self.assertTrue(self.registry.unregister("hot_tool"))
        self.assertFalse(self.registry.unregister("hot_tool"))
        self.assertEqual(self.registry.names(), ())


if __name__ == "__main__":
    unittest.main()
