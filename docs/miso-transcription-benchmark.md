# Miso offline transcription benchmark

Benchmark date: 2026-08-23. Target: Pancho Pi, Raspberry Pi 5, four
Cortex-A76 cores, 16 GiB RAM, Debian 12 aarch64. The benchmark used
whisper.cpp `v1.9.1` built for the host with `GGML_NATIVE=ON`, four CPU threads,
GPU disabled, automatic language detection, and cold per-utterance CLI model
loads. The repeatable scorer is `ops/benchmark-whisper.py`.

## Acceptance targets

The post-VAD command path must remain offline and satisfy all of these targets:

- at most 20% mean normalized word error rate (WER) on monolingual commands;
- at most 50% mean WER on mixed English-Spanish commands;
- 100% English, Spanish, or mixed classification on the six-case set; and
- at most 2.5 seconds p95 cold transcription latency.

Punctuation, case, and accent marks are normalized for WER. Mixed classification
requires at least two recognized command words from each language. The model's
dominant acoustic language and probability remain separately available so a
caller can distinguish a confident monolingual result from code switching.

## Fixtures

The six mono 16 kHz S16_LE fixtures are deterministic macOS system-voice
samples. They cover UK and US English, Spain and Mexico Spanish, and both
code-switch directions. Their durations are 1.368–4.168 seconds.

| Voice | Expected command |
| --- | --- |
| Daniel (`en_GB`) | Turn on the kitchen light |
| Samantha (`en_US`) | Add milk and coffee to the shopping list |
| Mónica (`es_ES`) | Enciende la luz de la cocina |
| Paulina (`es_MX`) | Añade leche y café a la lista de compras |
| Samantha + Mónica | Miso, set a timer for five minutes, y apaga la luz del salón |
| Mónica + Daniel | Miso, pon un temporizador de diez minutos, and add bread to the shopping list |

These samples make model comparisons repeatable, but they do not claim to model
room acoustics or human microphone speech. Live hardware and human-speech
acceptance remains a separate gate once the intended USB microphone is attached.

## Candidate matrix

The first pass used one run per case without command prompting. Values include
model loading and JSON serialization. Q5 is smaller but slower on this Pi build,
which reports ARM dot-product support but no i8mm support.

| Model | Disk | Mean WER | Language match | Median latency | p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| `tiny` F16 | 74.1 MiB | 21.28% | 83.33% | 1.651 s | 1.793 s |
| `tiny` Q5_1 | 30.7 MiB | 20.19% | 83.33% | 2.082 s | 2.301 s |
| `base` F16 | 141.1 MiB | 20.92% | 100% dominant language | 3.700 s | 3.928 s |
| `base` Q5_1 | 56.9 MiB | 20.92% | 100% dominant language | 5.014 s | 9.471 s |
| `small` Q5_1 | 181.3 MiB | 13.37% | 100% dominant language | 19.954 s | 21.374 s |

The sequential all-model run reached 84.5 C and activated the Pi's soft thermal
limit while evaluating the slower candidates. The host cooled to about 61 C
after the run, existing services stayed active, and no selected-model run is
expected to resemble that sustained workload. The small and base candidates are
rejected for interactive latency; quantized variants are rejected because they
save disk but do not improve speed on this CPU.

## Selected result

The multilingual F16 `tiny` model was rerun three times per case from a cooled
host with the production bilingual command prompt. All 18 offline attempts
passed:

| Measure | Result | Target |
| --- | ---: | ---: |
| Monolingual mean WER | 8.33% | <= 20% |
| Mixed-language mean WER | 11.54% | <= 50% |
| English/Spanish/mixed classification | 100% | 100% |
| Median cold latency | 1.701 s | informational |
| p95 cold latency | 2.107 s | <= 2.5 s |
| Median real-time factor | 0.750 | < 1 preferred |

The hardest monolingual fixture (Mexico Spanish shopping command) produced
33.33% WER despite the passing mean. Its returned token confidence was 0.763
and acoustic-language probability was 0.796, allowing downstream conversation
logic to ask for confirmation. Both code-switch directions were classified as
mixed; one had 23.08% WER and the other 0%.

`ops/install-whisper-cpp.sh` pins the tested release and verifies the selected
model's SHA-256 before enabling STT. The runtime assembles bounded utterances
after a replaceable VAD decision, preserves pre-roll, invokes only local files,
and returns text, language classification, acoustic language probability,
segment/token timestamps, token-derived confidence, inference latency, and
real-time factor.

The installed static production binary repeated the six cases as the `miso`
service user with the same 8.33%/11.54% WER and 100% classification results;
median latency was 1.666 s and p95 was 1.962 s. After deployment, Miso reported
the model available and the transcription worker listening with zero failures.

## Spanish timer command re-evaluation

Re-run date: 2026-08-30. Reported Spanish timer commands were reaching the
model lane instead of the deterministic fast lane, "cinco segundos" being heard
as "cicos segundos" among them. Word error rate alone hides that failure: a
one-word slip on a number word costs little WER and still sends the whole turn
to a model, so this pass scores **fast-lane intent accuracy** as well, meaning
the share of transcripts that still match the intended intent *with the
intended arguments*. `ops/benchmark-whisper.py` computes it from a manifest
`intent`/`intent_arguments` pair by stripping the wake phrase and calling the
production matchers with no tool invocation.

### Fixtures

`ops/make-stt-fixtures.py` synthesizes 20 mono 16 kHz S16_LE fixtures from
macOS system voices: 17 Spanish timer commands (Mónica `es_ES`, Paulina
`es_MX`) and 3 English timer controls (Samantha `en_US`, Daniel `en_GB`). The
English controls exist so a Spanish-leaning prompt cannot buy Spanish accuracy
with English accuracy. The audio is generated rather than committed. No fixture
sentence appears in any evaluated prompt: prompts carry domain vocabulary and
differently worded examples only.

This run used Homebrew whisper.cpp `1.9.2` on an Apple Silicon Mac with
`--no-gpu`, four threads, automatic language detection, and three repeats per
case, except the one-repeat `small` and cinco-primed diagnostics. Every case
returned byte-identical text across its repeats. **The
latencies below are not Pi latencies** and are only comparable to each other;
the Pi figures earlier in this document remain the latency record. Raw results
are in `benchmarks/whisper/spanish-timer-prompts.json`.

### Result

| Variant | Mean WER | Fast-lane intent | Spanish WER | Spanish intent | English WER | English intent | Median latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tiny`, previous prompt (before) | 16.79% | 80.0% | 14.15% | 82.35% | 31.75% | 66.7% | 0.788 s |
| `tiny`, command-example prompt (after) | 7.98% | 85.0% | 9.39% | 82.35% | 0% | 100% | 0.800 s |
| `tiny`, number-list prompt | 8.69% | 80.0% | 8.54% | 76.47% | 9.53% | 100% | 0.822 s |
| `base`, number-list prompt | 4.88% | 90.0% | 5.74% | 88.24% | 0% | 100% | 1.460 s |
| `small`, number-list prompt | 0% | 100% | 0% | 100% | 0% | 100% | 4.100 s |

The shipped change is the prompt only. Replacing the old keyword list with
whole example commands halves overall WER (16.79% to 7.98%) and repairs the
English control, which the previous prompt turned into "Set of time of 40
seconds" and a wrong-valued timer.

### Negative result: Spanish fast-lane accuracy did not improve

Spanish fast-lane intent accuracy is 82.35% before and after: the same 14 of 17
commands. Spanish WER falls from 14.15% to 9.39%, but the three failures are
unchanged, and no prompt tested moved them:

- "pon un temporizador de cinco segundos" to "cicos segundos";
- "pon un temporizador de cinco minutos" to "cico minutos"; and
- "cuánto queda en el temporizador" to "cuento queda en el temporizador".

Priming the prompt with "cinco segundos, cinco minutos" directly left all three
unchanged, so this is not a vocabulary problem. `base` clears one of them and
`small` clears all three at 0% WER, which places the remaining errors at the
capacity of `tiny` rather than at its conditioning. Neither is deployable:
`base` measured 3.700 s median on the Pi and `small` 19.954 s, against a 2.5 s
p95 target, and this re-run reproduces that ordering with `base` 1.8x and
`small` 5.2x the `tiny` latency.

A number-word list in the prompt is actively harmful and was rejected. It
lowered Spanish intent accuracy to 76.47% by turning a correctly transcribed
"12 minutos" into "dos minutos", which sets a two-minute timer instead of
twelve without any visible failure. Priming the model to spell numbers out
gives it a way to be confidently wrong.

The remaining Spanish gap therefore has to be closed after transcription, not
inside it: the fast-lane matcher needs tolerance for near-miss number words.
That is tracked separately.

## Transcription lanes

Change date: 2026-09-05. The numbers above were all measured against
`whisper-cli`, which is forked once per utterance: write a temporary WAV, spawn
a process, load `ggml-tiny.bin` from disk, infer, parse JSON, exit. On a live
pipeline that fires on every short segment, the model load dominates and the
compute hides behind it, so a 1.7 s median for roughly two seconds of audio is
mostly startup, not recognition.

Transcription is now a chain of lanes, tried in order. A lane that fails falls
through to the next and then sits out `MISO_STT_LANE_COOLDOWN_SECONDS`
(30 s by default) rather than being retried on the next sentence: once the
uplink is gone, paying the hosted timeout on every utterance is slower than
having no hosted lane at all.

| Lane | Where | Notes |
| --- | --- | --- |
| `wispr-flow` | hosted | Base64 16 kHz WAV to `POST /api`, `Authorization: Bearer`. Dictation-tuned rather than a raw recogniser: it drops filler words and repairs names. Audio leaves the house. Unset the key to disable. |
| `whisper-server` | local | `miso-whisper.service`, model resident, `POST /inference` multipart on loopback. Same model and prompt as the CLI. |
| `whisper-cli` | local | Unchanged, last resort. Reloads the model per utterance. |

The wake phrase warms the hosted lane's connection (`GET /warmup_dash`, at most
once every 45 s) so the TLS handshake is paid during the second between the
wake and the end of the sentence rather than inside the request.

`MISO_STT_VAD_END_SILENCE_MILLISECONDS` dropped from 600 to 400. That silence
is paid on every sentence before the recogniser starts at all.

### Not measured yet

None of the lane latencies have been measured on the Pi. `ops/benchmark-whisper.py`
still scores the CLI only, and the whisper-server lane also runs with
`--suppress-nst`, which the benchmarked CLI invocation did not. Both the
accuracy and the latency claims in the table above are design intent, not
results. Re-run the six-case set against each lane before treating them as
equivalent to the numbers earlier in this document.

Alternatives considered and rejected for this pass:

- **Wispr Flow desktop app / Handy**: neither is deployable here. Wispr Flow's
  app is closed-source macOS and Windows; its *API* is what Miso uses. Handy is
  open source but is an x64 desktop GUI with no Linux ARM build and no headless
  mode.
- **Parakeet TDT 0.6B v3 via sherpa-onnx**: multilingual including Spanish and
  CPU-fast, and it is what Handy switched to. Worth benchmarking against
  `ggml-tiny` on the Pi, but it is a 600 M parameter model on four A76 cores
  and nothing here has measured it.

## Gating the recogniser

The worker used to transcribe every utterance the VAD produced, forever,
regardless of what the conversation was doing. That had three costs: a core
spent on the room's own conversation, a queue in front of the utterance that
was actually addressed to Miso, and transcripts of background speech handed to
the state machine as requests. `MISO_STT_GATED=true` runs the recogniser only
while the conversation is in a state that expects speech.

The trade is the wake-phrase-by-transcription fallback, which needed the
recogniser running while Miso was idle. openWakeWord and the talk button both
still work; set `MISO_STT_GATED=false` to get the fallback back.
