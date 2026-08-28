const RIVE_ARTBOARD = "Miso";
const RIVE_STATE_MACHINE = "Miso Face";
const RIVE_STATE_INPUT = "state";

const COMPANION_STATES = Object.freeze({
  idle: { code: 0, label: "Ready" },
  waking: { code: 1, label: "Waking" },
  listening: { code: 2, label: "Listening" },
  thinking: { code: 3, label: "Thinking" },
  tool: { code: 4, label: "Using a tool" },
  speaking: { code: 5, label: "Speaking" },
  muted: { code: 6, label: "Microphone muted" },
  offline: { code: 7, label: "Offline" },
  error: { code: 8, label: "Needs attention" },
});

const CONVERSATION_TO_COMPANION = Object.freeze({
  disabled: "offline",
  stopped: "offline",
  idle: "idle",
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
  current: "idle",
  lastEventId: Number(localStorage.getItem("miso-companion-event-id") || "0"),
  liveGeneration: 0,
  liveAbort: null,
  riveInstance: null,
  riveStateInput: null,
  resizeObserver: null,
};

const canvas = document.querySelector("#rive-face");
const fallback = document.querySelector("#fallback-face");
const statusCopy = document.querySelector("#companion-status-copy");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function requestHeaders() {
  return companion.token ? { Authorization: `Bearer ${companion.token}` } : {};
}

function normalizeState(value) {
  const mapped = CONVERSATION_TO_COMPANION[value] || value;
  return Object.hasOwn(COMPANION_STATES, mapped) ? mapped : "idle";
}

function applyCompanionState(value) {
  const name = normalizeState(value);
  const definition = COMPANION_STATES[name];
  companion.current = name;
  document.body.dataset.companionState = name;
  statusCopy.textContent = `Miso · ${definition.label}`;
  if (companion.riveStateInput) companion.riveStateInput.value = definition.code;
}

function useFallback() {
  companion.riveInstance?.cleanup();
  companion.riveInstance = null;
  companion.riveStateInput = null;
  companion.resizeObserver?.disconnect();
  companion.resizeObserver = null;
  canvas.classList.add("is-hidden");
  fallback.classList.remove("is-hidden");
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
      companion.riveInstance = instance;
      instance.resizeDrawingSurfaceToCanvas();
      companion.resizeObserver = new ResizeObserver(() => instance.resizeDrawingSurfaceToCanvas());
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
  useFallback();
});

loadRiveFace();
loadInitialState().then(startLiveEvents);
