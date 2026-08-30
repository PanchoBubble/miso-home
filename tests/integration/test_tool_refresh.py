import http.client
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from miso.config import Settings
from miso.http import create_server
from miso.providers import ProviderHealth, ProviderSet
from miso.routing import ProviderRouter
from miso.tools import InMemoryAuditLog


PORCH_MODULE = '''
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "porch_light",
            "Switch the porch light",
            {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": ["on", "off"]}},
                "required": ["state"],
                "additionalProperties": False,
            },
            lambda arguments, context: {
                "summary": "porch light " + str(arguments["state"])
            },
        )
    ]
'''

BROKEN_MODULE = '''
def tool_definitions():
    raise RuntimeError("half written module")
'''


class OfflineProvider:
    name = "pi-ollama"

    def health(self):
        return ProviderHealth(False, "not_configured", "fake-model")

    def stream(self, request, cancel):
        yield from ()


class ToolRefreshIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.tools_dir = root / "tools.d"
        self.tools_dir.mkdir()
        settings = Settings(
            host="127.0.0.1",
            port=8090,
            database_path=root / "miso.sqlite3",
            state_dir=root,
            model_dir=root,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="qwen3:0.6b",
            provider_timeout_seconds=120,
            log_level="INFO",
            tools_dir=self.tools_dir,
        )
        router = ProviderRouter(
            ProviderSet(pi=OfflineProvider(), lan=None, hosted=None),
            InMemoryAuditLog(),
        )
        self.server = create_server(settings, port=0, router=router)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        body = None if payload is None else json.dumps(payload)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response, json.loads(content)

    def test_dropped_module_becomes_invocable_and_removal_takes_it_away(self) -> None:
        response, listing = self.request("GET", "/api/tools")
        self.assertEqual(response.status, 200)
        self.assertNotIn("porch_light", [tool["name"] for tool in listing["tools"]])

        (self.tools_dir / "porch.py").write_text(PORCH_MODULE)
        response, refreshed = self.request("POST", "/api/tools/refresh", {})

        self.assertEqual(response.status, 200)
        report = refreshed["result"]["output"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["added"], ["porch_light"])
        self.assertEqual(
            self.server.tool_registry.invoke("porch_light", {"state": "on"}).summary,
            "porch light on",
        )

        response, listing = self.request("GET", "/api/tools")
        entry = next(
            tool for tool in listing["tools"] if tool["name"] == "porch_light"
        )
        self.assertEqual(entry["source"], "porch")
        self.assertEqual(listing["refresh"]["modules"], ["porch"])

        (self.tools_dir / "porch.py").unlink()
        response, refreshed = self.request("POST", "/api/tools/refresh", {})

        self.assertEqual(response.status, 200)
        self.assertEqual(refreshed["result"]["output"]["removed"], ["porch_light"])
        self.assertNotIn("porch_light", self.server.tool_registry.names())

    def test_invalid_module_is_rejected_visibly_and_leaves_tools_working(self) -> None:
        (self.tools_dir / "porch.py").write_text(PORCH_MODULE)
        self.request("POST", "/api/tools/refresh", {})

        (self.tools_dir / "broken.py").write_text(BROKEN_MODULE)
        response, refreshed = self.request("POST", "/api/tools/refresh", {})

        self.assertEqual(response.status, 400)
        report = refreshed["result"]["output"]
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"][0]["module"], "broken")
        self.assertIn("half written module", report["failed"][0]["error"])
        self.assertEqual(
            self.server.tool_registry.invoke("porch_light", {"state": "off"}).summary,
            "porch light off",
        )

    def test_startup_loads_the_directory_before_the_first_request(self) -> None:
        (self.tools_dir / "porch.py").write_text(PORCH_MODULE)
        server = create_server(
            self.server.settings,
            port=0,
            router=self.server.router,
        )
        try:
            self.assertIn("porch_light", server.tool_registry.names())
            self.assertEqual(server.tool_loader.status()["modules"], ["porch"])
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
