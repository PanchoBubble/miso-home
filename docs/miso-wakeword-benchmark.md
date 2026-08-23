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

Train `Miso` as a single target phrase with openWakeWord 0.6.0. Use at least
20,000 augmented positive examples across English and Spanish TTS voices and
the upstream large negative feature set. Include confusable negatives such as
`Milo`, `Mia`, `missile`, `missing`, `mismo`, `misa`, `piso`, `quiso`, `hizo`,
`aviso`, and `permiso`. Keep a held-out set that was not used for augmentation
or threshold selection.

The exported ONNX file is configuration, not application code. Install it with
its recorded checksum:

```bash
sha256sum /path/to/miso.onnx
sudo ops/install-openwakeword.sh /path/to/miso.onnx MODEL_SHA256
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
for threshold in 0.35 0.45 0.55 0.65; do
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

## Current hardware gate

On 2026-08-23 the Pancho Pi exposed only its two HDMI playback cards and no
capture PCM. The runtime and repeatable scorer can therefore be validated in
software, but human distance and quiet-room measurements remain blocked until
the intended USB microphone is attached. This limitation must stay visible in
the beads acceptance record rather than being replaced by synthetic results.
