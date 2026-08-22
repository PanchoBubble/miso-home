const state = {
  token: sessionStorage.getItem("miso-dashboard-token") || "",
  conversationId: null,
  requestId: null,
  developerEnabled: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

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
    if (response.status === 401) $("#access-panel").classList.remove("hidden");
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function providerLabel(name) {
  return { "pi-ollama": "Pi Ollama", "lan-ollama": "LAN Ollama", "hosted-gpt": "Hosted GPT" }[name] || name;
}

async function loadStatus() {
  try {
    const data = await api("/api/status");
    const service = $("#service-status");
    service.textContent = `Online · ${data.service.architecture}`;
    service.classList.add("online");
    $("#providers").replaceChildren(...data.providers.map((provider) => {
      const card = document.createElement("div");
      card.className = "provider";
      const line = document.createElement("div");
      line.className = "provider-line";
      const label = document.createElement("strong");
      label.textContent = providerLabel(provider.name);
      const dot = document.createElement("span");
      dot.className = `dot${provider.available ? " up" : ""}`;
      line.append(label, dot);
      const detail = document.createElement("small");
      detail.textContent = `${provider.model || "not configured"} · ${provider.detail} · ${provider.latency_ms}ms`;
      card.append(line, detail);
      return card;
    }));
    renderDeveloper(data.developer_mode);
  } catch (error) {
    $("#service-status").textContent = error.message;
    $("#service-status").classList.remove("online");
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

function addMessage(role, text = "") {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : "Miso";
  const content = document.createElement("p");
  content.textContent = text;
  article.append(label, content);
  $("#messages").append(article);
  article.scrollIntoView({ behavior: "smooth", block: "end" });
  return content;
}

function addToolResult(result, provider) {
  const card = document.createElement("div");
  card.className = "tool-card";
  const output = result.output ? JSON.stringify(result.output, null, 2) : result.error;
  card.textContent = `${result.tool} · ${result.status} · ${provider || "unknown provider"}\n${output || ""}`;
  $("#messages").append(card);
  card.scrollIntoView({ behavior: "smooth", block: "end" });
}

async function sendChat(event) {
  event.preventDefault();
  if (state.requestId) return;
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  addMessage("user", text);
  input.value = "";
  const assistant = addMessage("assistant");
  state.requestId = crypto.randomUUID();
  $("#cancel-chat").classList.remove("hidden");
  $("#send-chat").disabled = true;
  $("#progress").textContent = "Acknowledging…";
  const body = {
    text,
    request_id: state.requestId,
    conversation_id: state.conversationId,
    route_class: $("#route-class").value,
  };
  if ($("#provider-override").value) body.provider = $("#provider-override").value;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || `Chat failed (${response.status})`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    while (true) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = pending.split("\n");
      pending = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        const item = JSON.parse(line);
        if (item.type === "progress") $("#progress").textContent = item.message;
        if (item.type === "delta") assistant.textContent += item.text;
        if (item.type === "tool_result") addToolResult(item.result, item.provider);
        if (item.type === "complete") state.conversationId = item.conversation_id;
        if (item.type === "cancelled") $("#progress").textContent = "Cancelled";
        if (item.type === "error") throw new Error(item.error);
      }
      if (done) break;
    }
    if (!assistant.textContent) assistant.textContent = "Tool request completed.";
    $("#progress").textContent = "";
    await loadActivity();
  } catch (error) {
    assistant.textContent = `I couldn’t complete that request: ${error.message}`;
    $("#progress").textContent = "";
  } finally {
    state.requestId = null;
    $("#cancel-chat").classList.add("hidden");
    $("#send-chat").disabled = false;
    input.focus();
  }
}

async function cancelChat() {
  if (!state.requestId) return;
  await api("/api/chat/cancel", {
    method: "POST",
    body: JSON.stringify({ request_id: state.requestId }),
  });
}

async function loadActivity() {
  try {
    const data = await api("/api/activity?limit=35");
    const nodes = data.events.map((event) => {
      const item = document.createElement("div");
      item.className = "activity-item";
      const title = document.createElement("strong");
      title.textContent = event.event.replaceAll("_", " ");
      const detail = document.createElement("p");
      const provider = event.provider || event.selected_provider || event.tool || "local";
      const status = event.status || event.classification || "";
      detail.textContent = `${provider} ${status} · ${new Date(event.timestamp).toLocaleTimeString()}`;
      item.append(title, detail);
      return item;
    });
    $("#activity").replaceChildren(...(nodes.length ? nodes : [emptyNode("No activity yet") ]));
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
    $("#memory-results").replaceChildren(...(nodes.length ? nodes : [emptyNode("No matches") ]));
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
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $("#activity-view").classList.toggle("hidden", name !== "activity");
  $("#memory-view").classList.toggle("hidden", name !== "memory");
}

$("#chat-form").addEventListener("submit", sendChat);
$("#cancel-chat").addEventListener("click", cancelChat);
$("#memory-form").addEventListener("submit", searchMemory);
$("#refresh-status").addEventListener("click", loadStatus);
$("#enable-developer").addEventListener("click", () => developerAction("enable"));
$("#disable-developer").addEventListener("click", () => developerAction("disable"));
$("#run-developer").addEventListener("click", runDeveloperCommand);
$("#new-chat").addEventListener("click", () => {
  state.conversationId = null;
  $("#messages").replaceChildren();
  addMessage("assistant", "New local conversation started.");
});
$("#settings-toggle").addEventListener("click", () => {
  const panel = $("#access-panel");
  panel.classList.toggle("hidden");
  $("#settings-toggle").setAttribute("aria-expanded", String(!panel.classList.contains("hidden")));
});
$("#save-token").addEventListener("click", () => {
  state.token = $("#access-token").value.trim();
  if (state.token) sessionStorage.setItem("miso-dashboard-token", state.token);
  else sessionStorage.removeItem("miso-dashboard-token");
  $("#access-panel").classList.add("hidden");
  loadStatus();
  loadActivity();
});
$$('.tab').forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));

$("#access-token").value = state.token;
loadStatus();
loadActivity();
setInterval(loadStatus, 15000);
