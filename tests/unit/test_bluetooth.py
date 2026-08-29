import unittest
from unittest.mock import patch

from miso.bluetooth import (
    configured_address,
    connect,
    is_connected,
    main,
    sink_address,
)


class SinkAddressTests(unittest.TestCase):
    def test_derives_address_from_pipewire_sink_name(self) -> None:
        self.assertEqual(
            sink_address("bluez_output.F8_5C_7D_19_EE_9E.1"),
            "F8:5C:7D:19:EE:9E",
        )

    def test_lowercase_sink_name_is_normalized(self) -> None:
        self.assertEqual(
            sink_address("bluez_output.f8_5c_7d_19_ee_9e.a2dp-sink"),
            "F8:5C:7D:19:EE:9E",
        )

    def test_non_bluetooth_sinks_are_ignored(self) -> None:
        for card in ("alsa_output.platform-bcm2835.stereo", "auto_null", "", None):
            self.assertIsNone(sink_address(card))

    def test_malformed_bluez_name_is_rejected(self) -> None:
        self.assertIsNone(sink_address("bluez_output.NOTANADDRESS.1"))
        self.assertIsNone(sink_address("bluez_output.F8_5C_7D_19_EE.1"))


class ConfiguredAddressTests(unittest.TestCase):
    def test_alsa_playback_maintains_no_speaker(self) -> None:
        self.assertIsNone(
            configured_address(
                {
                    "MISO_AUDIO_PLAYBACK_BACKEND": "alsa",
                    "MISO_AUDIO_PLAYBACK_CARD": "bluez_output.F8_5C_7D_19_EE_9E.1",
                }
            )
        )

    def test_pulse_playback_resolves_the_configured_sink(self) -> None:
        self.assertEqual(
            configured_address(
                {
                    "MISO_AUDIO_PLAYBACK_BACKEND": "pulse",
                    "MISO_AUDIO_PLAYBACK_CARD": "bluez_output.F8_5C_7D_19_EE_9E.1",
                }
            ),
            "F8:5C:7D:19:EE:9E",
        )

    def test_missing_configuration_is_not_an_error(self) -> None:
        self.assertIsNone(configured_address({}))


class ConnectionStateTests(unittest.TestCase):
    def test_connected_device_is_detected(self) -> None:
        info = "Device F8:5C:7D:19:EE:9E (public)\n\tPaired: yes\n\tConnected: yes\n"
        with patch("miso.bluetooth._bluetoothctl", return_value=(0, info)):
            self.assertTrue(is_connected("F8:5C:7D:19:EE:9E"))

    def test_disconnected_device_is_detected(self) -> None:
        info = "Device F8:5C:7D:19:EE:9E (public)\n\tPaired: yes\n\tConnected: no\n"
        with patch("miso.bluetooth._bluetoothctl", return_value=(0, info)):
            self.assertFalse(is_connected("F8:5C:7D:19:EE:9E"))

    def test_unknown_device_is_not_reported_connected(self) -> None:
        with patch("miso.bluetooth._bluetoothctl", return_value=(1, "not available")):
            self.assertFalse(is_connected("F8:5C:7D:19:EE:9E"))

    def test_connect_confirms_by_rereading_rather_than_exit_status(self) -> None:
        # bluetoothctl exits 0 even when the link never came up, so a successful
        # exit status alone must not be reported as a reconnect.
        info = "\tConnected: no\n"
        with patch("miso.bluetooth._bluetoothctl", return_value=(0, info)):
            self.assertFalse(connect("F8:5C:7D:19:EE:9E"))


class MainTests(unittest.TestCase):
    def test_already_connected_speaker_is_left_alone(self) -> None:
        with patch("miso.bluetooth.configured_address", return_value="AA:BB:CC:DD:EE:FF"):
            with patch("miso.bluetooth.is_connected", return_value=True) as connected:
                with patch("miso.bluetooth.connect") as attempted:
                    self.assertEqual(main([]), 0)
        connected.assert_called_once()
        attempted.assert_not_called()

    def test_disconnected_speaker_triggers_one_reconnect(self) -> None:
        with patch("miso.bluetooth.configured_address", return_value="AA:BB:CC:DD:EE:FF"):
            with patch("miso.bluetooth.is_connected", return_value=False):
                with patch("miso.bluetooth.connect", return_value=True) as attempted:
                    self.assertEqual(main([]), 0)
        attempted.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_powered_off_speaker_exits_cleanly_so_the_timer_keeps_retrying(self) -> None:
        with patch("miso.bluetooth.configured_address", return_value="AA:BB:CC:DD:EE:FF"):
            with patch("miso.bluetooth.is_connected", return_value=False):
                with patch("miso.bluetooth.connect", return_value=False):
                    self.assertEqual(main([]), 0)

    def test_host_without_a_bluetooth_speaker_does_nothing(self) -> None:
        with patch("miso.bluetooth.configured_address", return_value=None):
            with patch("miso.bluetooth.connect") as attempted:
                self.assertEqual(main([]), 0)
        attempted.assert_not_called()

    def test_explicit_address_overrides_configuration(self) -> None:
        with patch("miso.bluetooth.configured_address", return_value=None):
            with patch("miso.bluetooth.is_connected", return_value=False):
                with patch("miso.bluetooth.connect", return_value=True) as attempted:
                    self.assertEqual(main(["--address", "11:22:33:44:55:66"]), 0)
        attempted.assert_called_once_with("11:22:33:44:55:66")


if __name__ == "__main__":
    unittest.main()
