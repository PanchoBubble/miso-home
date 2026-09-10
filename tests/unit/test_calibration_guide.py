import json
import math
import struct
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miso.calibration_guide import Guide, PCM_BYTES, make_server, prompts, record_pi
from miso.wake_corpus import load_wake_corpus


def tone():
    return b''.join(struct.pack('<h', round(3000 * math.sin(i / 10)))
                    for i in range(PCM_BYTES // 2))


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'session'
        self.calls = 0
        def record(ready):
            self.calls += 1
            ready()
            return tone()
        self.guide = Guide(self.root, record)

    def tearDown(self):
        self.guide.close()
        self.tmp.cleanup()

    def test_reading_prompts_never_records_and_consent_is_required(self):
        self.guide.status()
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            self.guide.start(0, False)
        self.assertEqual(self.calls, 0)

    def test_saved_clips_load_as_disjoint_corpus_and_retry_replaces(self):
        for index in (0, 12, 0):
            self.guide.start(index, True)
            self.guide.worker.join(2)
            self.assertEqual(self.guide.phase, 'saved')
        corpus = load_wake_corpus(self.root / 'manifest.json')
        self.assertEqual(len(corpus.cases), 2)
        self.assertEqual(len(corpus.cases_for('training')), 1)
        self.assertEqual(len(corpus.cases_for('evaluation')), 1)
        self.assertEqual(self.calls, 3)
        self.assertEqual(len(list(self.root.rglob('*.wav'))), 2)

    def test_concurrent_click_does_not_start_second_capture(self):
        release = threading.Event()
        def record(ready):
            ready()
            release.wait(2)
            return tone()
        self.guide.recorder = record
        self.guide.start(0, True)
        try:
            with self.assertRaises(ValueError):
                self.guide.start(1, True)
        finally:
            release.set()
            self.guide.worker.join(2)

    def test_failed_capture_can_retry_and_no_partial_audio_is_kept(self):
        self.guide.recorder = lambda ready: b'bad'
        self.guide.start(0, True)
        self.guide.worker.join(2)
        self.assertEqual(self.guide.phase, 'error')
        self.assertFalse(self.root.exists())
        self.guide.recorder = lambda ready: tone()
        self.guide.start(0, True)
        self.guide.worker.join(2)
        self.assertEqual(self.guide.phase, 'saved')

    def test_close_deletes_audio_and_prevents_further_recording(self):
        self.guide.start(0, True)
        self.guide.worker.join(2)
        self.guide.close()
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            self.guide.start(0, True)

    def test_expiry_deletes_audio(self):
        self.guide.start(0, True)
        self.guide.worker.join(2)
        self.guide.consent['delete_raw_by'] = '2000-01-01T00:00:00+00:00'
        self.guide.expire()
        self.assertFalse(self.root.exists())
        self.assertTrue(self.guide.closed)

    def test_http_rejects_untrusted_record_requests(self):
        server = make_server(self.guide, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(url) as response:
                self.assertIn(b'Record this prompt', response.read())
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(
                    url + '/record', data=json.dumps(dict(index=0, consent=True)).encode(),
                    headers={'Content-Type': 'application/json'}))
            self.assertEqual(result.exception.code, 403)
            self.assertEqual(self.calls, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_ssh_ready_marker_does_not_consume_audio_bytes(self):
        real_popen = subprocess.Popen
        ready = []
        def local_worker(*args, **kwargs):
            return real_popen(
                [sys.executable, '-c',
                 "import sys; sys.stdout.buffer.write(b'RECORDING\\n' + b'xx' * 80000)"],
                **kwargs,
            )
        with patch('miso.calibration_guide.subprocess.Popen', side_effect=local_worker):
            pcm = record_pi('unused-test-host', lambda: ready.append(True))
        self.assertEqual(ready, [True])
        self.assertEqual(pcm, b'xx' * 80000)

    def test_prompt_coverage(self):
        positives = [p for p in prompts() if p['label'] == 'positive']
        self.assertEqual(len(positives), 24)
        self.assertEqual({(p['split'], p['language'], p['distance_meters']) for p in positives},
                         {(s, l, d) for s in ('training', 'evaluation')
                          for l in ('en', 'es') for d in (1, 3)})
