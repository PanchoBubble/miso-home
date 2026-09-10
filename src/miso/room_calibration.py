"""Bounded room-audio capture with a local Stop page and systemd restoration."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import signal
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from miso.wake_corpus import load_wake_corpus

SAMPLE_BYTES = 32000
TRAIN_SECONDS = 300
TOTAL_SECONDS = 3900
REMOTE_CAPTURE = '''
import subprocess, sys
subprocess.run(['systemctl', 'stop', 'miso.service'], check=True, timeout=20)
with subprocess.Popen(['arecord', '--quiet', '--device', 'plughw:CARD=Device,DEV=0',
                       '--format', 'S16_LE', '--rate', '16000', '--channels', '1',
                       '--file-type', 'raw', '--duration', '3900', '--fatal-errors'],
                      stdout=subprocess.PIPE) as capture:
    first = capture.stdout.read(640)
    if not first:
        raise RuntimeError('No USB audio')
    sys.stdout.buffer.write(b'RECORDING\\n' + first)
    sys.stdout.buffer.flush()
    while True:
        chunk = capture.stdout.read(32000)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    if capture.wait(timeout=10):
        raise RuntimeError('USB capture failed')
'''


class RoomCapture:
    def __init__(self, manifest: Path, host: str):
        self.manifest = manifest.resolve()
        load_wake_corpus(self.manifest)
        self.root = self.manifest.parent
        self.payload = json.loads(self.manifest.read_text())
        self.output = self.root / 'room-manifest.json'
        if self.output.exists():
            raise ValueError('Room capture already exists; use a fresh session.')
        self.host = host
        self.unit = 'miso-room-capture-' + secrets.token_hex(6)
        self.phase = 'preparing'
        self.error = None
        self.seconds = 0
        self.stop_requested = threading.Event()
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.process = None

    def ssh(self, *command, **kwargs):
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               self.host, shlex.join(command)], **kwargs)

    def status(self):
        with self.lock:
            return dict(phase=self.phase, error=self.error,
                        seconds=round(self.seconds), total=TOTAL_SECONDS,
                        stage='Training room audio' if self.seconds < TRAIN_SECONDS else 'Reserved false-wake test',
                        delete_raw_by=self.payload['consent']['delete_raw_by'])

    def stop(self):
        self.stop_requested.set()
        result = self.ssh('sudo', '-n', 'systemctl', 'stop', self.unit,
                          capture_output=True, timeout=35)
        if result.returncode and self.phase in ('preparing', 'recording'):
            raise RuntimeError('Could not confirm stop. The Pi watchdog still bounds capture to 67 minutes.')

    def save(self, pcm: bytes, part: int):
        split = 'training' if part < TRAIN_SECONDS // 60 else 'evaluation'
        path = self.root / split / f'room-{part:03d}.wav'
        path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        with wave.open(str(temporary), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(pcm)
        temporary.replace(path)
        self.payload['cases'].append(dict(path=str(path.relative_to(self.root)),
                                         label='negative', split=split, language='mixed',
                                         group_id=f'room-{split}'))
        draft = self.output.with_suffix('.tmp')
        draft.write_text(json.dumps(self.payload, indent=2))
        draft.replace(self.output)

    def run(self):
        pending = bytearray()
        part = 0
        try:
            active = self.ssh('systemctl', 'is-active', '--quiet', 'miso.service', timeout=10)
            if active.returncode:
                raise RuntimeError('Miso was not running; check the Pi before starting room capture.')
            if self.stop_requested.is_set():
                self.phase = 'stopped'
                return
            command = ['sudo', '-n', 'systemd-run', '--quiet', '--pipe', '--wait', '--collect',
                       '--unit=' + self.unit, '--property=RuntimeMaxSec=4000',
                       '--property=TimeoutStopSec=10',
                       '--property=ExecStopPost=/usr/bin/systemctl start miso.service',
                       '/usr/bin/python3', '-c', REMOTE_CAPTURE]
            with subprocess.Popen(
                ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                 '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                 self.host, shlex.join(command)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            ) as process:
                self.process = process
                watchdog = threading.Timer(4050, process.kill)
                watchdog.start()
                try:
                    if process.stdout.readline() != b'RECORDING\n':
                        raise RuntimeError('Could not start room capture; Miso restoration is managed by the Pi.')
                    self.phase = 'recording'
                    while True:
                        if self.stop_requested.is_set():
                            self.stop()
                        deadline = datetime.fromisoformat(self.payload['consent']['delete_raw_by'])
                        if datetime.now(timezone.utc) >= deadline or not self.root.exists():
                            self.stop()
                            raise RuntimeError('Recording session expired or was deleted.')
                        chunk = process.stdout.read(SAMPLE_BYTES)
                        if not chunk:
                            break
                        pending.extend(chunk)
                        with self.lock:
                            self.seconds += len(chunk) / SAMPLE_BYTES
                        while len(pending) >= 60 * SAMPLE_BYTES:
                            self.save(bytes(pending[:60 * SAMPLE_BYTES]), part)
                            del pending[:60 * SAMPLE_BYTES]
                            part += 1
                    code = process.wait(timeout=30)
                    if pending and self.root.exists():
                        self.save(bytes(pending[:len(pending) // 2 * 2]), part)
                    if self.stop_requested.is_set():
                        self.phase = 'stopped'
                    elif code or self.seconds < TOTAL_SECONDS:
                        raise RuntimeError('Capture ended early. Saved clips are retained; check Pi service status.')
                    else:
                        self.phase = 'completed'
                finally:
                    watchdog.cancel()
                    if process.poll() is None:
                        self.ssh('sudo', '-n', 'systemctl', 'stop', self.unit,
                                 capture_output=True, timeout=35)
                        process.kill()
                        process.wait()
            self.ssh('systemctl', 'is-active', '--quiet', 'miso.service', check=True, timeout=10)
        except Exception as error:
            self.phase, self.error = 'error', str(error)
        finally:
            if self.root.exists():
                (self.root / 'room-status.json').write_text(json.dumps(self.status(), indent=2))


PAGE = '''<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Miso room calibration</title>
<style>body{background:#151b19;color:#f3eee5;font:22px system-ui;margin:0;padding:36px}main{max-width:700px;margin:auto}h1{font-size:38px}p{line-height:1.5;color:#bdcec3}#time{font-size:64px;font-weight:bold}button{font:inherit;padding:18px;background:#eeb77f;color:#23190e;border:0;border-radius:12px}small{font-size:16px}</style>
<main><h1>Room sound calibration</h1><p id="phase">Preparing…</p><div id="time">65:00</div><p id="stage"></p>
<p>No more phrases to read. Leave normal room sounds or TV playing, and avoid deliberately saying “Miso”.</p>
<p>Miso resumes when recording ends. Audio stays on your PC, with the same deletion deadline as your voice clips.</p>
<button id="stop">Stop recording & resume Miso</button><p id="error"></p><small>Keep this PC awake and connected. This session lasts at most 65 minutes.</small></main>
<script>let sending=false;async function update(){try{const r=await fetch('/status');const s=await r.json();const n=Math.max(0,s.total-s.seconds);document.getElementById('time').textContent=Math.floor(n/60)+':'+String(n%60).padStart(2,'0');document.getElementById('phase').textContent=s.phase==='completed'?'Recording complete — Miso has resumed.':s.phase==='stopped'?'Recording stopped — Miso has resumed.':s.phase==='error'?'Recording needs attention':s.phase==='preparing'?'Preparing the Pi microphone…':'Recording room sound';document.getElementById('stage').textContent=s.stage;document.getElementById('stop').disabled=sending||!['preparing','recording'].includes(s.phase);document.getElementById('error').textContent=s.error||'';}catch(e){document.getElementById('error').textContent='Connection lost. The Pi automatically limits this recording.';}setTimeout(update,1000);}document.getElementById('stop').onclick=async()=>{sending=true;try{const r=await fetch('/stop',{method:'POST',headers:{'X-Calibration-Token':'__TOKEN__'}});if(!r.ok)alert('Could not confirm stop; please tell me.');}finally{sending=false;}};update();</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--host', default='pancho-pi')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--consent', action='store_true', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    capture = RoomCapture(args.manifest, args.host)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, code, body, kind='application/json'):
            value = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(value)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(value)
        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{args.port}':
                return self.reply(403, {})
            if self.path == '/':
                return self.reply(200, PAGE.replace('__TOKEN__', token), 'text/html; charset=utf-8')
            self.reply(200 if self.path == '/status' else 404, capture.status())
        def do_POST(self):
            if self.path != '/stop' or self.headers.get('X-Calibration-Token') != token:
                return self.reply(403, {})
            try:
                capture.stop()
                self.reply(200, capture.status())
            except Exception:
                self.reply(500, {})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    def stop(*_):
        def shutdown():
            try: capture.stop()
            finally: server.shutdown()
        threading.Thread(target=shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    capture.thread.start()
    print(f'Room calibration: http://127.0.0.1:{args.port}', flush=True)
    try: server.serve_forever()
    finally:
        capture.thread.join(timeout=40)
        server.server_close()


if __name__ == '__main__':
    main()
