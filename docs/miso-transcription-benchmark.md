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
