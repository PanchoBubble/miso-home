# Miso Phase 2 acceptance report

Issue: `miso-afm.3.6`  
Date: 2026-08-23  
Target: Pancho Pi, Raspberry Pi 5, aarch64  
Revision: `dd30cd1`

## Result

The deploy-time software and offline-provider portions of the bilingual voice
milestone pass. The physical across-room gate remains open because Pancho Pi has
no capture PCM: `/proc/asound` contains only the two HDMI cards. Consequently,
no result in this report is presented as a live microphone, acoustic barge-in,
or USB reconnect result.

The deployed service is active with zero restarts. Wake, transcription, both
speech voices, and the conversation coordinator report available/ready states
with zero processing failures or conversation errors. LAN Ollama and hosted GPT
are not configured. Direct local-only probes returned the exact requested
English response through `pi-ollama` in 2.505 seconds and the exact requested
Spanish response in 0.439 seconds.

## Acceptance matrix

| Capability | Evidence | Result |
| --- | --- | --- |
| Offline provider isolation | Live authenticated status reported `pi-ollama` ready, hosted GPT `not_configured`, no LAN provider, and no configured LAN/OpenAI credential; both language probes selected only `pi-ollama` | Pass |
| English local response | Deployed ARM64 router returned `Voice offline English ready.` exactly in 2.505 seconds | Pass |
| Spanish local response | Deployed ARM64 router returned `Voz local en español lista.` exactly in 0.439 seconds | Pass |
| Explicit transitions | Unit coverage rejects invalid transitions and exercises wake, acknowledgement, listening, transcribing, routing, tool use, speaking, follow-up, check-back, goodbye, error, idle, and stopped states | Pass (deterministic ARM64) |
| Routine tool and memory capture | A routed voice tool call passed schema validation, wrote its tool result and user/assistant messages under one SQLite conversation ID, spoke confirmation, and exposed the `using_tool` transition | Pass (deterministic ARM64) |
| Barge-in | A VAD speech-onset event cancelled blocked speech output, moved immediately to transcription, routed the interrupting utterance, and prevented the cancelled worker from changing the newer turn | Pass (deterministic ARM64); acoustic gate open |
| Follow-up and explicit goodbye | A completed response opened follow-up mode under the same conversation; Spanish `adiós` selected the Spanish goodbye and cleared the conversation | Pass (deterministic ARM64) |
| Timeout closure | The first listening timeout produced one check-back cue; the second produced goodbye and cleared the conversation ID; timeout counters recorded both events | Pass (deterministic ARM64) |
| Error recovery | Provider failure entered the bounded error path, cleared active context, recorded one error, and returned safely to idle | Pass (deterministic ARM64) |
| Wake and transcription runtime | Custom `miso.onnx` and multilingual `ggml-tiny.bin` reported available/listening with zero failures | Pass (runtime readiness); across-room gate open |
| Speech runtime | English and Spanish Piper workers reported available/idle with zero errors | Pass (runtime readiness); audible gate open |
| Service stability | Miso, Docker, and `zurg-watcher` remained active; Miso `NRestarts=0` | Pass |
| Across-room bilingual sessions | No USB capture card or capture PCM is attached | Blocked by hardware |
| USB loss/reconnect and reboot audio recovery | Intended USB microphone and speaker are not attached | Blocked by hardware |

## Quality gates

The final local and ARM64 staging copies each passed:

```text
95 unit tests passed
5 integration tests passed
Python compilation passed
install-miso-runtime.sh syntax passed
```

The deployed `src/miso/conversation.py` SHA-256 matched the pushed local source:
`bd72f640d3ad56313b6718ce5d8efb1b1eb49f02f8526dd34054b6b5e0ec919a`.
Authenticated status exposed state and bounded counters but no transcript or
spoken-response content.

## Remaining physical gate

`miso-9ft` and `miso-r2x` retain the hardware work. Once the intended USB
microphone and speaker are attached, run quiet-room English and Spanish sessions
from the target distance covering wake, routine tools, response, multi-turn
follow-up, explicit goodbye, one-then-two timeout behavior, interruption during
routing and playback, memory capture, unplug/reconnect, and reboot recovery.

The current `vcgencmd get_throttled` value is `0xe0000`: no current-condition
bits are set, but historical frequency-cap, throttling, and soft-temperature
flags have occurred since boot. Reboot to establish a clean baseline for the
physical session, record temperature and `get_throttled` before and after the
sustained bilingual run, and treat any newly asserted flag as a failed thermal
gate requiring investigation.
