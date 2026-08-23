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
