# BMO talk and stop buttons

Two momentary push buttons on the BMO enclosure give Miso a physical control
surface: one opens listening without waiting for the wake word, the other stops
whatever is in flight. Both are optional. With `MISO_BUTTONS_ENABLED=false`, or
on any machine without GPIO, the feature reports itself disabled and nothing
else changes.

## Behaviour

**Talk (BCM 23).** A press publishes a wake event with `source=button` into the
same `WakeEvents` queue openWakeWord publishes to, so it reuses the whole
existing turn pipeline. The one difference is that the conversation skips the
spoken acknowledgement and moves `idle -> listening` directly: a press is
already an unambiguous address, and speaking "Yes?" first would put a second of
synthesis and playback between the press and the open microphone. The companion
face therefore shows the listening expression on the press itself rather than
the waking expression.

**Stop (BCM 24).** A press calls `ConversationManager.interrupt`, which does
exactly what a wake-phrase barge-in does: cancel the in-flight turn, clear
playback so speech halts mid-sentence, and return to idle. Pressing it when
nothing is in flight is a harmless no-op, recorded as `interrupted=false`.

Both presses are written to the audit log as `button_press` with
`actor_source=button`, carrying the button name and, for stop, whether anything
was actually cancelled. The conversation and its stored events keep the voice
actor: the button is a local physical control for the household voice session,
not a separate identity, and the events table only accepts `web`, `voice`, and
`system`.

`/api/status` reports the buttons block: state, configured pins, press counts
per button, rejected bounces, and the last error.

## Long press

Shipped behaviour is tap-to-talk. A tap opens the listening window, which then
closes on the normal end-of-utterance silence or the listening timeout, exactly
as a wake word does. Holding the talk button does not currently change
anything.

Hold-to-talk is the intended long-press behaviour and the plumbing for it is
already in place, so adding it needs no rewiring:

- `Button` is created with `hold_time=MISO_BUTTON_HOLD_SECONDS`, and both edges
  plus the hold threshold are bound (`when_pressed`, `when_held`,
  `when_released`).
- `ButtonRouter` exposes `press`, `hold`, and `release`. `hold` fires once while
  the button is still down and audits a `button_hold` event with the duration;
  `release` returns how long the press lasted.
- `ButtonRouter` takes optional `on_talk_hold` and `on_talk_release` callbacks.
  Wiring hold-to-talk means supplying those two, not changing the pin or edge
  handling.

It is deliberately not enabled yet because closing the utterance on release
needs the transcription side to accept an externally forced end-of-utterance,
which does not exist today. Raise that with the owner of the STT path before
implementing it.

## Wiring

Both buttons are wired the simplest way: one leg to the GPIO pin, the other leg
to ground. The internal pull-up is enabled (`MISO_BUTTON_PULL_UP=true`), so the
pin idles high and a press pulls it to ground. No external resistor is needed.

| Button | BCM  | Header pin | Ground     |
| ------ | ---- | ---------- | ---------- |
| Talk   | 23   | 16         | 14 or 20   |
| Stop   | 24   | 18         | 14 or 20   |

BCM 23 and 24 were chosen because they are plain GPIO on the 40-pin header with
no boot-time alternate function, they sit next to a ground pin so each button is
a short two-wire run, and they avoid the I2C (2, 3), SPI (7-11), UART (14, 15),
and DSI/display pins already used on this build. They also avoid BCM 0-8, which
the SoC pulls up or down at boot regardless of configuration.

Change them with `MISO_BUTTON_TALK_PIN` and `MISO_BUTTON_STOP_PIN`. Both must be
BCM numbers between 0 and 27 and must differ.

Debounce is applied twice on purpose: `bounce_time` on the gpiozero device
filters contact chatter at the pin, and `ButtonRouter` re-checks the interval so
that no edge source can double-fire a turn. A second talk press inside the
debounce window would otherwise cancel the turn the first one had just opened.
Raise `MISO_BUTTON_BOUNCE_MILLISECONDS` if the chosen switches are noisy; the
default 50 ms suits ordinary tactile buttons.

## Why gpiozero

gpiozero ships with Raspberry Pi OS, gives debounce, hold detection, and both
edges as callbacks, and selects a working pin backend (lgpio, rpi-lgpio, native)
per board without code changes. libgpiod's Python binding is a line-request and
event-file-descriptor API with no debounce or hold abstraction, and its v1 and
v2 APIs are incompatible, so it would mean hand-rolling and testing an event
loop that gpiozero already provides. gpiozero also ships `MockFactory`, which
lets the unit tests drive the real device stack over simulated pins.

gpiozero is not a dependency of the Miso package. The import happens inside
`ButtonManager.start`, and its absence disables the feature with a warning
rather than failing the service.

## Installing

`ops/install-miso-runtime.sh` installs `ops/systemd/miso-buttons.env` to
`/etc/miso/miso-buttons.env` if it is not already present, adds the `miso` user
to the `gpio` group when that group exists, and `miso.service` reads the file
and is allowed the GPIO character devices. Install gpiozero on the Pi with:

```bash
sudo apt install python3-gpiozero python3-lgpio
```

Then enable the feature:

```text
MISO_BUTTONS_ENABLED=true
MISO_BUTTON_TALK_PIN=23
MISO_BUTTON_STOP_PIN=24
MISO_BUTTON_PULL_UP=true
MISO_BUTTON_BOUNCE_MILLISECONDS=50
MISO_BUTTON_HOLD_SECONDS=1.0
```

The buttons ride on the wake manager, which the conversation state machine
already requires, so `MISO_WAKE_ENABLED` must be true for a press to open a
turn. Confirm with `/api/status`: `buttons.state` should be `listening` and the
press counters should climb.
