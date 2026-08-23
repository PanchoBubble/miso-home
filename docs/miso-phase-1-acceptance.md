# Miso Phase 1 acceptance report

Issue: `miso-afm.2.9`  
Date: 2026-08-23  
Target: Pancho Pi, Raspberry Pi 5, aarch64  
Revision: `6f21e84`

## Result

The text-first Miso milestone passed its local-first acceptance gate. The live
Pi handled authenticated chat, a validated timer tool, SQLite search, audited
Pi routing, concurrent cancellation, and durable recovery through a full host
reboot. The operator also confirmed that the redesigned dashboard works from a
LAN browser.

LAN Ollama and hosted GPT are not configured on this host. Their ordering,
failure, timeout, streaming, tool translation, and fallback behavior passed the
same test suite on ARM64 using deterministic providers and mocked HTTP
transports. Real external-provider invocation is tracked separately as
`miso-4o8` and is not represented as a live result here.

## Acceptance matrix

| Capability | Evidence | Result |
| --- | --- | --- |
| Dashboard health | `/`, `/healthz`, and authenticated `/api/status` returned HTTP 200; Pi provider reported ready; deployed dashboard hashes matched Git; operator confirmed the redesigned UI and chat work | Pass |
| Text chat | A fresh live conversation returned `Acknowledged.` through `pi-ollama`; the earlier `ping`/wellbeing/weather sequence also returned correct non-hallucinated responses | Pass |
| Validated tools | A live dashboard API request selected `timer_create`; post-reboot `timer_list` found the same recovered timer through `pi-ollama` | Pass |
| SQLite recall | An exact acceptance marker was searchable before reboot and returned one hit after reboot; `PRAGMA integrity_check` returned `ok` before and after cleanup | Pass |
| Pi routing | Live progress and audit events selected `pi-ollama` for chat and timer work | Pass |
| LAN preference | ARM64 `test_complex_request_prefers_lan` and the route-plan test selected `lan-ollama` first for complex work | Pass (deterministic provider) |
| GPT fallback | ARM64 fallback tests selected `hosted-gpt` after unavailable Pi and failed LAN attempts, and after bounded Pi timeout | Pass (deterministic provider) |
| Cancellation | A chat stream was opened, then cancelled concurrently through `/api/chat/cancel`; the stream emitted `cancelled`, and the cancelled routing audit survived reboot | Pass |
| Restart recovery | A full Pi reboot preserved the conversation and timer. The conversation continued with `pong`; the timer became due during reboot and recovered as `completed`, revision 2 | Pass |
| Existing services | T7 mounted by UUID; Docker and VPN recovered; all ten production containers ran; Immich, Nextcloud, Vaultwarden, Homepage, and Homepage External probes passed; all nine DMAGA containers stayed stopped | Pass |
| Sensitive boundary | LAN API remained bearer-authenticated; no token was printed or returned by status; external provider credentials were absent | Pass |

## Quality gates

The following ran against the deployed ARM64 source:

```text
52 unit tests passed
5 integration tests passed
```

The same 57 tests passed locally. `node --check src/miso/web/app.js` passed
locally; Node.js is not installed on the Pi and is not a runtime dependency of
the dependency-free dashboard.

The provider coverage includes strict configuration, Ollama streaming/tool
translation, OpenAI Responses streaming/tool translation, no-dispatch behavior
when unconfigured, credential redaction, deterministic route ordering, bounded
health and attempt timeouts, cancellation, and the rule that a provider failure
after visible output cannot mix responses from a fallback provider.

## Reboot observations

The host booted at `2026-08-23 10:21:26 Europe/London`. Miso, Ollama, Docker,
containerd, NetworkManager, Cloudflare Tunnel, Avahi, `zurg-watcher`, the
encrypted-backup timers, and `nordvpn.service` recovered. `tun0` returned at
`10.100.0.2/20` with the two half-default routes through the VPN. The Pi
reported `get_throttled=0x0` and 56.5 C during the post-boot checks.

All synthetic conversations and the acceptance timer were removed after the
checks. Database integrity remained `ok`; durable routing/tool audit entries
were intentionally retained.
