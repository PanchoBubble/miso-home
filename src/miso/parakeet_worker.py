"""Resident Parakeet recogniser behind a loopback HTTP endpoint.

sherpa-onnx and its ONNX runtime live in their own virtualenv, the way Piper
and openWakeWord already do, so the Miso runtime keeps its empty dependency
list. The worker loads the model once and answers `POST /inference` with the
same `{"text": ...}` shape whisper-server uses, which is what lets the lane in
miso.transcription treat the two interchangeably.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path


LOGGER = logging.getLogger("miso.parakeet")

# A minute of 16 kHz mono is far more than a spoken command, and the cap keeps
# a malformed request from turning into an allocation.
MAXIMUM_REQUEST_BYTES = 2_000_000


def _decode(body: bytes) -> tuple["object", int]:
    import numpy

    with wave.open(BytesIO(body)) as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError("expected mono 16-bit PCM")
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    samples = numpy.frombuffer(frames, dtype=numpy.int16)
    return samples.astype(numpy.float32) / 32768.0, rate


def build_recognizer(model_dir: Path, threads: int):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model_dir / "encoder.int8.onnx"),
        decoder=str(model_dir / "decoder.int8.onnx"),
        joiner=str(model_dir / "joiner.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=threads,
        model_type="nemo_transducer",
    )


def handler_type(recognizer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # Cheap liveness probe: the lane uses it to decide whether the
            # worker is worth sending audio to before it records a failure.
            self._send(200, {"status": "ok"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") not in {"/inference", ""}:
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "bad content length"})
                return
            if length <= 0 or length > MAXIMUM_REQUEST_BYTES:
                self._send(413, {"error": "unsupported audio size"})
                return
            body = self.rfile.read(length)
            try:
                samples, rate = _decode(body)
            except (ValueError, wave.Error, EOFError) as error:
                self._send(400, {"error": f"unreadable audio: {error}"})
                return
            try:
                stream = recognizer.create_stream()
                stream.accept_waveform(rate, samples)
                recognizer.decode_stream(stream)
                result = stream.result
            except Exception as error:  # pragma: no cover - runtime guard
                LOGGER.exception("parakeet decoding failed")
                self._send(500, {"error": str(error)[:200]})
                return
            self._send(
                200,
                {
                    "text": result.text,
                    # Parakeet v3 detects language internally but this build
                    # returns the field empty, so the caller falls back to its
                    # own guess rather than being handed a wrong verdict.
                    "language": getattr(result, "lang", "") or "",
                },
            )

        def log_message(self, *_args) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8911)
    parser.add_argument("--threads", type=int, default=4)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if options.host not in {"127.0.0.1", "localhost", "::1"}:
        # The Pi publishes a public hostname through cloudflared. A recogniser
        # bound anywhere but loopback would put household audio on the internet.
        raise SystemExit("parakeet worker must bind loopback")
    recognizer = build_recognizer(options.model_dir, options.threads)
    server = ThreadingHTTPServer((options.host, options.port), handler_type(recognizer))
    LOGGER.info("parakeet listening on %s:%s", options.host, options.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
