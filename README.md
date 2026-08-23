# Miso

Miso is a local-first household assistant for the existing Pancho Raspberry Pi.
The first milestone is a provider-neutral text runtime with local SQLite memory,
validated tools, deterministic model routing, and a LAN dashboard. Voice and
hardware integration are later phases.

## Local development

Python 3.11 or newer is the only runtime dependency for the initial scaffold.
Audio streaming additionally uses the `arecord` and `aplay` commands from
`alsa-utils` on Linux; no Python audio package is required.

```bash
make test
make integration-test
make run
```

`make run` creates private development directories under `.local/` and listens
on `127.0.0.1:8090`. Check it with:

```bash
curl --fail http://127.0.0.1:8090/healthz
```

## Pi deployment

The Pi storage layout must first be installed with
`ops/configure-miso-storage.sh`. Copy the repository to the Pi, then run:

```bash
sudo ops/install-miso-runtime.sh
curl --fail http://127.0.0.1:8090/healthz
```

The installer places root-owned application code in `/opt/miso/app`, installs
`miso.service`, and runs the service as the unprivileged `miso` system user.
Optional environment overrides belong in `/etc/miso/miso.env`, never in Git.

The first Pi provider is Ollama on `127.0.0.1:11434`. Its systemd drop-in keeps
downloaded models under `/var/lib/miso/models/ollama`; the initial deployment
uses `qwen3:0.6b` as a small ARM64 smoke-test model. Larger-model benchmarking
and routing are tracked separately because the 0.6B model is useful for proving
streaming and tool-call mechanics, not for final assistant quality.

## Tool security boundary

All assistant tools are registered explicitly with a strict object-shaped JSON
Schema (`additionalProperties: false`). Requests are validated before handlers
run and return structured success, rejection, error, timeout, or cancellation
results. The service constructs its runtime registry with a private JSONL audit
log under `state_dir / "audit" / "tools.jsonl"`; arguments named in a tool's
`redact_fields` are removed from audit records.

MCP tools must be adapted through `MCPToolAdapter` with an explicit server
allowlist. The developer command tool is disabled by default, accepts argument
arrays rather than shell text, restricts execution to an approved directory and
executable allowlist, and can only be enabled for a bounded interval through its
dashboard-facing controller. Its status includes the visible scope and expiry.

## Household tools

The runtime registers durable timer, reminder, and shared shopping-list tools
against the same transactional SQLite database as memory. A background worker
normalizes scheduled timestamps to UTC, atomically fires overdue items (also on
process restart), and emits durable audit events. Every mutation increments a
revision. Shopping removals are retained as tombstones so operator views can
inspect history. Tool results use stable object shapes suitable for both local
models and the dashboard.

## Optional model providers

The Pi Ollama adapter remains the default local provider. A separate LAN Ollama
tier can be configured with `MISO_LAN_OLLAMA_URL` and
`MISO_LAN_OLLAMA_MODEL`. Hosted GPT uses the OpenAI Responses streaming API and
is disabled unless `MISO_OPENAI_API_KEY` is present; its model can be selected
with `MISO_OPENAI_MODEL`. Put these values in root-owned `/etc/miso/miso.env`
(recommended mode `0600`), never in source control. Hosted requests set
`store: false`, translate strict tool schemas to function definitions, and do
not include credentials in request bodies or settings representations.

Routing classifies requests as routine, standard, or complex with deterministic
bilingual markers. Routine and standard work prefer the Pi; complex work tries
configured LAN Ollama, then hosted GPT, then the Pi. Health failures and
pre-output timeouts fall through safely, while failures after visible output do
not mix providers. Every decision, attempt latency, fallback reason, and final
selection is written to `state_dir / "audit" / "routing.jsonl"`. Provider
overrides are strict by default, and progress chunks are emitted before health
checks or model loading. Household schemas are narrowed to the relevant tool
family and omitted entirely from unrelated prompts.

## Local dashboard

The service root (`/`) serves the dependency-free operator console. It streams
text and tool results, exposes bounded provider health and routing progress,
searches SQLite memory, and reads redacted tool/routing activity. Chat history
is retained under a conversation ID without exposing database paths, provider
URLs, API keys, or environment values to the browser.

Loopback access works without a token for local development and SSH forwarding.
Any non-loopback `MISO_HOST` requires `MISO_DASHBOARD_TOKEN`; the browser keeps
that token in session storage only. Developer mode is visibly disabled by
default, expires after at most 15 minutes, and runs only allowlisted argv (never
shell text) beneath `MISO_DEVELOPER_ROOT`. The default Pi scope is read-only
`/opt/miso/app`; override it deliberately in `/etc/miso/miso.env` if needed.

## Linux audio

Miso discovers PCM endpoints from `/proc/asound` and opens them through ALSA's
`arecord` and `aplay`. It addresses hardware with stable card IDs such as
`plughw:CARD=Device,DEV=0`, so Linux card-number changes after reboot or USB
reconnection do not change the configured identity. The installer adds the
service account to the `audio` group and grants the systemd unit access only to
sound devices.

Capture and playback use bounded raw S16_LE queues. `/api/status` reports the
resolved device, connection/reconnection state, buffer overruns, playback
underruns, peak and RMS levels, clipping, and consecutive silent chunks. A lost
device is rediscovered and reopened without restarting Miso.

The defaults are mono 16 kHz with 20 ms chunks and one second of buffering.
Set these optional values in `/etc/miso/miso.env`:

```text
MISO_AUDIO_ENABLED=true
MISO_AUDIO_CAPTURE_CARD=Device
MISO_AUDIO_PLAYBACK_CARD=Device
MISO_AUDIO_DEVICE_INDEX=0
MISO_AUDIO_SAMPLE_RATE=16000
MISO_AUDIO_CHANNELS=1
MISO_AUDIO_CHUNK_MILLISECONDS=20
MISO_AUDIO_BUFFER_MILLISECONDS=1000
MISO_AUDIO_RECONNECT_SECONDS=1
MISO_AUDIO_SILENCE_DBFS=-50
MISO_AUDIO_CLIPPING_RATIO=0.98
```

Leave either card ID empty to select the first compatible PCM. Find stable IDs
in brackets in `/proc/asound/cards`; do not configure volatile numeric names
such as `hw:2`. Audio can remain enabled while hardware is absent: status will
show `unavailable`, and the worker will attach when the device appears.

## Offline Miso wake word

Miso runs a configurable custom openWakeWord ONNX model in an isolated pinned
Python environment. Captured audio is fanned out to independent bounded taps,
so wake detection and transcription receive the same PCM without competing for
chunks. A wake activation must pass the bundled local Silero VAD, an RMS energy
gate, the model score threshold, and the configured consecutive-frame count.
Repeated activations are suppressed by a cooldown. `/api/status` reports model
availability, thresholds, bounded wake events, activation count, failures, and
the highest observed score without exposing model paths or captured audio.

The repository includes the reproducibly trained `Miso` model and a pinned
checksum. Install it, then deploy the runtime:

```bash
sudo ops/install-openwakeword.sh
sudo ops/install-miso-runtime.sh
```

To install a different compatible model, pass its path and SHA-256 explicitly:
`sudo ops/install-openwakeword.sh /path/to/miso.onnx MODEL_SHA256`.

Wake inference is entirely offline; network access is used only by the
installer to create the pinned environment and fetch openWakeWord's shared
feature/VAD assets. The optional settings are:

```text
MISO_WAKE_ENABLED=true
MISO_WAKE_PHRASE=Miso
MISO_WAKE_EXECUTABLE=/opt/miso/openwakeword/bin/python
MISO_WAKE_MODEL=/var/lib/miso/models/openwakeword/miso.onnx
MISO_WAKE_THRESHOLD=0.999
MISO_WAKE_VAD_THRESHOLD=0.5
MISO_WAKE_ENERGY_THRESHOLD_DBFS=-60
MISO_WAKE_ACTIVATION_FRAMES=1
MISO_WAKE_COOLDOWN_SECONDS=2
```

See `docs/miso-wakeword-benchmark.md` for bilingual training guidance, the
labeled-WAV manifest, repeatable false-activation/miss scoring, tuning targets,
and the outstanding physical-microphone acceptance gate.

## Offline English-Spanish transcription

Install the pinned whisper.cpp build and selected multilingual model on the Pi,
then deploy the runtime:

```bash
sudo ops/install-whisper-cpp.sh
sudo ops/install-miso-runtime.sh
```

The installer verifies the model checksum, installs a static `whisper-cli`, and
enables STT through `/etc/miso/miso-stt.env`. Captured S16_LE chunks pass through
a replaceable VAD gate and a bounded utterance assembler with pre-roll, minimum
speech, end-silence, and maximum-duration limits. Completed utterances are
transcribed entirely offline. Results include English, Spanish, or mixed
classification, the model's dominant acoustic language probability, segment
and token timestamps, token-derived confidence, inference latency, and
real-time factor. `/api/status` exposes only bounded diagnostics, not transcript
text.

The production default is the multilingual F16 `tiny` model. On the Raspberry
Pi 5 it beat the quantized and larger candidates on interactive latency and
passed the documented bilingual command targets. See
`docs/miso-transcription-benchmark.md` for the full benchmark and limitations.

The optional STT values are:

```text
MISO_STT_ENABLED=true
MISO_STT_EXECUTABLE=/usr/local/bin/whisper-cli
MISO_STT_MODEL=/var/lib/miso/models/whisper/ggml-tiny.bin
MISO_STT_THREADS=4
MISO_STT_TIMEOUT_SECONDS=45
MISO_STT_VAD_THRESHOLD_DBFS=-38
MISO_STT_VAD_MINIMUM_SPEECH_MILLISECONDS=250
MISO_STT_VAD_END_SILENCE_MILLISECONDS=600
MISO_STT_VAD_MAXIMUM_UTTERANCE_MILLISECONDS=15000
MISO_STT_VAD_PRE_ROLL_MILLISECONDS=200
```

`MISO_STT_PROMPT` can override the short bilingual command vocabulary bias.
Energy gating is deliberately replaceable so the wake/VAD phase can supply a
stronger speech decision without changing utterance or transcription contracts.

## Offline English-Spanish speech synthesis

Install the pinned Piper runtime and selected voices, then deploy Miso:

```bash
sudo ops/install-piper.sh
sudo ops/install-miso-runtime.sh
```

Miso pre-warms isolated English and Spanish voice workers, sends text over stdin
instead of process arguments, streams bounded S16_LE chunks to ALSA, and exposes
immediate cancellation and volume control through the internal speech contract.
Authenticated operator probes can use `POST /api/speech` with `text`, `language`
(`en` or `es`), and optional `volume`, then `POST /api/speech/cancel` with the
returned `request_id`. Status includes voice availability and timing diagnostics
but not spoken text.

The Raspberry Pi benchmark passed the one-second first-audio budget at 483 ms
p95, synthesized at 0.121 median real-time factor, and observed deployed API
cancellation in 49–62 ms. See `docs/miso-speech-benchmark.md` for the full results,
round-trip intelligibility proxy, and physical-speaker limitation.

The optional TTS values are:

```text
MISO_TTS_ENABLED=true
MISO_TTS_EXECUTABLE=/opt/miso/piper/bin/python
MISO_TTS_ENGLISH_VOICE=en_GB-cori-medium
MISO_TTS_ENGLISH_MODEL=/var/lib/miso/models/piper/en_GB-cori-medium.onnx
MISO_TTS_ENGLISH_CONFIG=/var/lib/miso/models/piper/en_GB-cori-medium.onnx.json
MISO_TTS_SPANISH_VOICE=es_ES-davefx-medium
MISO_TTS_SPANISH_MODEL=/var/lib/miso/models/piper/es_ES-davefx-medium.onnx
MISO_TTS_SPANISH_CONFIG=/var/lib/miso/models/piper/es_ES-davefx-medium.onnx.json
MISO_AUDIO_PLAYBACK_SAMPLE_RATE=22050
MISO_TTS_VOLUME=1.0
MISO_TTS_CHUNK_BYTES=4096
MISO_TTS_TIMEOUT_SECONDS=60
```
