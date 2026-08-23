# Miso offline wake-word benchmark

The wake path uses a custom openWakeWord ONNX model behind two local speech
gates: openWakeWord's bundled Silero VAD and a dependency-free RMS energy gate.
Inference runs in a pinned isolated environment and has no network path. The
service consumes an independent bounded audio tap, so wake detection cannot
remove chunks from transcription.

## Acceptance targets

Use mono 16 kHz S16_LE recordings from the intended USB microphone. A candidate
configuration passes when it satisfies all of these targets:

- at least 80% recall across English and Spanish speakers;
- at least 80% recall at the 3 m target distance;
- at most 0.5 false activations per hour of quiet-room household audio; and
- no network access during model loading or inference.

Report English, Spanish, near-field, and 3 m results separately as well as the
aggregate. Synthetic TTS is suitable for model selection, but final acceptance
requires human speech captured through the deployment microphone.

## Model training

`ops/train-wakeword.py` reproducibly trains `Miso` as a single target phrase
with openWakeWord 0.6.0. It uses 20,000 augmented positive examples across
English and Spanish TTS voices, 10,000 confusable negatives, and 20,000 windows
from the upstream large negative feature set. Confusables include `Milo`,
`Mia`, `missile`, `missing`, `mismo`, `misa`, `piso`, `quiso`, `hizo`, `aviso`,
and `permiso`. Voice identities and the final 20% of the general-negative
timeline are held out from training.

The generator currently requires macOS `say`, FFmpeg at
`/opt/homebrew/bin/ffmpeg`, Python 3.11, `openwakeword==0.6.0`, `onnx==1.18.0`,
and scikit-learn. Download openWakeWord's
[`validation_set_features.npy`](https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy),
then run:

```bash
python ops/train-wakeword.py \
  --output-directory .local/wake-training/run \
  --negative-features .local/wake-training/validation_set_features.npy
```

The fixed seed, disjoint voice sets, augmentation parameters, selected
threshold, complete threshold matrix, and ONNX/sklearn parity check are written
to `metrics.json`. A run exits nonzero unless it finds at least 80% aggregate
and per-language recall with no more than 0.5 false activations per hour.

The accepted model is committed at `models/openwakeword/miso.onnx`. Install it
with its pinned checksum:

```bash
sudo ops/install-openwakeword.sh
sudo ops/install-miso-runtime.sh
```

If the exporter creates `miso.onnx.data`, keep it beside the graph and pass its
SHA-256 as the third installer argument. The installer pins openWakeWord,
downloads its shared feature and VAD models at install time, validates the
custom graph, and then enables offline detection. Runtime never downloads a
model.

## Recording manifest

Record multiple people saying Miso naturally before an English or Spanish
command at 1 m and 3 m. Add at least one hour of negative room audio containing
conversation, television, music, silence, and the confusable words. WAV files
must be uncompressed mono 16 kHz 16-bit PCM. The manifest is JSON relative to
its own location:

```json
{
  "cases": [
    {
      "path": "positive/en-speaker-1-3m.wav",
      "label": "positive",
      "language": "en",
      "distance_meters": 3
    },
    {
      "path": "positive/es-speaker-1-3m.wav",
      "label": "positive",
      "language": "es",
      "distance_meters": 3
    },
    {
      "path": "negative/quiet-room-01.wav",
      "label": "negative",
      "language": "mixed"
    }
  ]
}
```

Run a threshold matrix and retain each JSON result with the model checksum:

```bash
for threshold in 0.99 0.995 0.9975 0.999; do
  PYTHONPATH=src /opt/miso/openwakeword/bin/python \
    ops/benchmark-wakeword.py \
    --manifest /path/to/manifest.json \
    --model /var/lib/miso/models/openwakeword/miso.onnx \
    --threshold "${threshold}" \
    --output "/path/to/results-${threshold}.json"
done
```

Tune `MISO_WAKE_THRESHOLD`, `MISO_WAKE_VAD_THRESHOLD`,
`MISO_WAKE_ENERGY_THRESHOLD_DBFS`, and `MISO_WAKE_ACTIVATION_FRAMES` from those
results. Do not select a threshold against the training clips.

## Synthetic model-selection result

The 2026-08-23 fixed-seed run produced model SHA-256
`f7d67c3d67911e65ff51a10967661b56b1aead161efe3816646a5190aa2ba59f`.
At threshold `0.999` with one activation frame, the disjoint
synthetic holdout measured:

- 95.5% aggregate recall across 2,000 positive examples;
- 96.29% English recall and 94.72% Spanish recall;
- 0.05% false-positive rate across 2,000 confusable examples; and
- one activation in 2.139 hours of held-out general-negative audio features,
  or 0.4675 activations/hour.

Replaying 500 independently augmented two-second clips through the actual
isolated streaming worker with Silero VAD `0.5` and an energy floor of `-60`
dBFS measured 96.8% recall in both English and Spanish. A two-frame debounce or
the former `-45` dBFS energy floor suppressed valid trailing score peaks, so the
deployed policy uses one frame and relies on the stricter model threshold plus
both speech gates.

The full machine-readable result is in
`benchmarks/openwakeword/training-metrics.json`. These measurements select a
software candidate; they do not replace the physical-microphone acceptance
described below.

## Current hardware gate

On 2026-08-23 the Pancho Pi exposed only its two HDMI playback cards and no
capture PCM. The runtime and repeatable scorer can therefore be validated in
software, but human distance and quiet-room measurements remain blocked until
the intended USB microphone is attached. This limitation must stay visible in
the beads acceptance record rather than being replaced by synthetic results.
