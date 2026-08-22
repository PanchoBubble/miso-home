const state = {
  token: sessionStorage.getItem("miso-dashboard-token") || "",
  conversationId: null,
  requestId: null,
  developerEnabled: false,
  connected: false,
  panelReturnFocus: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function createRequestId() {
  const secureCrypto = globalThis.crypto;
  if (secureCrypto && typeof secureCrypto.randomUUID === "function") {
    return secureCrypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (secureCrypto && typeof secureCrypto.getRandomValues === "function") {
    secureCrypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

function headers(json = false) {
  const result = {};
  if (json) result["Content-Type"] = "application/json";
  if (state.token) result.Authorization = `Bearer ${state.token}`;
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(Boolean(options.body)), ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      openPanel("system");
      $(".disclosure").open = true;
    }
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function providerLabel(name) {
  return {
    "pi-ollama": "Pi Ollama",
    "lan-ollama": "LAN Ollama",
    "hosted-gpt": "Hosted GPT",
  }[name] || name;
}

function friendlyToolName(name) {
  return {
    timer_create: "Timer created",
    timer_list: "Timers checked",
    reminder_create: "Reminder created",
    reminder_list: "Reminders checked",
    shopping_add: "Shopping list updated",
    shopping_list: "Shopping list checked",
    shopping_complete: "Shopping item completed",
    developer_command: "Developer command",
  }[name] || name.replaceAll("_", " ");
}

function setConnected(connected, detail = "") {
  state.connected = connected;
  const status = $("#service-status");
  status.classList.toggle("online", connected);
  status.classList.toggle("offline", !connected);
  $(".status-copy").textContent = connected ? detail || "Online" : detail || "Offline";
  $("#connection-banner-copy").textContent = detail === "Access needed"
    ? "Dashboard access is required. Enter the token in Controls to reconnect."
    : "Miso is offline. Your draft is safe; reconnect to send it.";
  $("#offline-banner").classList.toggle("hidden", connected);
}

function renderProviders(providers) {
  const nodes = providers.map((provider) => {
    const card = document.createElement("div");
    card.className = "provider";

    const dot = document.createElement("span");
    dot.className = `provider-dot${provider.available ? " up" : ""}`;

    const copy = document.createElement("div");
    copy.className = "provider-copy";
    const label = document.createElement("strong");
    label.textContent = providerLabel(provider.name);
    const detail = document.createElement("p");
    const latency = Number.isFinite(provider.latency_ms) ? ` · ${provider.latency_ms} ms` : "";
    detail.textContent = `${provider.model || "Not configured"} · ${provider.detail}${latency}`;
    copy.append(label, detail);
    card.append(dot, copy);
    return card;
  });
  $("#providers").replaceChildren(...(nodes.length ? nodes : [emptyNode("No providers reported")]));
}

async function loadStatus() {
  try {
    const data = await api("/api/status");
    setConnected(true, `Online · ${data.service.architecture}`);
    renderProviders(data.providers);
    renderDeveloper(data.developer_mode);
  } catch (error) {
    setConnected(false, error.message === "unauthorized" ? "Access needed" : "Offline");
  }
}

function renderDeveloper(status) {
  state.developerEnabled = status.enabled;
  $("#developer-state").textContent = status.enabled ? "Enabled" : "Off";
  $("#developer-state").classList.toggle("on", status.enabled);
  $("#developer-detail").textContent = status.enabled
    ? `Scope: ${status.scope} · expires ${new Date(status.expires_at).toLocaleTimeString()}`
    : `Disabled · scope ${status.scope}`;
  $("#run-developer").disabled = !status.enabled;
}

function scrollConversation() {
  requestAnimationFrame(() => {
    const messages = $("#messages");
    messages.scrollTo({ top: messages.scrollHeight, behavior: "smooth" });
  });
}

function addMessage(role, text = "") {
  const article = document.createElement("article");
  article.className = `message ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? "You" : "み";

  const column = document.createElement("div");
  column.className = "message-column";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = role === "user" ? "You" : "Miso";
  const content = document.createElement("p");
  content.className = "message-body";
  content.textContent = text;
  column.append(meta, content);
  article.append(avatar, column);
  $("#messages").append(article);
  scrollConversation();
  return { article, content, meta };
}

function addToolResult(result, provider) {
  const card = document.createElement("details");
  card.className = `tool-card${result.ok ? "" : " failed"}`;

  const summary = document.createElement("summary");
  const summaryLeft = document.createElement("span");
  summaryLeft.className = "tool-summary";
  const icon = document.createElement("span");
  icon.className = "tool-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = result.ok ? "✓" : "!";
  const copy = document.createElement("span");
  copy.className = "tool-copy";
  const title = document.createElement("strong");
  title.textContent = friendlyToolName(result.tool);
  const detail = document.createElement("small");
  detail.textContent = `${result.status} · ${providerLabel(provider || "local")}`;
  copy.append(title, detail);
  summaryLeft.append(icon, copy);
  const chevron = document.createElement("span");
  chevron.className = "tool-chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "›";
  summary.append(summaryLeft, chevron);

  const output = document.createElement("pre");
  output.className = "tool-output";
  const payload = result.output || result.error || { status: result.status };
  output.textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  card.append(summary, output);
  $("#messages").append(card);
  scrollConversation();
}

function showProgress(message, provider = "") {
  $("#progress-copy").textContent = message || "Working…";
  $("#progress-provider").textContent = provider ? providerLabel(provider) : "";
  $("#turn-progress").classList.remove("hidden");
}

function hideProgress() {
  $("#turn-progress").classList.add("hidden");
  $("#progress-copy").textContent = "";
  $("#progress-provider").textContent = "";
}

async function sendChat(event) {
  event.preventDefault();
  if (state.requestId) return;
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;

  if ($(".empty-state")) $("#messages").replaceChildren();
  addMessage("user", text);
  input.value = "";
  resizeComposer();
  const assistant = addMessage("assistant");
  state.requestId = createRequestId();
  $("#cancel-chat").classList.remove("hidden");
  $("#send-chat").disabled = true;
  showProgress("Choosing the best local route…");

  const body = {
    text,
    request_id: state.requestId,
    conversation_id: state.conversationId,
    route_class: $("#route-class").value,
  };
  if ($("#provider-override").value) body.provider = $("#provider-override").value;

  let selectedProvider = "";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json();
      if (response.status === 401) {
        openPanel("system");
        throw new Error("Access needed");
      }
      throw new Error(error.error || `Chat failed (${response.status})`);
    }
    setConnected(true, "Online");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    while (true) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = pending.split("\n");
      pending = lines.pop() || "";
      if (done && pending.trim()) {
        lines.push(pending);
        pending = "";
      }
      for (const line of lines) {
        if (!line.trim()) continue;
        const item = JSON.parse(line);
        if (item.provider) selectedProvider = item.provider;
        if (item.type === "progress") showProgress(item.message, item.provider);
        if (item.type === "delta") assistant.content.textContent += item.text;
        if (item.type === "tool_result") addToolResult(item.result, item.provider);
        if (item.type === "complete") state.conversationId = item.conversation_id;
        if (item.type === "cancelled") showProgress("Stopped");
        if (item.type === "error") throw new Error(item.error);
      }
      if (done) break;
    }
    if (!assistant.content.textContent) assistant.content.textContent = "Done — I’ve completed that for you.";
    assistant.meta.textContent = selectedProvider ? `Miso · ${providerLabel(selectedProvider)}` : "Miso";
    await loadActivity();
  } catch (error) {
    assistant.article.classList.add("error");
    assistant.content.textContent = `I couldn’t complete that request: ${error.message}`;
    if (!navigator.onLine || error instanceof TypeError) {
      setConnected(false, "Offline");
    } else if (error.message === "Access needed") {
      setConnected(false, "Access needed");
    }
  } finally {
    state.requestId = null;
    $("#cancel-chat").classList.add("hidden");
    $("#send-chat").disabled = false;
    hideProgress();
    input.focus();
  }
}

async function cancelChat() {
  if (!state.requestId) return;
  showProgress("Stopping…");
  try {
    await api("/api/chat/cancel", {
      method: "POST",
      body: JSON.stringify({ request_id: state.requestId }),
    });
  } catch (error) {
    showProgress(`Could not stop: ${error.message}`);
  }
}

function eventLabel(event) {
  return String(event.event || event.type || "local event").replaceAll("_", " ");
}

async function loadActivity() {
  try {
    const data = await api("/api/activity?limit=35");
    const nodes = data.events.map((event) => {
      const item = document.createElement("div");
      item.className = "activity-item";
      const icon = document.createElement("span");
      icon.className = "activity-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = event.tool ? "⌘" : "↗";
      const copy = document.createElement("div");
      copy.className = "activity-copy";
      const title = document.createElement("strong");
      title.textContent = eventLabel(event);
      const detail = document.createElement("p");
      const provider = event.provider || event.selected_provider || event.tool || "local";
      const status = event.status || event.classification || "";
      const time = event.timestamp ? new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      detail.textContent = [providerLabel(provider), status, time].filter(Boolean).join(" · ");
      copy.append(title, detail);
      item.append(icon, copy);
      return item;
    });
    $("#activity").replaceChildren(...(nodes.length ? nodes : [emptyNode("No activity yet")]));
  } catch (error) {
    $("#activity").replaceChildren(emptyNode(error.message));
  }
}

function emptyNode(text) {
  const node = document.createElement("p");
  node.className = "empty";
  node.textContent = text;
  return node;
}

async function searchMemory(event) {
  event.preventDefault();
  const query = $("#memory-query").value.trim();
  if (!query) return;
  $("#memory-results").replaceChildren(emptyNode("Searching local memory…"));
  try {
    const data = await api(`/api/memory?q=${encodeURIComponent(query)}`);
    const nodes = data.results.map((result) => {
      const item = document.createElement("div");
      item.className = "memory-item";
      const title = document.createElement("strong");
      title.textContent = `${result.record_type} · ${new Date(result.created_at).toLocaleString()}`;
      const content = document.createElement("p");
      content.textContent = result.content;
      item.append(title, content);
      return item;
    });
    $("#memory-results").replaceChildren(...(nodes.length ? nodes : [emptyNode("No matches")]));
  } catch (error) {
    $("#memory-results").replaceChildren(emptyNode(error.message));
  }
}

async function developerAction(action) {
  try {
    const data = await api("/api/developer", {
      method: "POST",
      body: JSON.stringify({ action, duration_seconds: 300 }),
    });
    renderDeveloper(data.developer_mode);
    await loadActivity();
  } catch (error) {
    $("#developer-output").textContent = error.message;
  }
}

async function runDeveloperCommand() {
  try {
    const command = JSON.parse($("#developer-command").value);
    if (!Array.isArray(command)) throw new Error("Command must be a JSON array");
    const data = await api("/api/developer/command", {
      method: "POST",
      body: JSON.stringify({ command }),
    });
    $("#developer-output").textContent = JSON.stringify(data.result.output, null, 2);
    await loadActivity();
  } catch (error) {
    $("#developer-output").textContent = error.message;
  }
}

function activateTab(name) {
  $$(".panel-tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  ["activity", "memory", "system"].forEach((view) => {
    $(`#${view}-view`).classList.toggle("hidden", view !== name);
  });
  if (name === "activity") loadActivity();
  if (name === "memory") requestAnimationFrame(() => $("#memory-query").focus());
}

function openPanel(name = "activity") {
  state.panelReturnFocus = document.activeElement;
  activateTab(name);
  $("#side-panel").classList.add("open");
  $("#side-panel").setAttribute("aria-hidden", "false");
  $("#panel-scrim").classList.remove("hidden");
  $("#panel-scrim").setAttribute("aria-hidden", "false");
  document.body.classList.add("panel-open");
  if (name !== "memory") requestAnimationFrame(() => $("#close-panel").focus());
}

function closePanel() {
  $("#side-panel").classList.remove("open");
  $("#side-panel").setAttribute("aria-hidden", "true");
  $("#panel-scrim").classList.add("hidden");
  $("#panel-scrim").setAttribute("aria-hidden", "true");
  document.body.classList.remove("panel-open");
  if (state.panelReturnFocus instanceof HTMLElement) state.panelReturnFocus.focus();
  state.panelReturnFocus = null;
}

function updateRouteChip() {
  const routeClass = $("#route-class").value;
  const provider = $("#provider-override").value;
  const label = provider
    ? providerLabel(provider)
    : routeClass === "auto"
      ? "Automatic route"
      : `${routeClass[0].toUpperCase()}${routeClass.slice(1)} route`;
  $("#route-chip span:last-child").textContent = label;
}

function resizeComposer() {
  const input = $("#chat-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

function resetConversation() {
  if (state.requestId) return;
  state.conversationId = null;
  $("#messages").replaceChildren($("#empty-state-template").content.cloneNode(true));
  hideProgress();
  $("#chat-input").focus();
}

$("#chat-form").addEventListener("submit", sendChat);
$("#chat-input").addEventListener("input", resizeComposer);
$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
$("#messages").addEventListener("click", (event) => {
  const prompt = event.target.closest("[data-prompt]");
  if (!prompt) return;
  $("#chat-input").value = prompt.dataset.prompt;
  resizeComposer();
  $("#chat-form").requestSubmit();
});
$("#cancel-chat").addEventListener("click", cancelChat);
$("#new-chat").addEventListener("click", resetConversation);
$("#memory-form").addEventListener("submit", searchMemory);
$("#refresh-status").addEventListener("click", loadStatus);
$("#refresh-activity").addEventListener("click", loadActivity);
$("#service-status").addEventListener("click", loadStatus);
$("#retry-connection").addEventListener("click", loadStatus);
$("#enable-developer").addEventListener("click", () => developerAction("enable"));
$("#disable-developer").addEventListener("click", () => developerAction("disable"));
$("#run-developer").addEventListener("click", runDeveloperCommand);
$("#route-class").addEventListener("change", updateRouteChip);
$("#provider-override").addEventListener("change", updateRouteChip);
$("#route-chip").addEventListener("click", () => openPanel("system"));
$("#close-panel").addEventListener("click", closePanel);
$("#panel-scrim").addEventListener("click", closePanel);
$$("[data-open-panel]").forEach((button) => button.addEventListener("click", () => openPanel(button.dataset.openPanel)));
$$(".panel-tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#side-panel").classList.contains("open")) closePanel();
});
$("#save-token").addEventListener("click", () => {
  state.token = $("#access-token").value.trim();
  if (state.token) sessionStorage.setItem("miso-dashboard-token", state.token);
  else sessionStorage.removeItem("miso-dashboard-token");
  loadStatus();
  loadActivity();
});
window.addEventListener("online", loadStatus);
window.addEventListener("offline", () => setConnected(false, "Offline"));

$("#access-token").value = state.token;
updateRouteChip();
resizeComposer();
loadStatus();
loadActivity();
setInterval(loadStatus, 15000);
