"""Persistent Piper model worker using a small length-prefixed PCM protocol."""

from __future__ import annotations

import argparse
import json
import struct
import sys

from piper import PiperVoice, SynthesisConfig


READY = 0xFFFFFFFF
ERROR = 0xFFFFFFFE


def _write_frame(value: bytes) -> None:
    sys.stdout.buffer.write(struct.pack(">I", len(value)))
    sys.stdout.buffer.write(value)
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--chunk-bytes", type=int, default=4096)
    options = parser.parse_args()
    voice = PiperVoice.load(options.model, config_path=options.config)
    sys.stdout.buffer.write(struct.pack(">I", READY))
    sys.stdout.buffer.flush()
    while True:
        header = sys.stdin.buffer.read(4)
        if not header:
            return 0
        if len(header) != 4:
            return 2
        request_size = struct.unpack(">I", header)[0]
        encoded = sys.stdin.buffer.read(request_size)
        if len(encoded) != request_size:
            return 2
        try:
            request = json.loads(encoded)
            text = request["text"]
            volume = float(request["volume"])
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            for chunk in voice.synthesize(
                text, syn_config=SynthesisConfig(volume=volume)
            ):
                pcm = chunk.audio_int16_bytes
                for offset in range(0, len(pcm), options.chunk_bytes):
                    _write_frame(pcm[offset : offset + options.chunk_bytes])
            _write_frame(b"")
        except Exception as error:
            message = str(error).encode("utf-8", "replace")[:4096]
            sys.stdout.buffer.write(struct.pack(">I", ERROR))
            _write_frame(message)


if __name__ == "__main__":
    raise SystemExit(main())
