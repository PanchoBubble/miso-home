import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from miso.config import Settings
from miso.http import create_server


class HealthIntegrationTests(unittest.TestCase):
    def test_health_endpoint_and_not_found(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
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
            )
            server = create_server(settings, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3
                )
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["service"], "miso")
                self.assertNotIn("database_path", payload)
                self.assertIn("timer_create", server.tool_registry.names())
                self.assertIn("shopping_add", server.tool_registry.names())

                connection.request("GET", "/missing")
                missing = connection.getresponse()
                self.assertEqual(missing.status, 404)
                self.assertEqual(json.loads(missing.read()), {"error": "not_found"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
