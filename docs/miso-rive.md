# Miso Rive companion

Miso's companion face lives at `/companion`. It is a deliberately simple, original device face using a mint, deep teal, cream, coral, amber, and sky-blue palette inspired by the color language of friendly retro electronics.

The managed Pi kiosk opens this route by default. The dashboard remains available through the companion's exit control or directly at `/`.

## Runtime and assets

- Rive Web Canvas Lite is pinned to `@rive-app/canvas-lite` 2.41.0 and served locally from `src/miso/web/vendor/rive/`.
- The runtime package tarball integrity is `sha512-vCbNEhNUQTyiM8Ufk3dd2GL5m6csUZkefm8Of9/i+FuM4ftm0ZfmwQlUWAebVVuAKVKFIMpnTDGMBNw8/BVpgg==`.
- `src/miso/web/assets/miso-face.riv` contains one 720×1280 artboard named `Miso` and one state machine named `Miso Face`.
- The `.riv` was generated from original primitive vector shapes with `rive-mcp-server` 0.5.0, then loaded and state-driven with the official Rive runtime. It contains no imported third-party artwork, fonts, images, audio, or hosted assets.
- Rive's MIT runtime license is stored alongside the vendored runtime as `src/miso/web/vendor/rive/LICENSE.txt`.

## Editing the face

A `.riv` is a compiled binary and cannot be edited in place, so the face is authored as source under `ops/face/`:

- `ops/face/scene.json` holds the static geometry: the artboard, the `face` group, and all 22 shapes.
- `ops/face/build.mjs` declares every animation and the state machine, then writes the asset.
- `ops/face/package.json` pins `rive-mcp-server` 0.5.0, the same generator that produced the original asset.

Run `make face` after changing either file, and commit the regenerated `.riv` alongside the source. `make face-verify` rebuilds in memory and fails if the committed asset is stale, so a source edit can never ship without its binary.

The build reads its own output back through the official decompiler before writing, checking the artboard name, every animation name, the input counts, and the layer count. State machine objects are not decompilable, so they are verified by count rather than by name.

Do not hand-edit the `.riv`. `rive-mcp-server`'s decompiler recovers shapes and animations but skips all state machine objects, so a decompile/recompile round trip would silently drop the state machine.

No CDN or runtime network request is required. The PWA shell caches the companion HTML, JavaScript, CSS, `.riv`, and both primary and compatibility WASM binaries for offline startup.

The canvas drawing surface is capped at 540×960 equivalent pixels and scaled to the 720×1280 DSI viewport. The artwork uses broad, flat vector shapes, so this keeps edges visually clean while bounding raster work during concurrent local inference.

## State contract

The state machine has two layers and three inputs: a numeric `state`, and two triggers, `poke` and `greet`.

The `State` layer holds one animation state per value of `state`:

| Value | State | Motion |
| ---: | --- | --- |
| 0 | active | restrained breathing and one blink per cycle |
| 1 | waking | eyes open with a short settle |
| 2 | listening | eyes wide and held wide, face lifted, mouth narrowed |
| 3 | thinking | eyes squeezed shut and high, face rocking, mouth tilting |
| 4 | tool | three processing dots |
| 5 | speaking | bounded mouth movement |
| 6 | muted | lowered eyes and a coral privacy slash |
| 7 | offline | settled, dim expression |
| 8 | error | coral cross eyes and a small shake |
| 9 | sleep | eyes closed and low, four-second deep breath, face dimmed to 0.6 |

`companion.js` maps the existing privacy-filtered `assistant_state` live events onto these values. Only the semantic state string is inspected. Transcripts, arbitrary text, tool arguments and output, credentials, provider details, and internal transition reasons are never passed to Rive.

Three states close the eyes, so each carries a second distinguishing signal: `thinking` stays at full opacity and keeps the mouth moving, `sleep` dims the whole face and breathes at half the rate, and `offline` holds still with no breath at all.

### Sleep

`sleep` is a presentation-only state derived from screen inactivity in `companion.js`. It is not a `ConversationState` and is never sent by the server. After 60 seconds with no live event and no touch, an `active` face settles into `sleep`; any live event or touch returns it to `active`. Only `active` sleeps, so muted, offline, and error stay visible.

The compositor still blanks the panel after `MISO_DISPLAY_IDLE_SECONDS` (300 by default), which leaves roughly four minutes where the sleeping face is on screen before the display powers down.

### Listening

Listening was the state people could not recognise. Its cue is deliberately the loudest: eyes go wide and stay wide, and a pulsing coral ring is drawn around the whole screen. The ring is the `.listening-ring` element in `companion.css`, not part of the `.riv`, so the Rive face and the SVG fallback show exactly the same cue. It is `pointer-events: none` and never intercepts a touch.

### Touch

The `Touch` layer reacts to a `pointerdown` anywhere on the companion stage:

| Trigger | Condition | Reaction |
| --- | --- | --- |
| `poke` | Miso is awake | blink twice and shake, then settle back into the current state |
| `greet` | Miso is asleep | `state` is set to `active`, then the eyes open and the mouth widens into a smile |

Touch reactions sit on their own state machine layer because every state on the `State` layer is reachable from Any State: a reaction placed there would be pulled back out the moment its numeric condition still held. The `Touch` layer's `rest` state keys no properties, so the `State` layer shows through between reactions.

Touching the face never arms the microphone. Starting and stopping a turn stays on the BMO buttons.

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
