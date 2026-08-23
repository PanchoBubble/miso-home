"""Isolated openWakeWord inference worker for :mod:`miso.wake`."""

from __future__ import annotations

import argparse
import struct
import sys

import numpy as np
from openwakeword.model import Model


_READY = b"OWW1"
_RESET = 0xFFFFFFFF


def _read_exact(size: int) -> bytes:
    value = sys.stdin.buffer.read(size)
    if len(value) != size:
        raise EOFError
    return value


def _response(status: int, score: float = 0.0, detail: bytes = b"") -> None:
    sys.stdout.buffer.write(struct.pack(">Bf", status, score))
    if status:
        sys.stdout.buffer.write(struct.pack(">I", len(detail)))
        sys.stdout.buffer.write(detail)
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--vad-threshold", type=float, required=True)
    options = parser.parse_args()
    wake = Model(
        wakeword_models=[options.model],
        vad_threshold=options.vad_threshold,
        inference_framework="onnx",
    )
    sys.stdout.buffer.write(_READY)
    sys.stdout.buffer.flush()
    while True:
        try:
            size = struct.unpack(">I", _read_exact(4))[0]
        except EOFError:
            return 0
        if size == _RESET:
            wake.reset()
            _response(0)
            continue
        try:
            pcm = _read_exact(size)
            samples = np.frombuffer(pcm, dtype="<i2")
            predictions = wake.predict(samples)
            score = max((float(value) for value in predictions.values()), default=0.0)
            _response(0, score)
        except EOFError:
            return 2
        except Exception as error:
            _response(1, detail=str(error).encode("utf-8", "replace")[:4096])


if __name__ == "__main__":
    raise SystemExit(main())
