# Miso offline speech-synthesis benchmark

Benchmark date: 2026-08-23. Target: Pancho Pi, Raspberry Pi 5, Debian 12
aarch64. The runtime uses `piper-tts` 1.4.2 and two 22.05 kHz medium voices:
`en_GB-cori-medium` and `es_ES-davefx-medium`. Cori's model card identifies
its LibriVox source as public domain; davefx's model card identifies its source
as CC0. Both models and configs are installed with verified SHA-256 hashes.

## Acceptance targets

The fully offline, pre-warmed path must meet these automated targets:

- at most 1 second p95 from request dispatch to the first PCM frame;
- synthesis faster than real time (median real-time factor below 1); and
- at most 100 ms from cancellation to stopped synthesis/output.

Representative English and Spanish household commands are also round-tripped
through the selected production Whisper model. Mean WER must remain at or below
20% as an automated intelligibility proxy. Physical listening remains necessary
for final pronunciation, prosody, and speaker-volume acceptance.

## Results

`ops/benchmark-piper.py` ran three attempts over each of four representative
commands (12 total). The initial cold CLI design took 2.54 seconds median and
2.67 seconds p95 to first audio because every request loaded its ONNX model.
The production design keeps one isolated worker per voice pre-warmed and sends
text over a private length-prefixed stdin protocol. This both avoids placing
speech text in process arguments and removes model-loading latency from turns.

| Measure | Result | Target |
| --- | ---: | ---: |
| Median first PCM | 425 ms | informational |
| p95 first PCM | 483 ms | <= 1,000 ms |
| Median synthesis RTF | 0.121 | < 1 |
| Worker cancellation after first PCM | 20 ms | <= 100 ms |
| Deployed API cancellation observed | 49–62 ms | <= 100 ms |

The final deployed English API probe began PCM in 253 ms, synthesized 1.382
seconds of audio in 255 ms across 15 bounded chunks, completed the ALSA playback
lifecycle in 1.316 seconds, and returned no error. Three English API requests
were then cancelled
while output was active: the cancellation calls returned in 0.9–4.0 ms and the
terminal `cancelled` results were observable in 49–62 ms. Each terminated voice
worker was automatically pre-warmed again. Miso remained active with zero
restarts.

## Intelligibility proxy

The first WAV from each representative case was resampled to mono 16 kHz and
transcribed with the installed multilingual Whisper `tiny` model. Mean WER was
16.72%, language classification was 100%, median transcription latency was
1.739 seconds, and all four cases stayed intelligible enough for the automated
gate. Per-case WER was 8.33%, 18.18%, 15.38%, and 25%; the last Spanish sample
is the reason physical listening is still tracked.

This round trip is deliberately a proxy, not a substitute for listening. The Pi
currently exposes HDMI playback but the intended USB speaker is not attached.
`miso-9ft` tracks listening to these fixtures on the actual device, volume
calibration, audible cancellation, reboot survival, and USB reconnect behavior.

## Reproduction

Install and enable the pinned runtime:

```bash
sudo ops/install-piper.sh
sudo ops/install-miso-runtime.sh
```

Then run `ops/benchmark-piper.py --help`. The script can retain one WAV per case
with `--wav-directory`; `benchmarks/piper/manifest.json` supplies the matching
text for the Whisper round-trip scorer. The production `/api/status` response
contains voice availability and bounded timing/error diagnostics but never
speech text.
