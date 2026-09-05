const RIVE_ARTBOARD = "Miso";
const RIVE_STATE_MACHINE = "Miso Face";
const RIVE_STATE_INPUT = "state";
const RIVE_POKE_INPUT = "poke";
const RIVE_GREET_INPUT = "greet";
const RIVE_MAX_RENDER_PIXELS = 540 * 960;
// Screen inactivity, not conversation state: Miso settles into a sleeping
// face well before the compositor blanks the panel at
// MISO_DISPLAY_IDLE_SECONDS, so an unattended screen reads as asleep rather
// than as awake and ignoring the room.
const SLEEP_AFTER_MS = 60000;
const REACTION_MS = Object.freeze({ poked: 500, greeting: 800 });
const CAPTION_MAX_AGE_MS = 30000;
const CAPTION_MIN_VISIBLE_MS = 6000;
const CAPTION_MAX_VISIBLE_MS = 20000;
const CAPTION_MS_PER_CHARACTER = 55;
const CAPTURE_CUE_MAX_AGE_MS = 5000;
// The panel keeps showing the last polled forecast, but stops presenting it as
// current once the poller has missed roughly three turns.
const WEATHER_STALE_AFTER_MS = 2_700_000;
const CAPTURE_CUE_TIMEOUT_MS = 20000;

// Shown while the microphone is still capturing, so a wake that landed
// looks different from one that was missed even before whisper answers.
const CAPTURE_CUES = Object.freeze({
  capturing: "Listening\u2026",
  transcribing: "Working out what you said\u2026",
});

// Open-Meteo weather codes, grouped down to the glyphs the panel draws.
const WEATHER_ICONS = Object.freeze([
  { codes: [0], icon: "wx-clear" },
  { codes: [1, 2], icon: "wx-partly" },
  { codes: [3], icon: "wx-cloud" },
  { codes: [45, 48], icon: "wx-fog" },
  { codes: [71, 73, 75, 77, 85, 86], icon: "wx-snow" },
  { codes: [95, 96, 99], icon: "wx-storm" },
]);

// Codes are the contract with the Rive `state` input built by
// ops/face/build.mjs. Keep both tables and docs/miso-rive.md in step.
const COMPANION_STATES = Object.freeze({
  active: { code: 0, label: "Awake" },
  waking: { code: 1, label: "Waking" },
  listening: { code: 2, label: "Listening" },
  thinking: { code: 3, label: "Thinking" },
  tool: { code: 4, label: "Using a tool" },
  speaking: { code: 5, label: "Speaking" },
  muted: { code: 6, label: "Microphone muted" },
  offline: { code: 7, label: "Offline" },
  error: { code: 8, label: "Needs attention" },
  sleep: { code: 9, label: "Asleep" },
});

const CONVERSATION_TO_COMPANION = Object.freeze({
  disabled: "offline",
  stopped: "offline",
  idle: "active",
  acknowledging: "waking",
  listening: "listening",
  follow_up: "listening",
  checking_back: "listening",
  transcribing: "thinking",
  routing: "thinking",
  using_tool: "tool",
  speaking: "speaking",
  goodbye: "speaking",
  error: "error",
});

const companion = {
  token: localStorage.getItem("miso-dashboard-token") || "",
  current: "active",
  lastEventId: Number(localStorage.getItem("miso-companion-event-id") || "0"),
  liveGeneration: 0,
  liveAbort: null,
  riveInstance: null,
  riveStateInput: null,
  riveTriggers: {},
  resizeObserver: null,
  captionTimer: null,
  sleepTimer: null,
  weatherTimer: null,
  weatherUpdatedAt: 0,
  reactionTimer: null,
};

const stage = document.querySelector(".companion-stage");
const canvas = document.querySelector("#rive-face");
const fallback = document.querySelector("#fallback-face");
const statusCopy = document.querySelector("#companion-status-copy");
const caption = document.querySelector("#companion-caption");
const captionCopy = document.querySelector("#companion-caption-copy");
const captionSpeaker = document.querySelector(".caption-speaker");
const weatherPanel = document.querySelector("#weather-panel");
const weatherIcon = document.querySelector("#weather-icon-use");
const weatherTemperature = document.querySelector("#weather-temperature");
const weatherRain = document.querySelector("#weather-rain");
const weatherPlace = document.querySelector("#weather-place");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function weatherIconName(code) {
  const match = WEATHER_ICONS.find((entry) => entry.codes.includes(code));
  // Everything left over is some form of drizzle, rain, or shower.
  return match ? match.icon : "wx-rain";
}

function formatTemperature(value, unit) {
  const rounded = Math.round(Number(value));
  if (!Number.isFinite(rounded)) return "--";
  // Open-Meteo already sends the degree sign with the unit.
  return `${rounded}${unit || ""}`;
}

// One timer that fires when this reading goes stale, rather than a ticker:
// the panel has nothing to check between the poller's updates.
function markWeatherStale() {
  if (companion.weatherTimer) window.clearTimeout(companion.weatherTimer);
  companion.weatherTimer = null;
  const remaining = companion.weatherUpdatedAt + WEATHER_STALE_AFTER_MS - Date.now();
  weatherPanel.dataset.weatherStale = remaining <= 0 ? "true" : "false";
  if (remaining <= 0) return;
  companion.weatherTimer = window.setTimeout(() => {
    companion.weatherTimer = null;
    weatherPanel.dataset.weatherStale = "true";
  }, remaining);
}

// The panel renders whatever the poller last stored. It never fetches weather
// itself, so every screen in the house shows the same reading Miso speaks.
function showWeather(panel) {
  if (!panel || typeof panel !== "object" || panel.available === false) return;
  const code = Number(panel.weather_code);
  weatherIcon.setAttribute("href", `#${weatherIconName(code)}`);
  weatherTemperature.textContent = formatTemperature(panel.temperature, panel.temperature_unit);
  weatherRain.textContent = typeof panel.rain_text === "string" ? panel.rain_text : "";
  const place = [panel.location, panel.conditions].filter(Boolean).join(" \u00b7 ");
  weatherPlace.textContent = place;
  weatherPanel.dataset.weatherRaining = panel.raining_now ? "true" : "false";
  const updated = Date.parse(panel.updated_at);
  companion.weatherUpdatedAt = Number.isFinite(updated) ? updated : Date.now();
  weatherPanel.hidden = false;
  markWeatherStale();
}

function requestHeaders() {
  return companion.token ? { Authorization: `Bearer ${companion.token}` } : {};
}

function normalizeState(value) {
  const mapped = CONVERSATION_TO_COMPANION[value] || value;
  return Object.hasOwn(COMPANION_STATES, mapped) ? mapped : "active";
}

// Only a fully awake, idle Miso falls asleep. Muted, offline, and error all
// carry information someone still needs to see on the panel.
function scheduleSleep() {
  if (companion.sleepTimer) window.clearTimeout(companion.sleepTimer);
  companion.sleepTimer = null;
  if (companion.current !== "active") return;
  companion.sleepTimer = window.setTimeout(() => applyCompanionState("sleep"), SLEEP_AFTER_MS);
}

function applyCompanionState(value) {
  const name = normalizeState(value);
  const definition = COMPANION_STATES[name];
  companion.current = name;
  document.body.dataset.companionState = name;
  statusCopy.textContent = `Miso · ${definition.label}`;
  if (companion.riveStateInput) companion.riveStateInput.value = definition.code;
  scheduleSleep();
}

function playReaction(name) {
  companion.riveTriggers[name === "greeting" ? RIVE_GREET_INPUT : RIVE_POKE_INPUT]?.fire();
  if (companion.reactionTimer) window.clearTimeout(companion.reactionTimer);
  // Restarting the attribute lets a second touch replay the animation instead
  // of being swallowed as a no-op class change.
  delete document.body.dataset.companionReaction;
  void document.body.offsetWidth;
  document.body.dataset.companionReaction = name;
  companion.reactionTimer = window.setTimeout(() => {
    companion.reactionTimer = null;
    delete document.body.dataset.companionReaction;
  }, REACTION_MS[name]);
}

// Touching the face is affection, not a command: it never arms the
// microphone. Starting and stopping a turn stays on the BMO buttons.
function handleFaceTouch(event) {
  if (event.target.closest("a, button")) return;
  if (companion.current === "sleep") {
    applyCompanionState("active");
    playReaction("greeting");
    return;
  }
  playReaction("poked");
  scheduleSleep();
}

function clearCaption() {
  if (companion.captionTimer) window.clearTimeout(companion.captionTimer);
  companion.captionTimer = null;
  caption.hidden = true;
  caption.dataset.captionState = "final";
  caption.dataset.captionSpeaker = "miso";
  delete caption.dataset.captionCue;
  captionSpeaker.textContent = "Miso";
  captionCopy.textContent = "";
}

function showCaption(text, final = true, speaker = "Miso") {
  const normalized = text.trim();
  if (!normalized) return;
  if (companion.captionTimer) window.clearTimeout(companion.captionTimer);
  companion.captionTimer = null;
  captionSpeaker.textContent = speaker;
  caption.dataset.captionSpeaker = speaker.toLowerCase();
  delete caption.dataset.captionCue;
  captionCopy.textContent = normalized;
  caption.hidden = false;
  // A draft caption is the answer still being generated. It must not time out
  // mid-sentence, so the auto-hide only starts once the text is settled.
  caption.dataset.captionState = final ? "final" : "draft";
  if (!final) return;
  const visibleMilliseconds = Math.min(
    CAPTION_MAX_VISIBLE_MS,
    Math.max(CAPTION_MIN_VISIBLE_MS, normalized.length * CAPTION_MS_PER_CHARACTER),
  );
  companion.captionTimer = window.setTimeout(clearCaption, visibleMilliseconds);
}

function showCaptureCue(state) {
  const label = CAPTURE_CUES[state];
  if (!label) {
    // Anything else ended the capture. Only retire the cue itself: a real
    // transcript or answer may already have replaced it.
    if (caption.dataset.captionCue) clearCaption();
    return;
  }
  showCaption(label, false, "You");
  caption.dataset.captionCue = state;
  companion.captionTimer = window.setTimeout(clearCaption, CAPTURE_CUE_TIMEOUT_MS);
}

function useFallback() {
  companion.riveInstance?.cleanup();
  companion.riveInstance = null;
  companion.riveStateInput = null;
  companion.riveTriggers = {};
  companion.resizeObserver?.disconnect();
  companion.resizeObserver = null;
  canvas.classList.add("is-hidden");
  fallback.classList.remove("is-hidden");
}

function resizeRiveSurface(instance) {
  const cssPixels = Math.max(1, canvas.clientWidth * canvas.clientHeight);
  const boundedRatio = Math.sqrt(RIVE_MAX_RENDER_PIXELS / cssPixels);
  const renderRatio = Math.max(0.5, Math.min(window.devicePixelRatio || 1, boundedRatio));
  instance.resizeDrawingSurfaceToCanvas(renderRatio);
}

function loadRiveFace() {
  useFallback();
  if (reducedMotion.matches || !globalThis.rive) return;

  globalThis.rive.RuntimeLoader.setWasmUrl("/vendor/rive/rive.wasm");
  globalThis.rive.RuntimeLoader.setWasmFallbackUrl("/vendor/rive/rive_fallback.wasm");

  let instance;
  instance = new globalThis.rive.Rive({
    src: "/assets/miso-face.riv",
    canvas,
    artboard: RIVE_ARTBOARD,
    stateMachines: RIVE_STATE_MACHINE,
    autoplay: true,
    layout: new globalThis.rive.Layout({
      fit: globalThis.rive.Fit.Contain,
      alignment: globalThis.rive.Alignment.Center,
    }),
    onLoad: () => {
      const inputs = instance.stateMachineInputs(RIVE_STATE_MACHINE) || [];
      companion.riveStateInput = inputs.find((input) => input.name === RIVE_STATE_INPUT) || null;
      if (!companion.riveStateInput) {
        useFallback();
        return;
      }
      // Touch reactions are optional: an older asset without them still gets
      // the fallback CSS reaction rather than dropping to the SVG face.
      companion.riveTriggers = Object.fromEntries(
        [RIVE_POKE_INPUT, RIVE_GREET_INPUT]
          .map((name) => [name, inputs.find((input) => input.name === name)])
          .filter(([, input]) => input),
      );
      companion.riveInstance = instance;
      resizeRiveSurface(instance);
      companion.resizeObserver = new ResizeObserver(() => resizeRiveSurface(instance));
      companion.resizeObserver.observe(canvas);
      applyCompanionState(companion.current);
      fallback.classList.add("is-hidden");
      canvas.classList.remove("is-hidden");
    },
    onLoadError: useFallback,
  });
  companion.riveInstance = instance;
}

async function loadInitialState() {
  try {
    const response = await fetch("/api/status", { headers: requestHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error(response.status === 401 ? "access" : "offline");
    const payload = await response.json();
    document.body.dataset.connection = "online";
    applyCompanionState(payload.conversation?.state || "idle");
    showWeather(payload.weather);
  } catch (_error) {
    document.body.dataset.connection = "offline";
    applyCompanionState("offline");
  }
}

function parseServerEvent(block) {
  let eventId = 0;
  const data = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("id:")) eventId = Number(line.slice(3).trim());
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  if (!data.length) return null;
  const event = JSON.parse(data.join("\n"));
  event.id = event.id || eventId;
  return event;
}

function handleLiveEvent(event) {
  if (!Number.isSafeInteger(event.id) || event.id <= companion.lastEventId) return;
  companion.lastEventId = event.id;
  localStorage.setItem("miso-companion-event-id", String(event.id));
  if (event.type === "assistant_state" && typeof event.payload?.state === "string") {
    applyCompanionState(event.payload.state);
    if (event.payload.state === "acknowledging") clearCaption();
  }
  if (
    event.type === "user_capture"
    && typeof event.payload?.state === "string"
    && Date.now() - Date.parse(event.created_at) <= CAPTURE_CUE_MAX_AGE_MS
  ) {
    showCaptureCue(event.payload.state);
  }
  if (
    event.type === "assistant_caption"
    && typeof event.payload?.text === "string"
    && Date.now() - Date.parse(event.created_at) <= CAPTION_MAX_AGE_MS
  ) {
    showCaption(event.payload.text, event.payload.final !== false);
  }
  if (
    event.type === "user_caption"
    && typeof event.payload?.text === "string"
    && Date.now() - Date.parse(event.created_at) <= CAPTION_MAX_AGE_MS
  ) {
    showCaption(event.payload.text, true, "You");
  }
  if (event.type === "weather_update") {
    showWeather(event.payload);
  }
  if (
    event.type === "assistant_error"
    && typeof event.payload?.text === "string"
    && Date.now() - Date.parse(event.created_at) <= CAPTION_MAX_AGE_MS
  ) {
    showCaption(event.payload.text, true, "Error");
  }
}

function stopLiveEvents() {
  companion.liveGeneration += 1;
  companion.liveAbort?.abort();
  companion.liveAbort = null;
}

async function startLiveEvents() {
  stopLiveEvents();
  const generation = companion.liveGeneration;
  let delay = 1000;
  while (generation === companion.liveGeneration && navigator.onLine) {
    const controller = new AbortController();
    companion.liveAbort = controller;
    try {
      const response = await fetch(`/api/events?after=${companion.lastEventId}`, {
        headers: requestHeaders(),
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("live_event_connection_failed");
      document.body.dataset.connection = "online";
      delay = 1000;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      while (generation === companion.liveGeneration) {
        const { done, value } = await reader.read();
        pending += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = pending.split("\n\n");
        pending = blocks.pop() || "";
        blocks.forEach((block) => {
          const event = parseServerEvent(block);
          if (event) handleLiveEvent(event);
        });
        if (done) throw new Error("live_event_stream_ended");
      }
    } catch (_error) {
      if (controller.signal.aborted || generation !== companion.liveGeneration) return;
      document.body.dataset.connection = "offline";
      applyCompanionState("offline");
      await new Promise((resolve) => window.setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 30000);
    }
  }
}

stage.addEventListener("pointerdown", handleFaceTouch);
reducedMotion.addEventListener("change", loadRiveFace);
window.addEventListener("online", () => {
  loadInitialState();
  startLiveEvents();
});
window.addEventListener("offline", () => {
  stopLiveEvents();
  document.body.dataset.connection = "offline";
  applyCompanionState("offline");
});
window.addEventListener("beforeunload", () => {
  stopLiveEvents();
  clearCaption();
  if (companion.sleepTimer) window.clearTimeout(companion.sleepTimer);
  if (companion.reactionTimer) window.clearTimeout(companion.reactionTimer);
  if (companion.weatherTimer) window.clearTimeout(companion.weatherTimer);
  useFallback();
});

loadRiveFace();
applyCompanionState(companion.current);
loadInitialState().then(startLiveEvents);
