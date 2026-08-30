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
`miso.service`, publishes `miso.local` with Avahi, configures the `pancho`
desktop to launch the companion face after its health check passes, and runs the
service as the unprivileged `miso` system user. If the legacy Stremio kiosk
entry exists, the installer archives it with a `.miso-disabled` suffix for
rollback.
Optional environment overrides belong in `/etc/miso/miso.env`, never in Git.

After the recovery key exists, install the separate local backup automation:

```bash
sudo ops/install-miso-backup.sh
sudo systemctl start miso-database-backup.service
sudo systemctl start miso-database-restore-check.service
```

It creates a verified SQLite online snapshot plus durable state and encrypted
configuration on the Samsung T7 each day. Publication uses an atomic rename;
weekly checks perform a full isolated restore. Retention is capped at 30 points
and 20 GiB so Miso cannot silently consume the media allocation. See
[`docs/miso-storage-layout.md`](docs/miso-storage-layout.md) for the format,
space thresholds, recovery procedure, and override settings.

The first Pi provider is Ollama on `127.0.0.1:11434`. Its systemd drop-in keeps
downloaded models under `/var/lib/miso/models/ollama`; the production default is
`qwen3:1.7b`, selected for greater tool-choice capacity after passing the strict
ARM64 tool-call benchmark. The faster `qwen3:0.6b` remains installed as a
low-latency recovery option.

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

The Codex CLI can act as a fourth provider once the owner has installed it and
run `codex login` on the machine. Set `MISO_CODEX_CLI_ENABLED=true` to add it;
`MISO_CODEX_CLI_BINARY` (default `codex`) and `MISO_CODEX_CLI_MODEL` (default:
whatever the CLI is configured to use) tune the invocation. Miso shells out to
`codex exec --json` and reads its event stream; it never reads or reuses the
credentials the CLI stores. Every run is pinned to `--sandbox read-only` inside
a throwaway empty directory with an answer-only prompt, so a spoken question
cannot reach the host filesystem. When the binary is absent or nobody is logged
in, health reports `binary_not_found` or `not_authenticated` and the router
skips the tier instead of stalling the lane.

Message intake is three-tiered. A deterministic fast lane (`intake.py`) matches
common bilingual household intents (timers, shopping list, weather) with strict
parsers and invokes the tool directly, answering in milliseconds without any
model; an ambiguous parse always falls through rather than guessing arguments.
It can be disabled with `MISO_FAST_LANE_ENABLED=false`.

When the fast lane misses but the request still looks tool-shaped, the picker
lane (`toolpick.py`) puts it to the Pi model as a selection problem rather than
a conversation: `format=json`, a catalogue of roughly 200 prompt tokens, and a
`MISO_TOOL_PICKER_MAX_TOKENS` cap (default 40) so the model answers with a name
and arguments instead of prose. The model only ever selects. The picked name
must be one of the allowlisted pickable tools (those with a fast-lane renderer),
the arguments are validated against that tool's schema before anything runs, and
the spoken reply comes from the same templated renderer the fast lane would have
used. Malformed JSON, an unknown or unpickable name, and arguments that miss the
schema all fall through to the model lane without executing anything. Every
attempt is audited as a `tool_pick` event (`picked`, `rejected`, or
`fell_through`) with its end-to-end `duration_ms`. Disable it with
`MISO_TOOL_PICKER_ENABLED=false`; bound it with
`MISO_TOOL_PICKER_TIMEOUT_SECONDS` (default 6).

Everything else goes to the model lane, which always prefers hosted GPT, then
the Codex CLI, then LAN Ollama, keeping the Pi model only as the offline
fallback of last resort. Positive provider health
verdicts are cached (`MISO_ROUTING_HEALTH_CACHE_SECONDS`, default 20, `0`
disables) so repeat turns start streaming immediately; a mid-stream failure
evicts the cached verdict and an unavailable verdict is never cached. Health
failures and pre-output timeouts fall through safely, while failures after
visible output do not mix providers. Every decision, fast-lane match, attempt
latency, fallback reason, and final selection is written to
`state_dir / "audit" / "routing.jsonl"` and the tool audit log. Provider
overrides are strict by default, and progress chunks are emitted before health
checks or model loading. Voice replies now speak sentence by sentence: synthesis
of the first sentence overlaps generation of the rest, so long answers start
sounding almost immediately.

Each completed Pi attempt records a `generation` block in
`routing.jsonl` with prompt tokens, prompt-evaluation milliseconds, generated
tokens, generation milliseconds, and tokens per second. Prompt and generation
cost are fixed differently: a slow prompt means too much replayed conversation
history, a slow generation means the model is too large. Without the split,
tuning either one is guesswork.

`MISO_ROUTING_ATTEMPT_TIMEOUT` (default 45s) bounds *silence between chunks*,
not the length of an answer: a small local model may stream for minutes and must
not be cut off while it is still producing tokens. `MISO_ROUTING_STREAM_TIMEOUT`
(default 300s) is the hard ceiling on one attempt and must not be lower than the
attempt timeout. When a provider dies after emitting usable text, the voice turn
speaks what it already had rather than falling silent, and records the reason on
the stored assistant message.

Google Calendar is an optional validated tool family with per-user local OAuth
tokens, timezone-aware events, recurrence, and a deliberately explicit mapping
for unidentified voice requests. See
[`docs/miso-google-calendar.md`](docs/miso-google-calendar.md) for Google Cloud,
authorization, security, and Pi deployment steps.

Current conditions and short forecasts use the validated Open-Meteo tool. It
uses fixed HTTPS endpoints, redacts requested locations from tool audit records,
caches responses for ten minutes, and returns attributed English or Spanish
summaries suitable for text and voice. See
[`docs/miso-weather.md`](docs/miso-weather.md) for configuration and privacy
details.

## Local dashboard

The service root (`/`) opens a full-viewport household organizer backed by live
SQLite state. Members can manage shared or private shopping lists, reminders,
timers, and noticeboard messages, see which list entries came from voice, and
switch to chat without leaving the PWA. Revision-checked mutations reject stale
browser edits instead of overwriting newer household or voice changes. Installed
copies request fullscreen display and fall back to standalone mode where the
platform does not support it.

Authenticated clients keep one resumable live-event stream open for assistant
state, scheduled-item notifications, household changes, and safe tool outcomes.
Events are committed to a bounded SQLite inbox before delivery, so reconnecting
devices replay missed entries from their last event ID without polling. Shared
events reach household members; private events are selected by the same
server-side owner predicate as their source record. Stream and inbox payloads
exclude transcripts, message content, tool output, credentials, and provider
internals.

The dependency-free operator console streams
text and tool results, exposes bounded provider health and routing progress,
searches SQLite memory, and reads redacted tool/routing activity. Chat history
is retained under a conversation ID without exposing database paths, provider
URLs, API keys, or environment values to the browser.

The Memory view browses explicit, inferred, routine, summary, and transcript
records under the same household visibility policy. Members can add tagged
explicit memories, inspect transcript provenance, mark records important,
export their accessible records, preview age- or topic-based pruning, and
select records for permanent deletion. Deleting a source also removes dependent
summaries, tags, full-text rows, and embedding rows in the same transaction.

Loopback access works without a token for local development and SSH forwarding.
Any non-loopback `MISO_HOST` requires `MISO_DASHBOARD_TOKEN`; the browser keeps
that token in session storage only. Developer mode is visibly disabled by
default, expires after at most 15 minutes, and runs only allowlisted argv (never
shell text) beneath `MISO_DEVELOPER_ROOT`. The default Pi scope is read-only
`/opt/miso/app`; override it deliberately in `/etc/miso/miso.env` if needed.

The canonical installed-app origin is `https://miso.jyjonline.com`. Cloudflare
Tunnel and Access publication is configured separately so the origin is never
made public without its email allowlist. On the home LAN, Avahi publishes the
fallback address `http://miso.local` (the Pi unit binds port 80 on the LAN
address only, leaving loopback port 80 to the tunnel ingress). That plain-HTTP fallback is useful for
discovery and recovery, but browsers do not grant it service-worker or push
permissions; install Miso and approve notifications only from the canonical
HTTPS origin. The service worker caches a fixed list of shell assets and always
sends API, authenticated, streaming, and cross-origin requests to the network.

Remote API requests must carry a valid Cloudflare Access application assertion.
Set `MISO_ACCESS_TEAM_DOMAIN` to the exact HTTPS team origin and
`MISO_ACCESS_AUDIENCE` to the Miso application's AUD tag in `/etc/miso/miso.env`.
Miso verifies the assertion's RS256 signature against Cloudflare's rotating key
set, issuer, audience, time bounds, token type, and email. Cloudflare Access is
the sole remote membership authority; Miso dynamically registers each verified
Access email as a web actor for ownership and audit records. The local bearer
token remains available over the LAN as a recovery path.
The dedicated `miso-cloudflared.service` reads its remotely managed tunnel token
from the root-only `/etc/cloudflared/miso.token` through systemd credentials. It
runs separately from any legacy `cloudflared.service`; the runtime installer
enables it only after that token file exists.

## Household identity and sharing

Every request is attributed to an origin-controlled actor. Local dashboard and
bearer-token requests use the normalized `MISO_DASHBOARD_EMAIL` (default
`local@miso.invalid`); remote web identities come only from application JWTs
admitted by the Cloudflare Access policy. Unidentified speech always uses
`household:voice`, while background work uses `miso:system`. Names in prompts or
transcripts never select an identity.

SQLite enforces one common visibility model for conversations, memory,
timers/reminders, and shopping lists: shared records are available to the
household, while private records are available only to their owning web email.
Voice cannot own private data. Dashboard conversations and scheduled items are
private by default; voice equivalents and the current shopping tools are
shared. Tool and routing audits carry the actor identity. See
[`docs/miso-household-identity.md`](docs/miso-household-identity.md) for the
authorization matrix and migration behavior.

## Linux audio

Miso discovers capture PCM endpoints from `/proc/asound` and opens them through
ALSA's `arecord`. It addresses hardware with stable card IDs such as
`plughw:CARD=Device,DEV=0`, so Linux card-number changes after reboot or USB
reconnection do not change the configured identity. The installer adds the
service account to the `audio` group and grants the systemd unit access only to
sound devices.

Bluetooth playback uses a separate, hardened `miso-audio-playback.service` as
the `pancho` desktop user. It exposes only a group-restricted Unix socket to the
main `miso` service and forwards raw PCM to a specifically configured
PipeWire/Pulse sink. The assistant therefore never inherits the desktop user's
session or home access, and a missing Bluetooth sink is reported unavailable
instead of silently falling back to HDMI. The proxy and playback worker both
survive late desktop-session startup and rediscover the sink after reconnect.

A `miso-bluetooth.timer` reconnects the configured speaker every two minutes.
Pulse playback never falls back to another sink, so a speaker that has drifted
off leaves Miso completely silent: the acknowledgement cue fails, the turn
errors, and the conversation never reaches its listening state. BlueZ accepts a
reconnection from a trusted device but does not initiate one. The address is
derived from `MISO_AUDIO_PLAYBACK_CARD` rather than configured twice, and a
powered-off speaker exits cleanly so the timer keeps retrying quietly.

Capture and playback use bounded raw S16_LE queues. `/api/status` reports the
resolved device, connection/reconnection state, buffer overruns, playback
underruns, peak and RMS levels, clipping, and consecutive silent chunks. A lost
device is rediscovered and reopened without restarting Miso.

The playback worker holds the sink open across short underruns rather than
closing it whenever the queue drains. Piper cannot always stay ahead of playback
on the Pi, and reopening a Bluetooth sink per chunk costs hundreds of
milliseconds of link setup, which turns an ordinary underrun into audible
dropouts. Only a gap longer than the grace window ends the utterance and
releases the device.

The defaults are mono 16 kHz with 20 ms chunks and one second of buffering.
Set these optional values in `/etc/miso/miso.env`:

```text
MISO_AUDIO_ENABLED=true
MISO_AUDIO_CAPTURE_CARD=Device
MISO_AUDIO_PLAYBACK_CARD=Device
MISO_AUDIO_PLAYBACK_BACKEND=alsa
MISO_AUDIO_PLAYBACK_PROXY=/run/miso-audio/playback.sock
MISO_AUDIO_DEVICE_INDEX=0
MISO_AUDIO_SAMPLE_RATE=16000
MISO_AUDIO_CHANNELS=1
MISO_AUDIO_CHUNK_MILLISECONDS=20
MISO_AUDIO_BUFFER_MILLISECONDS=1000
MISO_AUDIO_RECONNECT_SECONDS=1
MISO_AUDIO_SILENCE_DBFS=-50
MISO_AUDIO_CLIPPING_RATIO=0.98
```

Leave an ALSA card ID empty to select the first compatible PCM. Find stable IDs
in brackets in `/proc/asound/cards`; do not configure volatile numeric names
such as `hw:2`. For Bluetooth, set `MISO_AUDIO_PLAYBACK_BACKEND=pulse` and set
`MISO_AUDIO_PLAYBACK_CARD` to the exact sink from `pactl list short sinks`; a
sink is mandatory in this mode to prevent fallback. Audio can remain enabled
while hardware is absent: status will show `unavailable`, and the worker will
attach when the device appears.

## Offline Miso wake word

Miso runs a configurable custom openWakeWord ONNX model in an isolated pinned
Python environment. Captured audio is fanned out to independent bounded taps,
so wake detection and transcription receive the same PCM without competing for
chunks. A wake activation must pass the bundled local Silero VAD, an RMS energy
gate, the model score threshold, and the configured consecutive-frame count.
Repeated activations are suppressed by a cooldown. `/api/status` reports model
availability, thresholds, bounded wake events, activation count, failures, and
the highest observed score without exposing model paths or captured audio.

On a supported local DSI display, `miso-display.service` uses the compositor's
idle protocol to power the panel off after five minutes without touch or
keyboard input. A validated Miso wake activation updates a non-sensitive runtime
marker, immediately powers the panel on, and resets the idle countdown while
audio capture remains active. Configure the timeout, output, and marker in
`/etc/miso/miso-display.env`; installers leave this service disabled on headless
systems or hosts without `swayidle` and `wlopm`.

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

## BMO talk and stop buttons

Two momentary buttons on the enclosure give Miso a physical control surface.
The talk button publishes a wake event with `source=button` into the same queue
openWakeWord publishes to, so it reuses the whole turn pipeline, but the
conversation skips the spoken acknowledgement and opens listening directly: a
press is already an unambiguous address, and speaking "Yes?" first would put a
second of synthesis and playback between the press and the open microphone. The
stop button cancels the in-flight turn and clears playback, exactly as a
wake-phrase barge-in does. Both presses are audited with `actor_source=button`.

Wire each button between its BCM pin and a ground pin; the internal pull-up
means no external resistor. Defaults are BCM 23 (talk, header pin 16) and BCM 24
(stop, header pin 18), both next to ground on header pins 14 and 20 and clear of
the I2C, SPI, UART, and display pins. Install `python3-gpiozero` and
`python3-lgpio` on the Pi, then set:

```text
MISO_BUTTONS_ENABLED=true
MISO_BUTTON_TALK_PIN=23
MISO_BUTTON_STOP_PIN=24
MISO_BUTTON_PULL_UP=true
MISO_BUTTON_BOUNCE_MILLISECONDS=50
MISO_BUTTON_HOLD_SECONDS=1.0
```

gpiozero is not a package dependency: it is imported when the buttons start, and
its absence, or absent GPIO hardware, disables the feature with a warning
instead of failing the service. Long press is reserved for hold-to-talk and is
not yet active, but both edges and the hold threshold are already bound so
enabling it needs no rewiring. See `docs/miso-bmo-buttons.md` for the wiring
table, the pin choice, and the long-press plan.

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

## Offline conversational turns

When wake detection, transcription, and speech synthesis are enabled, Miso
coordinates them through an explicit conversational state machine. A wake starts
an acknowledgement and listening window; a completed utterance is routed through
the same provider and allowlisted-tool boundary as dashboard chat, then spoken in
the detected English or Spanish language. The conversation remains open for a
follow-up, gives one bilingual check-back cue, and closes with a goodbye after the
second timeout. Explicit goodbye phrases close it immediately.

VAD speech-onset events are separate from completed transcripts, but a microphone
onset only starts a turn while Miso is listening or waiting for a follow-up.
Everywhere else Miso is either speaking or working on a turn, and on shared-room
hardware the VAD fires on its own speaker and on ordinary noise. Treating those
as a barge-in cancelled real turns: a request that had already been transcribed
and routed was destroyed several seconds into generation by an impatient repeat,
leaving it permanently unanswered. Onsets outside those two states are therefore
ignored, and a transcript that arrives while Miso is speaking or working is
dropped rather than cancelling the turn: cancelling there cleared the playback
buffer mid-phrase and left only the tail of the answer audible.

A transcript that does reach a listening state is compared against what Miso
actually spoke in the last `MISO_CONVERSATION_ECHO_MEMORY_SECONDS`, so its own
acknowledgement heard through the speaker is never routed as a request. Matching
the spoken text cannot mis-credit the wrong utterance the way a bare counter
could, and needs no timestamp on the transcript, which the recogniser does not
provide.

The wake phrase remains the way to interrupt: openWakeWord is far more selective
than the VAD and does not fire on Miso's own voice or on room noise, so saying it
cancels whatever is in flight and starts a new turn.

Miso's own audio is an exception. The Pi shares a room with its speaker and has
no acoustic echo cancellation, so the microphone hears every acknowledgement,
check-back, goodbye, and spoken answer. Without a guard the VAD treats that as a
barge-in: during a cue it transcribes Miso's own words and routes them as the
request, swallowing the real one, and during an answer it cancels playback and
clears the buffer so only the tail of the phrase is ever heard. Mic-driven
interruption is therefore ignored while Miso is speaking, plus a short tail, and
the transcript that onset produces is dropped.

Tune the tail with
`MISO_CONVERSATION_ECHO_GUARD_SECONDS` if the speaker is unusually loud or
distant. Invalid state transitions
are rejected, and bounded provider, tool, or speech errors clear the active
conversation and return safely to idle. `/api/status` exposes the current state,
turn/interruption/timeout/error counts, and the latest transition without
transcript or spoken-response text.

The optional conversation values are:

```text
MISO_CONVERSATION_ENABLED=true
MISO_CONVERSATION_LISTEN_TIMEOUT_SECONDS=8
MISO_CONVERSATION_CHECKBACK_TIMEOUT_SECONDS=5
MISO_CONVERSATION_ACKNOWLEDGEMENT=Yes?
MISO_CONVERSATION_ECHO_GUARD_SECONDS=0.6
MISO_CONVERSATION_ECHO_MEMORY_SECONDS=12
```
