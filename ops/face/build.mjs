#!/usr/bin/env node
// Regenerates src/miso/web/assets/miso-face.riv.
//
// The .riv is a compiled binary, so the face is authored here instead: the
// static geometry lives in scene.json and every animation and the state
// machine are declared below. Run `make face` after changing either.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { buildScene, writeRiv } from "rive-mcp-server/dist/rivWriter.js";
import { decompileRiv } from "rive-mcp-server/dist/rivDecompile.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUTPUT = path.resolve(HERE, "../../src/miso/web/assets/miso-face.riv");
const FPS = 60;

const STATE_MACHINE = "Miso Face";

// Order is the contract: the index is the numeric `state` input companion.js
// writes, and docs/miso-rive.md documents the same table.
const STATES = [
  "active",
  "waking",
  "listening",
  "thinking",
  "tool",
  "speaking",
  "muted",
  "offline",
  "error",
  "sleep",
];

// Every animation keys the same property set so a state can never inherit a
// stale pose from whichever state preceded it. Overrides replace an entry.
const REST_POSE = {
  "face.x": 360,
  "face.y": 500,
  "face.scaleX": 1,
  "face.scaleY": 1,
  "face.opacity": 1,
  "left-eye.x": -132,
  "left-eye.y": -84,
  "left-eye.scaleX": 1,
  "left-eye.scaleY": 1,
  "left-eye.opacity": 1,
  "right-eye.x": 132,
  "right-eye.y": -84,
  "right-eye.scaleX": 1,
  "right-eye.scaleY": 1,
  "right-eye.opacity": 1,
  "left-cheek.scaleX": 1,
  "left-cheek.scaleY": 1,
  "right-cheek.scaleX": 1,
  "right-cheek.scaleY": 1,
  "mouth.x": 0,
  "mouth.y": 92,
  "mouth.rotation": 0,
  "mouth.scaleX": 1,
  "mouth.scaleY": 1,
  "mouth.opacity": 1,
  "mute-slash.opacity": 0,
  "tool-dot-left.opacity": 0,
  "tool-dot-center.opacity": 0,
  "tool-dot-right.opacity": 0,
  "tool-dot-left.scaleY": 0.62,
  "tool-dot-center.scaleY": 0.62,
  "tool-dot-right.scaleY": 0.62,
  "error-left-a.opacity": 0,
  "error-left-b.opacity": 0,
  "error-right-a.opacity": 0,
  "error-right-b.opacity": 0,
};

function animation(name, duration, loop, overrides) {
  const pose = { ...REST_POSE, ...overrides };
  const tracks = Object.entries(pose).map(([key, value]) => {
    const separator = key.lastIndexOf(".");
    const keyframes = Array.isArray(value)
      ? value.map(([frame, at]) => ({ frame, value: at }))
      : [{ frame: 0, value }, { frame: duration, value }];
    return {
      target: key.slice(0, separator),
      property: key.slice(separator + 1),
      keyframes,
    };
  });
  return { name, fps: FPS, duration, loop, tracks };
}

// Mirrors an override across the two eyes so a blink can never go lopsided.
function eyes(property, value) {
  return { [`left-eye.${property}`]: value, [`right-eye.${property}`]: value };
}

const ANIMATIONS = [
  // Awake and armed. Restrained: a slow breath and one blink per cycle, so
  // listening reads as a clear change rather than more of the same.
  animation("active", 180, "loop", {
    "face.y": [[0, 500], [90, 490], [180, 500]],
    ...eyes("scaleY", [[0, 1], [72, 1], [76, 0.08], [81, 1], [180, 1]]),
  }),
  animation("waking", 42, "oneShot", {
    ...eyes("scaleY", [[0, 0.08], [25, 1.18], [42, 1]]),
    "face.scaleY": [[0, 0.94], [25, 1.035], [42, 1]],
  }),
  // The state that was hardest to recognise. Eyes go visibly wide and stay
  // wide, the face lifts, and the mouth narrows to an attentive line. The
  // pulsing screen-edge ring in companion.css carries the rest.
  animation("listening", 96, "loop", {
    "face.y": [[0, 492], [48, 486], [96, 492]],
    ...eyes("scaleX", [[0, 1.14], [48, 1.2], [96, 1.14]]),
    ...eyes("scaleY", [[0, 1.3], [48, 1.42], [96, 1.3]]),
    "mouth.scaleX": 0.55,
  }),
  // Eyes shut in concentration, held high on the face and squeezing slowly.
  // Sleep also closes the eyes, so this stays at full opacity and keeps the
  // mouth working to tell the two apart at a glance.
  animation("thinking", 150, "loop", {
    ...eyes("y", -78),
    ...eyes("scaleY", [[0, 0.1], [75, 0.17], [150, 0.1]]),
    "face.x": [[0, 354], [75, 366], [150, 354]],
    "mouth.rotation": [[0, -5], [75, 5], [150, -5]],
    "mouth.scaleX": 0.7,
  }),
  animation("tool", 84, "loop", {
    "mouth.opacity": 0,
    "tool-dot-left.opacity": 1,
    "tool-dot-center.opacity": 1,
    "tool-dot-right.opacity": 1,
    "tool-dot-left.scaleY": [[0, 0.62], [20, 1.28], [50, 0.62], [84, 0.62]],
    "tool-dot-center.scaleY": [[0, 0.62], [32, 1.28], [62, 0.62], [84, 0.62]],
    "tool-dot-right.scaleY": [[0, 0.62], [44, 1.28], [74, 0.62], [84, 0.62]],
  }),
  animation("speaking", 72, "loop", {
    "mouth.scaleY": [[0, 1], [12, 4.8], [25, 1.6], [40, 3.7], [55, 1.3], [72, 1]],
  }),
  animation("muted", 80, "loop", {
    ...eyes("scaleY", 0.64),
    "mouth.scaleX": 0.58,
    "mute-slash.opacity": [[0, 0.72], [40, 1], [80, 0.72]],
  }),
  animation("offline", 180, "loop", {
    "face.opacity": [[0, 0.46], [90, 0.62], [180, 0.46]],
    ...eyes("scaleY", 0.12),
    "mouth.scaleX": 0.58,
  }),
  animation("error", 42, "loop", {
    "face.x": [[0, 360], [7, 352], [14, 368], [21, 355], [28, 365], [42, 360]],
    ...eyes("opacity", 0),
    "error-left-a.opacity": 1,
    "error-left-b.opacity": 1,
    "error-right-a.opacity": 1,
    "error-right-b.opacity": 1,
    "mouth.scaleX": 0.7,
  }),
  // Four-second breath, twice the travel of active, eyes flat and low, and a
  // dimmed face. Thinking never dims, which is what separates the two.
  animation("sleep", 240, "loop", {
    "face.y": [[0, 506], [120, 518], [240, 506]],
    "face.scaleY": [[0, 1], [120, 1.02], [240, 1]],
    "face.opacity": 0.6,
    ...eyes("y", -66),
    ...eyes("scaleY", [[0, 0.06], [120, 0.08], [240, 0.06]]),
    "mouth.scaleX": 0.5,
  }),
];

// Touch reactions live on their own state machine layer. On the main layer
// every state is reachable from Any State, so a reaction placed there would be
// yanked back the moment its condition still held. A second layer keys only
// what it animates and lets the main layer show through from `rest`.
const REACTIONS = [
  { name: "rest", fps: FPS, duration: 1, loop: "oneShot", tracks: [] },
  {
    name: "poked",
    fps: FPS,
    duration: 30,
    loop: "oneShot",
    tracks: [
      {
        target: "face",
        property: "x",
        keyframes: [
          { frame: 0, value: 360 }, { frame: 4, value: 348 }, { frame: 9, value: 372 },
          { frame: 14, value: 352 }, { frame: 19, value: 368 }, { frame: 24, value: 357 },
          { frame: 30, value: 360 },
        ],
      },
      {
        target: "face",
        property: "scaleY",
        keyframes: [
          { frame: 0, value: 1 }, { frame: 6, value: 0.96 }, { frame: 16, value: 1.02 },
          { frame: 30, value: 1 },
        ],
      },
      ...["left-eye", "right-eye"].map((target) => ({
        target,
        property: "scaleY",
        keyframes: [
          { frame: 0, value: 1 }, { frame: 5, value: 0.08 }, { frame: 11, value: 1 },
          { frame: 16, value: 0.08 }, { frame: 22, value: 1 }, { frame: 30, value: 1 },
        ],
      })),
    ],
  },
  {
    name: "greeting",
    fps: FPS,
    duration: 48,
    loop: "oneShot",
    tracks: [
      {
        target: "face",
        property: "y",
        keyframes: [
          { frame: 0, value: 512 }, { frame: 18, value: 492 }, { frame: 32, value: 502 },
          { frame: 48, value: 500 },
        ],
      },
      {
        target: "face",
        property: "opacity",
        keyframes: [{ frame: 0, value: 0.6 }, { frame: 14, value: 1 }, { frame: 48, value: 1 }],
      },
      ...["left-eye", "right-eye"].flatMap((target) => [
        {
          target,
          property: "y",
          keyframes: [{ frame: 0, value: -66 }, { frame: 20, value: -84 }, { frame: 48, value: -84 }],
        },
        {
          target,
          property: "scaleY",
          keyframes: [
            { frame: 0, value: 0.06 }, { frame: 14, value: 1.22 }, { frame: 26, value: 0.95 },
            { frame: 48, value: 1 },
          ],
        },
      ]),
      // A wide, tall rounded mouth is this face's smile.
      {
        target: "mouth",
        property: "scaleX",
        keyframes: [
          { frame: 0, value: 0.5 }, { frame: 20, value: 1.3 }, { frame: 34, value: 1.24 },
          { frame: 48, value: 1.2 },
        ],
      },
      {
        target: "mouth",
        property: "scaleY",
        keyframes: [
          { frame: 0, value: 1 }, { frame: 20, value: 2.4 }, { frame: 34, value: 2 },
          { frame: 48, value: 1.8 },
        ],
      },
      ...["left-cheek", "right-cheek"].flatMap((target) =>
        ["scaleX", "scaleY"].map((property) => ({
          target,
          property,
          keyframes: [
            { frame: 0, value: 1 }, { frame: 20, value: 1.4 }, { frame: 48, value: 1.15 },
          ],
        })),
      ),
    ],
  },
];

const REACTION_EXIT_MS = { poked: 500, greeting: 800 };

function stateMachine() {
  return {
    name: STATE_MACHINE,
    inputs: [
      { name: "state", type: "number", initial: 0 },
      { name: "poke", type: "trigger" },
      { name: "greet", type: "trigger" },
    ],
    layers: [
      {
        name: "State",
        states: STATES.map((name) => ({ name, animation: name })),
        transitions: [
          { from: "entry", to: "active" },
          ...STATES.map((name, code) => ({
            from: "any",
            to: name,
            durationMs: 160,
            condition: { input: "state", op: "==", value: code },
          })),
        ],
      },
      {
        name: "Touch",
        states: [
          { name: "rest", animation: "rest" },
          { name: "poked", animation: "poked" },
          { name: "greeting", animation: "greeting" },
        ],
        transitions: [
          { from: "entry", to: "rest" },
          { from: "any", to: "poked", durationMs: 60, condition: { input: "poke" } },
          { from: "any", to: "greeting", durationMs: 60, condition: { input: "greet" } },
          { from: "poked", to: "rest", durationMs: 140, exitTimeMs: REACTION_EXIT_MS.poked },
          { from: "greeting", to: "rest", durationMs: 220, exitTimeMs: REACTION_EXIT_MS.greeting },
        ],
      },
    ],
  };
}

function build() {
  const geometry = JSON.parse(fs.readFileSync(path.join(HERE, "scene.json"), "utf8"));
  const scene = {
    ...geometry,
    animations: [...ANIMATIONS, ...REACTIONS],
    stateMachine: stateMachine(),
  };
  const built = buildScene(scene);
  if (built.warnings?.length) {
    throw new Error(`scene warnings: ${built.warnings.join("; ")}`);
  }
  return Buffer.from(writeRiv(built.objects ?? built));
}

// Reads the asset back through the official decompiler so a silently corrupt
// write cannot reach the Pi. State machine objects are not decompilable, so
// they are checked by count rather than by name.
async function verify(bytes) {
  const { scene, coverage } = await decompileRiv(bytes);
  const problems = [];
  if (coverage.warnings?.length) problems.push(`decompile warnings: ${coverage.warnings.join("; ")}`);
  if (scene.artboard.name !== "Miso") problems.push(`artboard is '${scene.artboard.name}'`);
  const expected = [...ANIMATIONS, ...REACTIONS].map((a) => a.name);
  const found = scene.animations.map((a) => a.name);
  const missing = expected.filter((name) => !found.includes(name));
  if (missing.length) problems.push(`missing animations: ${missing.join(", ")}`);
  const skipped = coverage.skipped ?? {};
  if (skipped.StateMachineNumber !== 1) problems.push("expected one number input");
  if (skipped.StateMachineTrigger !== 2) problems.push("expected two trigger inputs");
  if (skipped.StateMachineLayer !== 2) problems.push("expected two state machine layers");
  if (skipped.AnimationState !== expected.length) {
    problems.push(`expected ${expected.length} animation states, saw ${skipped.AnimationState}`);
  }
  if (problems.length) throw new Error(problems.join("\n"));
  return { shapes: scene.shapes.length, animations: found.length };
}

const bytes = build();
const summary = await verify(bytes);
if (process.argv.includes("--verify")) {
  const current = fs.readFileSync(OUTPUT);
  if (!current.equals(bytes)) {
    console.error("miso-face.riv is stale: run `make face`");
    process.exit(1);
  }
  console.log("miso-face.riv is up to date");
} else {
  fs.writeFileSync(OUTPUT, bytes);
  console.log(
    `wrote ${path.relative(process.cwd(), OUTPUT)} `
    + `(${bytes.length} bytes, ${summary.shapes} shapes, ${summary.animations} animations)`,
  );
}
