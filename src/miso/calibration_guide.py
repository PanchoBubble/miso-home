"""Click-paced Pi microphone capture, served only on this PC's loopback interface."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from miso.calibration import _levels

DURATION = 5
PCM_BYTES = 16000 * 2 * DURATION


def prompts() -> list[dict]:
    result = []
    for split in ("training", "evaluation"):
        for distance in (1, 3):
            for language, phrases in (
                ("en", ("Miso", "Miso, set a timer for five minutes", "Miso, what is the weather?")),
                ("es", ("Miso", "Miso, pon un temporizador de cinco minutos", "Miso, ¿qué tiempo hace?")),
            ):
                for phrase in phrases:
                    result.append(dict(split=split, distance_meters=distance,
                                       language=language, phrase=phrase, label="positive"))
    for language, phrase in (("en", "Milo, the soup is ready"),
                             ("es", "El piso es el mismo")):
        result.append(dict(split="training", distance_meters=1,
                           language=language, phrase=phrase, label="negative"))
    return result


# No remote WAVs: PCM is streamed over SSH, and Miso is restored even on failure.
# A transient watchdog also restores it if the SSH worker is killed unexpectedly.
REMOTE = r'''
import subprocess, sys
active = subprocess.run(['sudo', '-n', 'systemctl', 'is-active', '--quiet', 'miso.service']).returncode == 0
unit = 'miso-calibration-restore-' + sys.argv[1]
try:
    if active:
        subprocess.run(['sudo', '-n', 'systemd-run', '--quiet', '--unit=' + unit,
                        '--on-active=45s', '/usr/bin/systemctl', 'start', 'miso.service'], check=True)
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'miso.service'], check=True, timeout=20)
    with subprocess.Popen(['arecord', '--quiet', '--device', 'plughw:CARD=Device,DEV=0',
                           '--format', 'S16_LE', '--rate', '16000', '--channels', '1',
                           '--file-type', 'raw', '--duration', '5', '--fatal-errors'],
                          stdout=subprocess.PIPE) as capture:
        first = capture.stdout.read(640)
        if not first:
            raise RuntimeError('USB microphone did not supply audio')
        sys.stdout.buffer.write(b'RECORDING\n' + first)
        sys.stdout.buffer.flush()
        while True:
            chunk = capture.stdout.read(4096)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        if capture.wait(timeout=10):
            raise RuntimeError('USB microphone capture failed')
finally:
    if active:
        subprocess.run(['sudo', '-n', 'systemctl', 'start', 'miso.service'], check=True, timeout=20)
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit + '.timer'], timeout=10)
'''


def record_pi(host: str, on_recording) -> bytes:
    command = 'python3 -c ' + shlex.quote(REMOTE) + ' ' + secrets.token_hex(6)
    with subprocess.Popen(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', host, command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    ) as process:
        timer = threading.Timer(65, process.kill)
        timer.start()
        try:
            if process.stdout.readline() != b'RECORDING\n':
                raise RuntimeError('Could not open the Pi USB microphone. Check SSH and Miso service access.')
            on_recording()
            pcm, error = process.communicate()
            if process.returncode or len(pcm) != PCM_BYTES:
                raise RuntimeError('Recording failed or Miso could not restart. Check the Pi before retrying.')
            return pcm
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()


class Guide:
    def __init__(self, root: Path, recorder):
        self.root = root
        self.recorder = recorder
        self.lock = threading.RLock()
        self.items = prompts()
        self.saved: dict[int, dict] = {}
        self.phase = 'ready'
        self.error = None
        self.index = None
        self.consent = None
        self.started = None
        self.worker = None
        self.closed = False

    def status(self):
        with self.lock:
            return dict(phase=self.phase, error=self.error, index=self.index,
                        saved=sorted(self.saved), prompts=self.items,
                        started=self.started, duration=DURATION,
                        delete_raw_by=self.consent['delete_raw_by'] if self.consent else None)

    def start(self, index, consent):
        with self.lock:
            if self.closed:
                raise ValueError('This session has ended. Restart the helper for a new session.')
            if self.phase in ('preparing', 'recording'):
                raise ValueError('A recording is already in progress.')
            if type(index) is not int or not 0 <= index < len(self.items):
                raise ValueError('Invalid prompt.')
            if consent is not True:
                raise ValueError('Please confirm local recording before starting.')
            now = datetime.now(timezone.utc)
            if self.consent and now >= datetime.fromisoformat(self.consent['delete_raw_by']):
                raise ValueError('This recording session has expired.')
            if self.consent is None:
                self.consent = dict(confirmed=True, confirmed_at=now.isoformat(),
                                    delete_raw_by=(now + timedelta(hours=24)).isoformat())
            self.phase, self.index, self.error = 'preparing', index, None
            self.worker = threading.Thread(target=self._capture, args=(index,), daemon=True)
            self.worker.start()

    def _capture(self, index):
        def ready():
            with self.lock:
                self.phase, self.started = 'recording', time.time()
        try:
            pcm = self.recorder(ready)
            if len(pcm) != PCM_BYTES:
                raise ValueError('Incomplete recording. Please retry this prompt.')
            peak, rms = _levels(pcm)
            if rms < -65:
                raise ValueError('This clip is too quiet. Check the microphone and retry.')
            item = self.items[index]
            relative = f"{item['split']}/{index:02d}.wav"
            with self.lock:
                if self.closed:
                    return
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = path.with_suffix('.tmp')
                with wave.open(str(temporary), 'wb') as output:
                    output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    output.writeframes(pcm)
                temporary.replace(path)
                self.saved[index] = dict(
                    path=relative, label=item['label'], split=item['split'],
                    group_id=f"{item['split']}-{item['distance_meters']}m",
                    language=item['language'], distance_meters=item['distance_meters'],
                )
                manifest = self.root / 'manifest.json'
                draft = manifest.with_suffix('.tmp')
                draft.write_text(json.dumps(dict(consent=self.consent,
                                                 cases=list(self.saved.values())), indent=2))
                draft.replace(manifest)
                self.phase = 'saved'
                self.error = 'Audio is near clipping. Speak normally or move slightly back and retry.' if peak > -0.2 else None
        except Exception as error:
            with self.lock:
                self.phase, self.error = 'error', str(error)

    def expire(self):
        with self.lock:
            if self.consent and datetime.now(timezone.utc) >= datetime.fromisoformat(self.consent['delete_raw_by']):
                self.close()

    def close(self):
        with self.lock:
            self.closed = True
            shutil.rmtree(self.root, ignore_errors=True)
            self.saved.clear()
            self.phase = 'closed'


def make_server(guide: Guide, port: int):
    token = secrets.token_urlsafe(32)
    html = (Path(__file__).parent / 'web' / 'calibration-guide.html').read_text().replace('__TOKEN__', token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload, kind='application/json'):
            body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error': 'Invalid host'})
            if self.path == '/':
                return self.reply(200, html, 'text/html; charset=utf-8')
            if self.path == '/status':
                guide.expire()
                return self.reply(200, guide.status())
            self.reply(404, {})

        def do_POST(self):
            if not self.valid_host() or self.headers.get('X-Calibration-Token') != token:
                return self.reply(403, {'error': 'Reload this page before recording.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Invalid request')
                if self.path == '/record':
                    guide.start(data.get('index'), data.get('consent'))
                elif self.path == '/delete':
                    if guide.status()['phase'] in ('preparing', 'recording'):
                        raise ValueError('Wait for this five-second clip to finish first.')
                    guide.close()
                else:
                    return self.reply(404, {})
                self.reply(200, guide.status())
            except (ValueError, TypeError) as error:
                self.reply(400, {'error': str(error)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='pancho-pi')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output', type=Path, default=Path('.local/wake-corpus'))
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output.resolve() / ('guided-' + secrets.token_hex(8))
    guide = Guide(root, lambda ready: record_pi(args.host, ready))
    server = make_server(guide, args.port)
    atexit.register(guide.close)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def cleanup():
        while not guide.closed:
            time.sleep(1)
            guide.expire()
    threading.Thread(target=cleanup, daemon=True).start()
    print(f'Calibration ready: http://127.0.0.1:{server.server_port}', flush=True)
    print('No audio captured until Record is pressed. Closing this helper deletes session audio.', flush=True)
    try:
        server.serve_forever()
    finally:
        guide.close()
        if guide.worker:
            guide.worker.join(timeout=70)
        server.server_close()


if __name__ == '__main__':
    main()
