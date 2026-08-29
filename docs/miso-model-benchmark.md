# Miso Pi model benchmark

Benchmark date: 2026-08-22. Host: Raspberry Pi 5, four Cortex-A76 cores at up
to 2.6 GHz, 16 GiB RAM, Ollama 0.32.15 CPU-only. All models used their default
Ollama quantization, `temperature: 0`, `think: false`, a 96-token output cap,
and a warm model. The repeatable harness is `ops/benchmark-ollama.py`.

| Model | Size | Median first output | Median generation | Timer tool |
| --- | ---: | ---: | ---: | --- |
| `qwen3:0.6b` | 522 MB | 0.172 s | 26.50 tok/s | Correct |
| `qwen3:1.7b` | 1.4 GB | 0.476 s | 9.87 tok/s | Correct |
| `qwen3:4b` | 2.5 GB | 1.151 s | 4.45 tok/s | Incorrect |

The cases were a one-sentence Spanish response, a strict `timer_create` call,
and a two-sentence SQLite comparison. The 0.6B model had 0.153–0.172 s first
text output and completed the tool call in 1.662 s. The 1.7B model had
0.459–0.476 s first text output and completed the tool call in 4.263 s. The 4B
model hit the 96-token cap in every case (22.489–27.701 s total), ignored the
requested concise form, and failed to call the timer tool.

`qwen3:1.7b` is the production Pi tool model because it passed the strict tool
case and has more capacity for choosing among Miso's expanding tool families.
`qwen3:0.6b` remains installed as the faster low-latency recovery option.
`qwen3:4b` is rejected for this CPU-only routing profile and was removed after
benchmarking. Requests without a matching local tool prefer hosted GPT, while
the Pi remains the offline fallback.
