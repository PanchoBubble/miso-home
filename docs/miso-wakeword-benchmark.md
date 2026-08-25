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
with openWakeWord 0.6.0. Its baseline uses 20,000 augmented positive examples
across English and Spanish TTS voices, 10,000 confusable negatives, and 20,000
windows from the upstream large negative feature set. It can additionally mix
consented training-split microphone positives and household hard negatives.
Confusables include `Milo`,
`Mia`, `missile`, `missing`, `mismo`, `misa`, `piso`, `quiso`, `hizo`, `aviso`,
and `permiso`. Voice identities and the final 20% of the general-negative
timeline are held out from training.

The generator currently requires macOS `say`, FFmpeg at
`/opt/homebrew/bin/ffmpeg`, Python 3.11, `openwakeword==0.6.0`, `onnx==1.18.0`,
and scikit-learn. Download openWakeWord's
[`validation_set_features.npy`](https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy),
then run:

```bash
PYTHONPATH=src python ops/train-wakeword.py \
  --output-directory .local/wake-training/run \
  --negative-features .local/wake-training/validation_set_features.npy \
  --microphone-manifest .local/wake-corpus/manifest.json
```

The fixed seed, disjoint voice sets, aggregate microphone corpus counts,
retention deadline, selected threshold, complete threshold matrix, and
ONNX/sklearn parity check are written to `metrics.json`. Raw paths, group IDs,
and audio are not copied into the training output. A run exits nonzero unless
its synthetic/upstream-negative model-selection holdout finds at least 80%
aggregate and per-language recall with no more than 0.5 false activations per
hour. That exit status is not physical-microphone acceptance; the evaluation
split below remains authoritative.

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

Record Miso naturally before English and Spanish commands at 1 m and 3 m. Use
separate recording sessions for training and evaluation, represented by
pseudonymous `group_id` values; a group may not cross the split boundary. Add
training hard negatives containing confusable words and reserve at least one
hour of evaluation room audio containing ordinary conversation, television,
music, and silence. WAV files must be uncompressed mono 16 kHz 16-bit PCM and
must stay beneath the manifest directory. Keep the corpus under `.local/`, not
in git. The manifest must record the explicit consent time and deletion
deadline:

```json
{
  "consent": {
    "confirmed": true,
    "confirmed_at": "2026-08-25T20:00:00Z",
    "delete_raw_by": "2026-08-26T20:00:00Z"
  },
  "cases": [
    {
      "path": "training/en-session-a-1m.wav",
      "label": "positive",
      "language": "en",
      "distance_meters": 1,
      "split": "training",
      "group_id": "positive-session-a"
    },
    {
      "path": "evaluation/es-session-b-3m.wav",
      "label": "positive",
      "language": "es",
      "distance_meters": 3,
      "split": "evaluation",
      "group_id": "positive-session-b"
    },
    {
      "path": "evaluation/quiet-room-01.wav",
      "label": "negative",
      "language": "mixed",
      "split": "evaluation",
      "group_id": "negative-session-b"
    }
  ]
}
```

The loader refuses missing consent, expired retention, absolute or escaping
paths, duplicate files, unsupported labels/languages, and any `group_id` used
in both splits. The trainer reads only `training` audio. The scorer defaults to
`evaluation` and never selects a threshold from training recordings.

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

After the model, aggregate metrics, and checksums are retained, delete the raw
audio before its approved deadline. The deletion helper resolves and validates
only manifest-listed files and emits a path-free audit receipt:

```bash
PYTHONPATH=src python ops/delete-wakeword-corpus.py \
  --manifest .local/wake-corpus/manifest.json \
  --split all \
  --audit-output .local/wake-training/raw-audio-deletion.json \
  --confirm-delete
```

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

## Current physical result

The intended USB microphone is now attached. A consented 2026-08-25 calibration
at approximately 1 m measured only 30% recall at the deployed `0.999` threshold.
Lowering the threshold to `0.95` recovered 80% English and Spanish recall but
raised the existing negative-set prediction to 6.5452 false activations/hour.
Those ten temporary clips and their path-bearing artifacts were deleted after
aggregate metrics were recorded. Production therefore remains at `0.999` until
a retrained model passes the disjoint physical evaluation above.
