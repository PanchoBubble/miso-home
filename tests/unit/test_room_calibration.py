import json
import subprocess
import sys
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from miso.room_calibration import RoomCapture, SAMPLE_BYTES
from miso.wake_corpus import load_wake_corpus


class RoomCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        now = datetime.now(timezone.utc)
        with wave.open(str(self.root / 'positive.wav'), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'xx' * 16000)
        payload = dict(consent=dict(confirmed=True, confirmed_at=now.isoformat(),
                                    delete_raw_by=(now+timedelta(hours=24)).isoformat()),
                       cases=[dict(path='positive.wav', label='positive', split='training',
                                   group_id='positive', language='en', distance_meters=1)])
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps(payload))
        self.capture = RoomCapture(self.manifest, 'unused-test-host')
        self.capture.ssh = Mock(return_value=subprocess.CompletedProcess([], 0))

    def tearDown(self):
        self.tmp.cleanup()

    def test_room_segments_are_disjoint_and_do_not_overwrite_original_manifest(self):
        original = self.manifest.read_bytes()
        self.capture.save(b'xx' * 16000, 4)
        self.capture.save(b'xx' * 16000, 5)
        corpus = load_wake_corpus(self.root/'room-manifest.json')
        self.assertEqual(len(corpus.cases_for('training', 'negative')), 1)
        self.assertEqual(len(corpus.cases_for('evaluation', 'negative')), 1)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertNotEqual(corpus.cases[-1].group_id, corpus.cases[-2].group_id)

    def test_capture_stream_saves_audio_and_checks_service_restored(self):
        real_popen = subprocess.Popen
        def local_worker(*args, **kwargs):
            return real_popen([sys.executable, '-c',
                "import sys; sys.stdout.buffer.write(b'RECORDING\\n'+b'xx'*16000)"], **kwargs)
        with patch('miso.room_calibration.subprocess.Popen', side_effect=local_worker), \
             patch('miso.room_calibration.TOTAL_SECONDS', 1):
            self.capture.run()
        self.assertEqual(self.capture.phase, 'completed')
        self.assertEqual(self.capture.seconds, 1)
        self.capture.ssh.assert_called_with('systemctl', 'is-active', '--quiet',
                                            'miso.service', check=True, timeout=10)
        self.assertEqual(len(load_wake_corpus(self.capture.output).cases), 2)

    def test_stopping_before_start_does_not_open_microphone(self):
        self.capture.stop_requested.set()
        with patch('miso.room_calibration.subprocess.Popen') as popen:
            self.capture.run()
        popen.assert_not_called()
        self.assertEqual(self.capture.phase, 'stopped')

    def test_inactive_miso_is_not_changed(self):
        self.capture.ssh.return_value = subprocess.CompletedProcess([], 3)
        with patch('miso.room_calibration.subprocess.Popen') as popen:
            self.capture.run()
        popen.assert_not_called()
        self.assertEqual(self.capture.phase, 'error')

    def test_stopping_uses_capture_unit(self):
        self.capture.stop()
        self.assertTrue(self.capture.stop_requested.is_set())
        self.capture.ssh.assert_called_once_with('sudo', '-n', 'systemctl', 'stop',
            self.capture.unit, capture_output=True, timeout=35)
