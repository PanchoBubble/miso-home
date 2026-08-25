const state = {
  token: localStorage.getItem("miso-dashboard-token") || "",
  conversationId: null,
  requestId: null,
  developerEnabled: false,
  connected: false,
  panelReturnFocus: null,
  installPrompt: null,
  reconnectTimer: null,
  reconnectDelay: 3000,
  statusInFlight: false,
  memoryRecords: new Map(),
  selectedMemory: new Set(),
  memoryLoaded: false,
  memoryTag: "",
  householdLoaded: false,
  householdData: null,
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
  const showLanFallback = !connected
    && detail !== "Access needed"
    && window.location.hostname !== "miso.local";
  $("#lan-fallback").classList.toggle("hidden", !showLanFallback);
}

function scheduleReconnect() {
  if (state.reconnectTimer || !navigator.onLine) return;
  const delay = state.reconnectDelay;
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
    loadStatus();
  }, delay);
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
  if (state.statusInFlight) return;
  state.statusInFlight = true;
  try {
    const data = await api("/api/status");
    setConnected(true, `Online · ${data.service.architecture}`);
    state.reconnectDelay = 3000;
    if (state.reconnectTimer) window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    renderProviders(data.providers);
    renderDeveloper(data.developer_mode);
  } catch (error) {
    setConnected(false, error.message === "unauthorized" ? "Access needed" : "Offline");
    scheduleReconnect();
  } finally {
    state.statusInFlight = false;
  }
}

async function installApp() {
  if (!state.installPrompt) return;
  state.installPrompt.prompt();
  await state.installPrompt.userChoice;
  state.installPrompt = null;
  $("#install-app").classList.add("hidden");
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("/service-worker.js", { scope: "/" })
    .then((registration) => {
      if (registration.waiting) registration.waiting.postMessage("skip-waiting");
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (worker.state === "installed" && navigator.serviceWorker.controller) {
            worker.postMessage("skip-waiting");
          }
        });
      });
    })
    .catch(() => {});
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

function householdActorLabel(actorId) {
  if (actorId === "household:voice") return "Added by voice";
  if (state.householdData?.actor?.id === actorId) return "Added by you";
  return `Added by ${actorId}`;
}

function visibilityLabel(sharedOrVisibility) {
  return sharedOrVisibility === true || sharedOrVisibility === "shared"
    ? "Household"
    : "Private";
}

function localDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function actionButton(label, action, data = {}, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `row-action${danger ? " danger" : ""}`;
  button.dataset.householdAction = action;
  Object.entries(data).forEach(([key, value]) => {
    button.dataset[key] = String(value);
  });
  button.textContent = label;
  return button;
}

function renderShoppingLists(lists) {
  const activeCount = lists.reduce(
    (total, list) => total + list.items.filter((item) => !item.completed).length,
    0,
  );
  $("#shopping-count").textContent = `${activeCount} item${activeCount === 1 ? "" : "s"}`;
  const listNodes = lists.map((list) => {
    const section = document.createElement("section");
    section.className = "shopping-list";
    const header = document.createElement("div");
    header.className = "shopping-list-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "shopping-list-title";
    const title = document.createElement("strong");
    title.textContent = list.name;
    const scope = document.createElement("span");
    scope.className = "visibility-chip";
    scope.textContent = visibilityLabel(list.shared);
    titleWrap.append(title, scope);
    const count = document.createElement("span");
    count.className = "count-badge";
    count.textContent = `${list.items.length}`;
    header.append(titleWrap, count);

    const itemNodes = list.items.map((item) => {
      const row = document.createElement("div");
      row.className = `household-row${item.completed ? " completed" : ""}`;
      const checked = document.createElement("input");
      checked.type = "checkbox";
      checked.checked = item.completed;
      checked.className = "item-check";
      checked.dataset.householdAction = "shopping_toggle";
      checked.dataset.id = item.id;
      checked.dataset.revision = item.revision;
      checked.setAttribute("aria-label", `${item.completed ? "Restore" : "Complete"} ${item.name}`);
      const copy = document.createElement("div");
      copy.className = "household-row-copy";
      const name = document.createElement("strong");
      name.textContent = item.quantity > 1 ? `${item.quantity} × ${item.name}` : item.name;
      const meta = document.createElement("small");
      meta.textContent = `${householdActorLabel(item.added_by || item.actor_id)} · ${localDateTime(item.created_at)}`;
      copy.append(name, meta);
      const actorChip = document.createElement("span");
      actorChip.className = `actor-chip${item.added_by === "household:voice" ? " voice" : ""}`;
      actorChip.textContent = item.added_by === "household:voice" ? "Voice" : "Web";
      const actions = document.createElement("div");
      actions.className = "row-actions";
      actions.append(
        actionButton("Edit", "shopping_edit", {
          id: item.id,
          revision: item.revision,
          name: item.name,
          quantity: item.quantity,
        }),
        actionButton("Remove", "shopping_remove", {
          id: item.id,
          revision: item.revision,
        }, true),
      );
      row.append(checked, copy, actorChip, actions);
      return row;
    });
    section.append(header, ...(itemNodes.length ? itemNodes : [emptyNode("Nothing on this list")]));
    return section;
  });
  $("#shopping-lists").replaceChildren(
    ...(listNodes.length ? listNodes : [emptyNode("No lists yet. Add the first item above.")]),
  );
}

function renderSchedule(data) {
  const pending = [...data.reminders, ...data.timers]
    .filter((item) => item.status === "pending")
    .sort((left, right) => left.due_at.localeCompare(right.due_at));
  const recentPast = [...data.reminders, ...data.timers]
    .filter((item) => item.status !== "pending")
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 4);
  $("#schedule-count").textContent = `${pending.length} active`;
  const nodes = [...pending, ...recentPast].map((item) => {
    const row = document.createElement("div");
    row.className = `household-row schedule-item${item.status === "pending" ? "" : " past"}`;
    const icon = document.createElement("span");
    icon.className = "schedule-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = item.kind === "timer" ? "◷" : "◇";
    const copy = document.createElement("div");
    copy.className = "household-row-copy";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("small");
    meta.textContent = [
      item.status === "pending" ? localDateTime(item.due_at) : item.status,
      visibilityLabel(item.visibility),
      householdActorLabel(item.created_by),
    ].join(" · ");
    copy.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (item.status === "pending") {
      actions.append(
        actionButton("Edit", "schedule_edit", {
          id: item.id,
          kind: item.kind,
          title: item.title,
          dueAt: item.due_at,
          revision: item.revision,
        }),
        actionButton("Cancel", "schedule_cancel", {
          id: item.id,
          kind: item.kind,
          revision: item.revision,
        }, true),
      );
    }
    row.append(icon, copy, actions);
    return row;
  });
  $("#schedule-items").replaceChildren(
    ...(nodes.length ? nodes : [emptyNode("No reminders or timers yet")]),
  );
}

function renderHouseholdMessages(messages) {
  $("#message-count").textContent = `${messages.length} note${messages.length === 1 ? "" : "s"}`;
  const nodes = messages.map((message) => {
    const article = document.createElement("article");
    article.className = "household-message";
    const content = document.createElement("p");
    content.textContent = message.content;
    const footer = document.createElement("div");
    footer.className = "message-meta-row";
    const meta = document.createElement("span");
    meta.textContent = `${visibilityLabel(message.visibility)} · ${householdActorLabel(message.created_by)} · ${localDateTime(message.created_at)}`;
    const remove = actionButton("Remove", "message_delete", {
      recordId: message.record_id,
    }, true);
    footer.append(meta, remove);
    article.append(content, footer);
    return article;
  });
  $("#household-messages").replaceChildren(
    ...(nodes.length ? nodes : [emptyNode("No household notes yet")]),
  );
}

function showHouseholdNotice(message) {
  const notice = $("#household-notice");
  notice.textContent = message;
  notice.classList.toggle("hidden", !message);
}

async function loadHousehold() {
  $("#refresh-household").disabled = true;
  try {
    const data = await api("/api/household");
    state.householdData = data;
    state.householdLoaded = true;
    renderShoppingLists(data.lists);
    renderSchedule(data);
    renderHouseholdMessages(data.messages);
    $("#household-updated").textContent = `Updated ${new Date(data.refreshed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    showHouseholdNotice("");
  } catch (error) {
    showHouseholdNotice(`Could not load household: ${error.message}`);
  } finally {
    $("#refresh-household").disabled = false;
  }
}

async function submitHouseholdAction(payload, submitter = null) {
  if (submitter) submitter.disabled = true;
  try {
    await api("/api/household", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadHousehold();
    return true;
  } catch (error) {
    const message = error.message === "revision_conflict"
      ? "Someone changed that item first. The latest version has been loaded."
      : error.message;
    showHouseholdNotice(message);
    if (error.message === "revision_conflict") await loadHousehold();
    return false;
  } finally {
    if (submitter) submitter.disabled = false;
  }
}

function setAppView(name) {
  const household = name === "household";
  $("#household-view").classList.toggle("hidden", !household);
  $("#chat-view").classList.toggle("hidden", household);
  $$('[data-app-view]').forEach((button) => {
    const active = button.dataset.appView === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (household && !state.householdLoaded) loadHousehold();
  if (!household) requestAnimationFrame(() => $("#chat-input").focus());
}

function memoryKey(record) {
  return record.record_type + ":" + record.record_id;
}

function memoryKindLabel(record) {
  if (record.record_type === "event") {
    return record.role ? record.role + " transcript" : "Transcript";
  }
  if (record.kind === "inferred" && record.importance >= 0.9) {
    return "Inferred · important";
  }
  return record.kind;
}

function renderMemoryResults(results, summary = "") {
  state.memoryRecords = new Map(results.map((record) => [memoryKey(record), record]));
  state.selectedMemory.clear();
  $("#delete-memory").disabled = true;
  $("#delete-memory").textContent = "Delete selected";
  $("#memory-count").textContent = summary
    || results.length + " record" + (results.length === 1 ? "" : "s");
  const nodes = results.map((result) => {
    const item = document.createElement("article");
    item.className = "memory-item";

    const select = document.createElement("input");
    select.type = "checkbox";
    select.className = "memory-select";
    select.setAttribute("aria-label", "Select " + memoryKindLabel(result));
    select.dataset.memoryKey = memoryKey(result);

    const copy = document.createElement("div");
    copy.className = "memory-copy";
    const top = document.createElement("div");
    top.className = "memory-top";
    const title = document.createElement("strong");
    title.textContent = memoryKindLabel(result);
    top.append(title);

    if (result.record_type === "memory") {
      const important = document.createElement("button");
      important.type = "button";
      important.className = "important-button"
        + (result.importance >= 0.9 ? " active" : "");
      important.dataset.importantKey = memoryKey(result);
      important.textContent = result.importance >= 0.9
        ? "★ Important"
        : "☆ Mark important";
      top.append(important);
    }

    const content = document.createElement("p");
    content.textContent = result.content || "(no text recorded)";
    const meta = document.createElement("span");
    meta.className = "memory-meta";
    const importance = result.importance == null
      ? ""
      : " · importance " + Math.round(result.importance * 100) + "%";
    meta.textContent = new Date(result.created_at).toLocaleString()
      + " · " + result.visibility + " · " + result.created_by + importance;
    copy.append(top, content, meta);

    if (result.tags.length) {
      const tags = document.createElement("div");
      tags.className = "memory-tags";
      result.tags.forEach((name) => {
        const tag = document.createElement("button");
        tag.type = "button";
        tag.className = "memory-tag";
        tag.dataset.memoryTag = name;
        tag.textContent = name;
        tags.append(tag);
      });
      copy.append(tags);
    }

    if (result.sources.length) {
      const source = document.createElement("details");
      source.className = "memory-source";
      const sourceTitle = document.createElement("summary");
      sourceTitle.textContent = "Provenance · " + result.sources.length
        + " source" + (result.sources.length === 1 ? "" : "s");
      source.append(sourceTitle);
      result.sources.forEach((value) => {
        const line = document.createElement("p");
        const detail = value.content ? " — " + value.content : "";
        line.textContent = value.source_type + " · " + value.source_id + detail;
        source.append(line);
      });
      copy.append(source);
    }

    item.append(select, copy);
    return item;
  });
  $("#memory-results").replaceChildren(
    ...(nodes.length ? nodes : [emptyNode("No matches")]),
  );
}

async function searchMemory(event) {
  if (event) {
    event.preventDefault();
    state.memoryTag = "";
  }
  const query = $("#memory-query").value.trim();
  $("#memory-results").replaceChildren(emptyNode("Searching local memory…"));
  try {
    const kind = $("#memory-kind").value;
    const parameters = new URLSearchParams({ q: query });
    if (kind === "transcript") parameters.set("record_type", "event");
    else if (kind) parameters.set("kind", kind);
    if (state.memoryTag) parameters.set("tag", state.memoryTag);
    const data = await api("/api/memory?" + parameters);
    const summary = state.memoryTag
      ? data.results.length + " tagged “" + state.memoryTag + "”"
      : "";
    renderMemoryResults(data.results, summary);
    state.memoryLoaded = true;
  } catch (error) {
    $("#memory-results").replaceChildren(emptyNode(error.message));
  }
}

function parsedTags(value) {
  return [...new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean))]
    .slice(0, 20);
}

async function rememberMemory(event) {
  event.preventDefault();
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    await api("/api/memory", {
      method: "POST",
      body: JSON.stringify({
        action: "remember",
        content: $("#remember-content").value,
        tags: parsedTags($("#remember-tags").value),
        visibility: $("#remember-visibility").value,
      }),
    });
    event.currentTarget.reset();
    state.memoryTag = "";
    $("#memory-query").value = "";
    $("#memory-kind").value = "";
    await searchMemory();
  } catch (error) {
    $("#memory-count").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
}

async function toggleImportant(key) {
  const record = state.memoryRecords.get(key);
  if (!record || record.record_type !== "memory") return;
  const active = record.importance >= 0.9;
  const tags = active
    ? record.tags.filter((tag) => tag !== "important")
    : [...new Set([...record.tags, "important"])];
  try {
    await api("/api/memory", {
      method: "POST",
      body: JSON.stringify({
        action: "update",
        record_id: record.record_id,
        importance: active ? 0.5 : 1,
        tags,
      }),
    });
    await searchMemory();
  } catch (error) {
    $("#memory-count").textContent = error.message;
  }
}

function updateMemorySelection(key, selected) {
  if (selected) state.selectedMemory.add(key);
  else state.selectedMemory.delete(key);
  const count = state.selectedMemory.size;
  $("#delete-memory").disabled = count === 0;
  $("#delete-memory").textContent = count
    ? "Delete selected (" + count + ")"
    : "Delete selected";
}

async function previewPruning(event) {
  event.preventDefault();
  const rawDays = $("#prune-days").value;
  const topic = $("#prune-topic").value.trim();
  if (!rawDays && !topic) {
    $("#memory-count").textContent = "Enter an age or topic to preview";
    return;
  }
  $("#memory-results").replaceChildren(emptyNode("Building a deletion preview…"));
  try {
    const data = await api("/api/memory", {
      method: "POST",
      body: JSON.stringify({
        action: "preview_prune",
        older_than_days: rawDays ? Number(rawDays) : null,
        topic,
      }),
    });
    const impact = data.impact;
    renderMemoryResults(
      data.candidates,
      "Preview: " + impact.records + " records, "
        + impact.derived_memories + " derived, "
        + impact.embeddings + " embeddings",
    );
  } catch (error) {
    $("#memory-results").replaceChildren(emptyNode(error.message));
  }
}

async function deleteSelectedMemory() {
  const records = [...state.selectedMemory]
    .map((key) => state.memoryRecords.get(key));
  if (!records.length) return;
  const suffix = records.length === 1 ? "" : "s";
  if (!window.confirm(
    "Permanently delete " + records.length + " selected record" + suffix
      + " and its derived data?",
  )) return;
  try {
    const data = await api("/api/memory", {
      method: "POST",
      body: JSON.stringify({
        action: "delete",
        records: records.map(({ record_type, record_id }) => ({
          record_type,
          record_id,
        })),
      }),
    });
    const deleted = data.deleted;
    await searchMemory();
    $("#memory-count").textContent = "Deleted " + deleted.records
      + " selected and " + deleted.derived_memories + " derived records";
  } catch (error) {
    $("#memory-count").textContent = error.message;
  }
}

async function exportMemory() {
  try {
    const data = await api("/api/memory/export");
    const blob = new Blob(
      [JSON.stringify(data, null, 2)],
      { type: "application/json" },
    );
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "miso-memory-" + new Date().toISOString().slice(0, 10) + ".json";
    link.click();
    URL.revokeObjectURL(link.href);
    $("#memory-count").textContent = "Exported " + data.records.length + " records";
  } catch (error) {
    $("#memory-count").textContent = error.message;
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
  if (name === "memory") {
    if (!state.memoryLoaded) searchMemory();
    requestAnimationFrame(() => $("#memory-query").focus());
  }
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

$("#shopping-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const completed = await submitHouseholdAction({
    action: "shopping_add",
    name: $("#shopping-name").value.trim(),
    quantity: Number($("#shopping-quantity").value),
    list_name: $("#shopping-list-name").value.trim(),
    shared: $("#shopping-shared").checked,
  }, event.submitter);
  if (completed) {
    $("#shopping-name").value = "";
    $("#shopping-quantity").value = "1";
    $("#shopping-name").focus();
  }
});

$("#reminder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const due = new Date($("#reminder-due").value);
  const completed = await submitHouseholdAction({
    action: "reminder_create",
    title: $("#reminder-title").value.trim(),
    due_at: due.toISOString(),
    visibility: $("#reminder-shared").checked ? "shared" : "private",
  }, event.submitter);
  if (completed) event.currentTarget.reset();
});

$("#timer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const completed = await submitHouseholdAction({
    action: "timer_create",
    title: $("#timer-title").value.trim(),
    duration_seconds: Number($("#timer-minutes").value) * 60,
    visibility: $("#timer-shared").checked ? "shared" : "private",
  }, event.submitter);
  if (completed) {
    $("#timer-title").value = "Timer";
    $("#timer-minutes").value = "5";
  }
});

$("#message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const completed = await submitHouseholdAction({
    action: "message_create",
    content: $("#message-content").value.trim(),
    visibility: $("#message-shared").checked ? "shared" : "private",
  }, event.submitter);
  if (completed) $("#message-content").value = "";
});

$("#shopping-lists").addEventListener("change", async (event) => {
  const input = event.target.closest('[data-household-action="shopping_toggle"]');
  if (!input) return;
  input.disabled = true;
  await submitHouseholdAction({
    action: "shopping_update",
    id: input.dataset.id,
    expected_revision: Number(input.dataset.revision),
    completed: input.checked,
  });
});

async function handleHouseholdClick(event) {
  const button = event.target.closest("[data-household-action]");
  if (!button || button.matches("input")) return;
  const action = button.dataset.householdAction;
  if (action === "shopping_edit") {
    const name = window.prompt("Item name", button.dataset.name);
    if (name === null || !name.trim()) return;
    const quantity = window.prompt("Quantity", button.dataset.quantity);
    if (quantity === null) return;
    await submitHouseholdAction({
      action: "shopping_update",
      id: button.dataset.id,
      expected_revision: Number(button.dataset.revision),
      name: name.trim(),
      quantity: Number(quantity),
    }, button);
    return;
  }
  if (action === "shopping_remove") {
    await submitHouseholdAction({
      action,
      id: button.dataset.id,
      expected_revision: Number(button.dataset.revision),
    }, button);
    return;
  }
  if (action === "schedule_cancel") {
    await submitHouseholdAction({
      action: `${button.dataset.kind}_cancel`,
      id: button.dataset.id,
      expected_revision: Number(button.dataset.revision),
    }, button);
    return;
  }
  if (action === "schedule_edit") {
    const title = window.prompt("Title", button.dataset.title);
    if (title === null || !title.trim()) return;
    const payload = {
      action: `${button.dataset.kind}_update`,
      id: button.dataset.id,
      expected_revision: Number(button.dataset.revision),
      title: title.trim(),
    };
    if (button.dataset.kind === "timer") {
      const minutes = window.prompt("Restart for how many minutes?", "5");
      if (minutes === null) return;
      payload.duration_seconds = Number(minutes) * 60;
    } else {
      const current = new Date(button.dataset.dueAt);
      const initial = Number.isNaN(current.getTime())
        ? button.dataset.dueAt
        : new Date(current.getTime() - current.getTimezoneOffset() * 60000)
          .toISOString().slice(0, 16);
      const dueAt = window.prompt("Due date and time", initial);
      if (dueAt === null) return;
      const dueDate = new Date(dueAt);
      if (Number.isNaN(dueDate.getTime())) {
        showHouseholdNotice("Enter a valid reminder date and time.");
        return;
      }
      payload.due_at = dueDate.toISOString();
    }
    await submitHouseholdAction(payload, button);
    return;
  }
  if (action === "message_delete") {
    if (!window.confirm("Remove this household note?")) return;
    await submitHouseholdAction({
      action,
      record_id: Number(button.dataset.recordId),
    }, button);
  }
}

$("#shopping-lists").addEventListener("click", handleHouseholdClick);
$("#schedule-items").addEventListener("click", handleHouseholdClick);
$("#household-messages").addEventListener("click", handleHouseholdClick);
$("#refresh-household").addEventListener("click", loadHousehold);
$$('[data-app-view]').forEach((button) => {
  button.addEventListener("click", () => setAppView(button.dataset.appView));
});

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
$("#remember-form").addEventListener("submit", rememberMemory);
$("#prune-form").addEventListener("submit", previewPruning);
$("#delete-memory").addEventListener("click", deleteSelectedMemory);
$("#export-memory").addEventListener("click", exportMemory);
$("#memory-results").addEventListener("change", (event) => {
  if (event.target.matches("[data-memory-key]")) {
    updateMemorySelection(event.target.dataset.memoryKey, event.target.checked);
  }
});
$("#memory-results").addEventListener("click", (event) => {
  const button = event.target.closest("[data-important-key]");
  if (button) {
    toggleImportant(button.dataset.importantKey);
    return;
  }
  const tag = event.target.closest("[data-memory-tag]");
  if (tag) {
    state.memoryTag = tag.dataset.memoryTag;
    $("#memory-query").value = "";
    $("#memory-kind").value = "";
    searchMemory();
  }
});
$("#refresh-status").addEventListener("click", loadStatus);
$("#refresh-activity").addEventListener("click", loadActivity);
$("#service-status").addEventListener("click", loadStatus);
$("#retry-connection").addEventListener("click", loadStatus);
$("#install-app").addEventListener("click", installApp);
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
  if (state.token) localStorage.setItem("miso-dashboard-token", state.token);
  else localStorage.removeItem("miso-dashboard-token");
  loadStatus();
  loadActivity();
  loadHousehold();
});
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  state.installPrompt = event;
  $("#install-app").classList.remove("hidden");
});
window.addEventListener("appinstalled", () => {
  state.installPrompt = null;
  $("#install-app").classList.add("hidden");
});
window.addEventListener("online", () => {
  state.reconnectDelay = 3000;
  loadStatus();
});
window.addEventListener("offline", () => setConnected(false, "Offline"));

$("#access-token").value = state.token;
updateRouteChip();
resizeComposer();
loadStatus();
loadActivity();
loadHousehold();
registerServiceWorker();
setInterval(() => {
  if (state.connected) {
    loadStatus();
    if (!$("#household-view").classList.contains("hidden")) loadHousehold();
  }
}, 15000);
