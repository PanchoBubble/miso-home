import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "ops" / "bin" / "miso-kiosk-launch.sh"


class KioskLauncherTests(unittest.TestCase):
    def test_launches_companion_by_default_and_checks_root_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser_capture = root / "browser-arguments"
            curl_capture = root / "curl-arguments"
            self._write_executable(
                root / "curl",
                '#!/bin/sh\nprintf "%s\\n" "$@" >"$CURL_CAPTURE_PATH"\n',
            )
            self._write_executable(
                root / "chromium-browser",
                '#!/bin/sh\nprintf "%s\\n" "$@" >"$BROWSER_CAPTURE_PATH"\n',
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "BROWSER_CAPTURE_PATH": str(browser_capture),
                    "CURL_CAPTURE_PATH": str(curl_capture),
                    "PATH": f"{root}:{environment['PATH']}",
                }
            )
            environment.pop("MISO_KIOSK_URL", None)
            environment.pop("MISO_KIOSK_HEALTH_URL", None)

            result = subprocess.run(
                [str(LAUNCHER)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                browser_capture.read_text(encoding="utf-8").splitlines(),
                ["--kiosk", "--app=http://miso.local/companion"],
            )
            self.assertEqual(
                curl_capture.read_text(encoding="utf-8").splitlines()[-1],
                "http://miso.local/healthz",
            )

    def test_waits_for_health_and_launches_configured_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "browser-arguments"
            self._write_executable(root / "curl", "#!/bin/sh\nexit 0\n")
            self._write_executable(
                root / "chromium-browser",
                '#!/bin/sh\nprintf "%s\\n" "$@" >"$CAPTURE_PATH"\n',
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "CAPTURE_PATH": str(capture),
                    "MISO_KIOSK_URL": "http://miso.test/",
                    "PATH": f"{root}:{environment['PATH']}",
                }
            )

            result = subprocess.run(
                [str(LAUNCHER)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                ["--kiosk", "--app=http://miso.test/"],
            )

    def test_rejects_invalid_wait_seconds(self) -> None:
        environment = os.environ.copy()
        environment["MISO_KIOSK_WAIT_SECONDS"] = "later"

        result = subprocess.run(
            [str(LAUNCHER)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a non-negative integer", result.stderr)

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
