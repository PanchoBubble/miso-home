import json
import subprocess
import unittest
from unittest.mock import patch

from miso.audio_proxy import discover_sinks, playback_command


class PulseAudioProxyTests(unittest.TestCase):
    def test_discovers_pulse_sink_name_and_description(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                [
                    {
                        "name": "bluez_output.F8_5C_7D_19_EE_9E.1",
                        "properties": {"device.description": "JBL Clip 3"},
                    }
                ]
            ).encode(),
            stderr=b"",
        )
        with patch("miso.audio_proxy.subprocess.run", return_value=result):
            self.assertEqual(
                discover_sinks(),
                [
                    {
                        "name": "bluez_output.F8_5C_7D_19_EE_9E.1",
                        "description": "JBL Clip 3",
                    }
                ],
            )

    def test_unavailable_pulse_server_is_an_error(self) -> None:
        result = subprocess.CompletedProcess(
            [], 1, stdout=b"", stderr=b"Connection refused"
        )
        with patch("miso.audio_proxy.subprocess.run", return_value=result):
            with self.assertRaisesRegex(OSError, "Connection refused"):
                discover_sinks()

    def test_playback_command_is_sink_specific_and_rejects_injection(self) -> None:
        command = playback_command(
            {
                "sink": "bluez_output.F8_5C_7D_19_EE_9E.1",
                "sample_rate": 22_050,
                "channels": 1,
                "sample_width": 2,
            }
        )
        self.assertIn("--device=bluez_output.F8_5C_7D_19_EE_9E.1", command)
        self.assertIn("--format=s16le", command)
        with self.assertRaisesRegex(ValueError, "invalid Pulse sink"):
            playback_command(
                {
                    "sink": "default; reboot",
                    "sample_rate": 22_050,
                    "channels": 1,
                    "sample_width": 2,
                }
            )


if __name__ == "__main__":
    unittest.main()
