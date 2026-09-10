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

## Click-paced calibration guide

From this PC, run:

```bash
PYTHONPATH=src python3 -m miso.calibration_guide
```

Open `http://127.0.0.1:8766`. The page shows each instruction before recording;
check the local-recording consent box, press **Record this prompt**, wait for
**Speak now**, and say the phrase once. Every clip is five seconds. **Next
prompt** never records automatically; **Retry this prompt** replaces its saved
clip. The 26-prompt starter set includes English/Spanish positives at 1 m and
3 m in separate training/evaluation rounds, plus two training confusables. This
small set does not establish final recall or household false-activation rates.

The microphone stays on the Pi. The helper uses the existing `pancho-pi` SSH
alias and the deployed `plughw:CARD=Device,DEV=0` mono 16 kHz S16_LE capture
settings. It requires passwordless `sudo` for service control. Each explicit
recording briefly stops `miso.service` to free the USB device, streams PCM over
SSH, and restores the service if it was running. A 45-second transient systemd
watchdog provides a second restoration path if the capture worker dies. No
runtime source or wake thresholds are changed, and no remote WAV is written.

Clips and a trainer-compatible `manifest.json` are stored under a fresh
`.local/wake-corpus/guided-*` directory on this PC. Keep the helper running for
analysis. **End session & delete clips**, normal helper shutdown, or the
24-hour deadline deletes this session's corpus. Closing a browser tab does not
stop the helper. If the PC loses power or the helper is forcibly killed, its
cleanup cannot run: remove any orphaned session directory before reusing the
corpus. Do not copy raw recordings elsewhere without carrying over the same
retention deadline. No training, cloud upload, or deployment happens from this
page.

## Guided microphone result — 2026-09-10

All 26 five-second clips completed through the Pi's deployed USB capture device:
12 training positives, 12 reserved evaluation positives, and two training
confusables. No clip was near-silent or clipped (peak levels ranged from -21.42
to -8.69 dBFS). This checks signal levels, not pronunciation or absence of noise.
Offline streaming replay on the workstation used the deployed model checksum,
Silero VAD 0.5, energy floor -60 dBFS, one activation frame, and two-second
cooldown. These are recognition measurements, not Pi response-time benchmarks.

| Threshold | Evaluation detections | English | Spanish | At 3 m |
| --- | ---: | ---: | ---: | ---: |
| 0.999 (deployed) | 5/12 | 1/6 | 4/6 | 3/6 |
| 0.997 | 10/12 | 5/6 | 5/6 | 4/6 |
| 0.995 | 10/12 | 5/6 | 5/6 | 4/6 |
| 0.98 | 12/12 | 6/6 | 6/6 | 6/6 |

Neither training confusable activated at these thresholds, but ten seconds of
training negatives cannot establish household false-wake performance. The
existing upstream held-out feature benchmark predicts 3.2726 activations/hour
at 0.98, above the 0.5/hour target. The production threshold remains unchanged.

The starter guide omitted evaluation negatives. The trainer's preflight
requires them, and final acceptance requires at least an hour of evaluation
room sound. That needs a separate explicit recording step; the 26 clips do not
complete retraining or acceptance. Fresh evaluation is also appropriate after
using this small set for threshold comparisons. Aggregate results are retained
in `benchmarks/openwakeword/microphone-2026-09-10.json`; raw clips remain subject
to their original deletion deadline, 2026-09-11 20:27:34 UTC.

### Consented room-audio follow-up

For an explicitly authorized background session, leave the original guide
running (it owns retention) and start:

```bash
PYTHONPATH=src python3 -m miso.room_calibration \
  --manifest .local/wake-corpus/guided-SESSION/manifest.json --consent
```

This immediately starts a bounded 65-minute capture, with a countdown and Stop
button at `http://127.0.0.1:8768`. Five minutes are reserved for training and the
following 60 for evaluation. Each minute is saved as a separate WAV. The combined
`room-manifest.json` includes the original voice clips and disjoint room groups;
it does not overwrite the guide's manifest. Use the combined manifest for
training and benchmarking after capture. Do not intentionally utter the wake
phrase during this negative-audio session; review accidental target phrases
before interpreting false-wake counts.

Capture runs in a transient Pi systemd unit with `RuntimeMaxSec=4000` and
`ExecStopPost` restoring Miso. Stop targets that unit. The local SSH stream has
its own timeout, and partial clips remain available if the session ends early.
An incomplete session does not pass the one-hour negative-audio acceptance gate.
The helper refuses to start when Miso is already inactive. The original corpus
deletion deadline still applies; keep the original guide running for cleanup.

To show either loopback-only page on the Pi, forward the same port over SSH,
then open it in the Pi desktop browser. For example:

```bash
ssh -fNT -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:8768:127.0.0.1:8768 pancho-pi
```

Use `http://127.0.0.1:8768` on the Pi. The helper stays on the PC; audio capture
still uses the Pi's USB microphone.
