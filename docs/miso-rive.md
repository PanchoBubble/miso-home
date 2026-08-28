# Miso Rive companion

Miso's companion face lives at `/companion`. It is a deliberately simple, original device face using a mint, deep teal, cream, coral, amber, and sky-blue palette inspired by the color language of friendly retro electronics.

## Runtime and assets

- Rive Web Canvas Lite is pinned to `@rive-app/canvas-lite` 2.41.0 and served locally from `src/miso/web/vendor/rive/`.
- The runtime package tarball integrity is `sha512-vCbNEhNUQTyiM8Ufk3dd2GL5m6csUZkefm8Of9/i+FuM4ftm0ZfmwQlUWAebVVuAKVKFIMpnTDGMBNw8/BVpgg==`.
- `src/miso/web/assets/miso-face.riv` contains one 720×1280 artboard named `Miso` and one state machine named `Miso Face`.
- The `.riv` was generated on 2026-08-28 from original primitive vector shapes with `rive-mcp-server` 0.5.0, then loaded and state-driven with the official Rive runtime. It contains no imported third-party artwork, fonts, images, audio, or hosted assets.
- Rive's MIT runtime license is stored alongside the vendored runtime as `src/miso/web/vendor/rive/LICENSE.txt`.

No CDN or runtime network request is required. The PWA shell caches the companion HTML, JavaScript, CSS, `.riv`, and both primary and compatibility WASM binaries for offline startup.

The canvas drawing surface is capped at 540×960 equivalent pixels and scaled to the 720×1280 DSI viewport. The artwork uses broad, flat vector shapes, so this keeps edges visually clean while bounding raster work during concurrent local inference.

## State contract

The Rive state machine exposes a single numeric input named `state`:

| Value | State | Motion |
| ---: | --- | --- |
| 0 | idle | restrained breathing and blink |
| 1 | waking | eyes open with a short settle |
| 2 | listening | slow attentive eye pulse |
| 3 | thinking | gaze moves from side to side |
| 4 | tool | three processing dots |
| 5 | speaking | bounded mouth movement |
| 6 | muted | lowered eyes and a coral privacy slash |
| 7 | offline | settled, dim expression |
| 8 | error | coral cross eyes and a small shake |

`companion.js` maps the existing privacy-filtered `assistant_state` live events onto these values. Only the semantic state string is inspected. Transcripts, arbitrary text, tool arguments and output, credentials, provider details, and internal transition reasons are never passed to Rive.

## Fallback and motion policy

The route starts with an accessible inline SVG equivalent of the face. Rive replaces it only after the local runtime, WASM, asset, artboard, state machine, and `state` input load successfully. A load failure keeps the SVG visible. The same semantic state mapping drives both renderers.

When `prefers-reduced-motion: reduce` is active, the page does not initialize Rive and uses a static SVG expression. This avoids loading or advancing the animation runtime when motion has been disabled by the user.

## Raspberry Pi 5 benchmark

The deployed renderer was exercised on 2026-08-28 in the Pi's real 720×1280 Wayland session while openWakeWord and offline STT remained active and Ollama generated 256 tokens with `qwen3:0.6b`:

- Broadcom V3D 7.1 / OpenGL ES 3.1 provided the GPU renderer.
- The bounded 540×960 drawing surface sustained 52.8 FPS over 15 seconds.
- Median frame time was 16.7 ms; p95 and p99 were 33.3 ms and 33.4 ms.
- The complete temporary Chromium companion process group used about 1.14 GiB RSS and roughly 32% of one CPU core. This includes the browser, renderer, GPU, network, and storage processes rather than only Rive's incremental cost.
- Temperature stayed between 60.4°C and 62.0°C, and `vcgencmd get_throttled` remained `0x0` throughout.

The temporary benchmark browser ran alongside the normal dashboard kiosk, making these figures conservative. It was removed after the run; the original kiosk and Miso services remained active with zero restarts.
