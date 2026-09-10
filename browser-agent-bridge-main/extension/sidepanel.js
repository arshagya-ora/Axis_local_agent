import { AxisApi } from "./ui/api.js";
import { reconcilePending } from "./ui/pending.js";
import { mountAttachments, attachmentRefs, sameAttachments, clearSentAttachments } from "./ui/attachments/composer.js";
import {
  createState,
  applySnapshot,
  applyEvent,
  persistentState,
  rememberView,
  ownsRuntime,
  mergeTask,
} from "./ui/state.js";
import { el, icon, button, decorate, feedback } from "./ui/dom.js";
import { renderChat, duration } from "./ui/chat.js";
import { mountHistory } from "./ui/history.js";
import { applyAppearance, mountSettings } from "./ui/settings.js";
import { mountApprovals } from "./ui/approvals.js";
import { renderExecutionApproval } from "./ui/execution.js";

const $ = (selector) => document.querySelector(selector),
  api = new AxisApi(),
  state = createState();
const workspace = $("#workspace"),
  scroller = $("#conversation"),
  input = $("#instruction"),
  mode = $("#message-mode"),
  timeline = $("#timeline");
let bridge = { state: "disconnected" },
  settings = null,
  sending = false,
  selectionVersion = 0,
  streamController = null,
  reconnectTimer = null,
  saveTimer = null,
  renderTimer = null,
  pendingActivity = false,
  loadingSnapshot = false,
  bufferedEvents = [];
decorate();
await applyAppearance();
await api.load();
// Pairing credentials and drafts are available only to trusted extension contexts.
await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
const saved =
  (await chrome.storage.local.get("axisWorkspace")).axisWorkspace || {};
for (const key of [
  "selected",
  "drafts",
  "attachmentDrafts",
  "views",
  "expanded",
  "pending",
  "extensions",
  "executionMode",
])
  if (saved[key] !== undefined) state[key] = saved[key];
state.selected ||= crypto.randomUUID();
input.value = state.drafts[state.selected] || "";
const view = state.views[state.selected];
if (view) {
  state.mode = view.mode;
  state.target = view.target;
  state.context = view.context;
}
mode.value = state.mode;
const attachmentComposer = mountAttachments($("#attachment-composer"), api, state, { save, error, changed: renderComposer });
function error(text) {
  feedback($("#workspace-error"), text, true);
}
function nearBottom() {
  return (
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 64
  );
}
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (!workspace.hidden) rememberView(state, scroller.scrollTop);
    chrome.storage.local
      .set({ axisWorkspace: persistentState(state) })
      .catch(() =>
        error(
          "The local draft could not be saved. Keep the panel open until the instruction is sent.",
        ),
      );
  }, 180);
}
function draft() {
  state.drafts[state.selected] = input.value;
  save();
  input.style.height = "auto";
  input.style.height = `${Math.min(160, input.scrollHeight)}px`;
}
input.addEventListener("input", draft);
scroller.addEventListener("scroll", () => {
  if (nearBottom()) $("#new-activity").hidden = true;
  save();
});
function draw(receivedActivity = false) {
  const follow =
      !workspace.hidden && !$("#history-drawer").open && nearBottom(),
    oldTop = scroller.scrollTop;
  renderChat(timeline, state, actions);
  $("#conversation-title").textContent =
    state.conversation?.title || "New conversation";
  if (follow) {
    scroller.scrollTop = scroller.scrollHeight;
    $("#new-activity").hidden = true;
  } else {
    scroller.scrollTop = oldTop;
    if (!workspace.hidden && nearBottom()) $("#new-activity").hidden = true;
    else if (receivedActivity) $("#new-activity").hidden = false;
  }
  renderConnection();
  renderComposer();
}
function scheduleDraw(receivedActivity = false) {
  pendingActivity ||= receivedActivity;
  if (renderTimer) return;
  renderTimer = setTimeout(() => {
    renderTimer = null;
    draw(pendingActivity);
    pendingActivity = false;
  }, 100);
}
timeline.addEventListener("focusout", () => scheduleDraw());
function renderConnection() {
  renderExecutionApproval($("#execution-approval"), state.active, api, error);
  const connected = state.connected && bridge.state === "connected",
    label = $("#connection-button");
  label.textContent = !api.token || state.pairingRequired
    ? "Setup required"
    : !state.connected
    ? "Service offline"
    : state.stream === "reconnecting"
      ? "Reconnecting"
      : bridge.state !== "connected"
        ? "Bridge disconnected"
        : "Connected";
  label.classList.toggle("ready", connected && state.stream !== "reconnecting");
  $("#connection-details").textContent =
    `AXIS service: ${state.connected ? "connected" : "offline or not paired"}\nBrowser bridge: ${bridge.state}\nActivity stream: ${state.stream}${bridge.error ? "\n" + bridge.error : ""}`;
  const context =
    state.active?.conversation_id === state.selected
      ? state.active.browser_context
      : Object.values(state.tasks).at(-1)?.browser_context;
  $("#browser-context").replaceChildren(
    icon("browser"),
    el("span", "", context?.title || "No controlled tab yet"),
  );
  $("#return-active").hidden =
    !state.active || state.active.conversation_id === state.selected;
}
function renderComposer() {
  const live = state.active?.conversation_id === state.selected ? state.active : null;
  $("#execution-mode").value = live?.execution_mode || state.executionMode || "approval";
  $("#pending-instruction").hidden = !state.pending;
  $("#pending-preview").textContent = state.pending
    ? state.pending.body.text.slice(0, 180)
    : "";
  $("#retry-instruction").disabled = sending || !state.connected;
  const current =
    state.active?.conversation_id === state.selected ? state.active : null;
  if (state.mode === "follow_up" && (!current || state.target !== current.id)) {
    state.mode = "new_task";
    state.target = null;
    mode.value = "new_task";
  }
  const target =
      state.mode === "follow_up"
        ? current
        : state.context
          ? state.tasks[state.context]
          : null,
    chip = $("#context-chip");
  chip.hidden = !target;
  if (target) {
    chip.replaceChildren(
      el(
        "span",
        "",
        `${state.mode === "follow_up" ? "Follow-up" : "Saved outcome"} · ${target.title}`,
      ),
      button(
        "Clear task context",
        () => {
          state.context = null;
          state.target = null;
          state.mode = "new_task";
          mode.value = state.mode;
          save();
          renderComposer();
        },
        "close",
        "icon-button",
      ),
    );
  }
  const hint = $("#composer-hint");
  hint.textContent = !api.token || state.pairingRequired
    ? "Pair the local AXIS service in Settings to get started."
    : !state.connected
    ? "Connect AXIS in Settings to start a browser task."
    : state.mode === "follow_up"
      ? "Instructions apply at the next safe step."
      : state.context
        ? "A bounded saved outcome will accompany this new task."
        : "Enter to send · Shift+Enter for a new line";
  if (state.connected && current && state.mode === "new_task")
    hint.textContent =
      "A task owns this browser. Use Current task to send a follow-up.";
  if (state.connected && current?.status === "limit_reached")
    hint.textContent =
      "Extend the task’s budget to resume, or Stop to release this browser.";
  const unavailable = !state.connected || !api.token || state.pairingRequired;
  $("#send").disabled = sending || attachmentComposer.uploading || unavailable;
  attachmentComposer.render(sending || Boolean(state.pending) || unavailable);
  input.readOnly = sending;
}
function openSettings() {
  rememberView(state, scroller.scrollTop);
  save();
  workspace.hidden = true;
  $("#settings-view").hidden = false;
  if (!settings)
    settings = mountSettings($("#settings-view"), api, {
      onBack: closeSettings,
      onPaired: connect,
    });
  else settings.refresh();
  positionApprovals();
  $("#settings-view").querySelector("button").focus();
}
function closeSettings() {
  workspace.hidden = false;
  $("#settings-view").hidden = true;
  positionApprovals();
  scroller.scrollTop = state.views[state.selected]?.scroll || 0;
  $("#settings-button").focus();
}
function positionApprovals() {
  const region = $("#approval-region");
  if ($("#history-drawer").open) $("#history-drawer .toolbar").after(region);
  else if (!$("#settings-view").hidden)
    $("#settings-view .toolbar").after(region);
  else $("#return-active").after(region);
  region.after($("#execution-approval"));
}
async function selectConversation(id, { fresh = false } = {}) {
  rememberView(state, scroller.scrollTop);
  state.drafts[state.selected] = input.value;
  const version = ++selectionVersion;
  loadingSnapshot = true;
  bufferedEvents = [];
  try {
    const snapshot = fresh
      ? null
      : await api.request(`/api/conversations/${id}`);
    if (version !== selectionVersion) return;
    state.readingEarlier = false;
    if (snapshot) applySnapshot(state, snapshot);
    else {
      state.selected = id;
      state.conversation = null;
      state.messages = [];
      state.tasks = {};
      state.activity = {};
      state.hasMore = false;
    }
    const remembered = state.views[id];
    state.mode =
      remembered?.mode ||
      (state.active?.conversation_id === id ? "follow_up" : "new_task");
    state.target =
      remembered?.target ||
      (state.active?.conversation_id === id ? state.active.id : null);
    state.context = remembered?.context || null;
    mode.value = state.mode;
    input.value = state.drafts[id] || "";
    error("");
    // Events received while fetching a snapshot are replayed only if newer than
    // that snapshot. The global cursor continues to cover other conversations.
    for (const event of bufferedEvents)
      if (!snapshot || event.sequence > snapshot.cursor) {
        const cursor = state.cursor;
        state.cursor = event.sequence - 1;
        applyEvent(state, event);
        state.cursor = Math.max(cursor, state.cursor);
      }
    draw();
    scroller.scrollTop = remembered?.scroll ?? scroller.scrollHeight;
    draft();
  } catch (cause) {
    error(cause.message);
  } finally {
    if (version === selectionVersion) {
      loadingSnapshot = false;
      bufferedEvents = [];
    }
  }
}
function newConversation() {
  selectConversation(crypto.randomUUID(), { fresh: true });
}
const history = mountHistory({
  dialog: $("#history-drawer"),
  list: $("#history-list"),
  search: $("#history-search"),
  api,
  state,
  onSelect: selectConversation,
  onNew: newConversation,
  onDelete: (id) => {
    delete state.drafts[id];
    delete state.views[id];
    if (id === state.selected) newConversation();
    save();
  },
});
const actions = {
  settings: openSettings,
  expand(id, value) {
    state.expanded[id] = value;
    draw();
    save();
  },
  async control(task, action) {
    try {
      const updated = await api.request(`/api/tasks/${task.id}/control`, {
        method: "POST",
        body: { action },
      });
      mergeTask(state, updated);
      error("");
      draw();
    } catch (cause) {
      error(cause.message);
    }
  },
  async extend(task, amounts) {
    state.extensions ||= {};
    let body = state.extensions[task.id];
    if (
      body &&
      ["requests", "steps", "actions"].some((key) => body[key] !== amounts[key])
    ) {
      error(
        "Retry the previous budget amounts first so an uncertain response cannot grant extra budget twice.",
      );
      return false;
    }
    body ||= {
      action: "extend",
      ...amounts,
      client_request_id: crypto.randomUUID(),
    };
    state.extensions[task.id] = body;
    await chrome.storage.local.set({ axisWorkspace: persistentState(state) });
    try {
      const updated = await api.request(`/api/tasks/${task.id}/control`, {
        method: "POST",
        body,
      });
      delete state.extensions[task.id];
      mergeTask(state, updated);
      error("");
      draw();
      save();
      return true;
    } catch (cause) {
      if (cause.code && cause.code !== "unavailable")
        delete state.extensions[task.id];
      error(cause.message);
      save();
      return false;
    }
  },
  async earlierActivity(task) {
    try {
      const page = state.activity[task.id],
        before = page?.items[0]?.sequence;
      if (!before) return;
      state.activity[task.id] = {
        ...(await api.request(`/api/tasks/${task.id}/events?before=${before}`)),
        older: true,
      };
      draw();
    } catch (cause) {
      error(cause.message);
    }
  },
  async latestActivity(task) {
    try {
      state.activity[task.id] = await api.request(
        `/api/tasks/${task.id}/events`,
      );
      draw();
    } catch (cause) {
      error(cause.message);
    }
  },
  async earlierMessages() {
    const before = state.messages[0]?.ordinal;
    if (!before) return;
    try {
      const snapshot = await api.request(
        `/api/conversations/${state.selected}?before=${before}`,
      );
      applySnapshot(state, snapshot);
      state.readingEarlier = true;
      draw();
      scroller.scrollTop = 0;
      $("#new-activity").hidden = false;
      $("#new-activity").textContent = "↓ Recent messages";
    } catch (cause) {
      error(cause.message);
    }
  },
  savedContext(task) {
    state.mode = "new_task";
    state.target = null;
    state.context = task.id;
    mode.value = state.mode;
    renderComposer();
    save();
    input.focus();
  },
};
$("#history-button").addEventListener("click", () => {
  history.open();
  positionApprovals();
});
$("#history-drawer").addEventListener("close", positionApprovals);
$("#settings-button").addEventListener("click", openSettings);
$("#new-conversation").addEventListener("click", newConversation);
$("#return-active").addEventListener(
  "click",
  () => state.active && selectConversation(state.active.conversation_id),
);
$("#connection-button").addEventListener(
  "click",
  () => ($("#connection-details").hidden = !$("#connection-details").hidden),
);
$("#new-activity").addEventListener("click", () => {
  if (state.readingEarlier) {
    selectConversation(state.selected);
    $("#new-activity").textContent = "↓ New activity";
  } else scroller.scrollTop = scroller.scrollHeight;
  $("#new-activity").hidden = true;
});
mode.addEventListener("change", () => {
  const current =
    state.active?.conversation_id === state.selected ? state.active : null;
  if (mode.value === "follow_up" && !current) {
    mode.value = "new_task";
    error(
      "There is no live task in this conversation. Use a saved outcome to start a new task.",
    );
  } else {
    error("");
    state.mode = mode.value;
    state.target = mode.value === "follow_up" ? current.id : null;
    state.context = null;
  }
  renderComposer();
  save();
});
input.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.connected || !api.token || state.pairingRequired) return;
  if (sending || attachmentComposer.uploading || !input.value.trim()) return;
  const text = input.value.trim(),
    conversationId = state.selected;
  if (text.startsWith("/extend")) {
    const match = /^\/extend\s+(\d+)(?:\s+(\d+)\s+(\d+))?$/.exec(text);
    const target =
      state.active?.conversation_id === conversationId ? state.active : null;
    if (!match || !target) {
      error(
        "Use /extend REQUESTS [STEPS ACTIONS] for this conversation’s live task at its budget limit.",
      );
      return;
    }
    sending = true;
    renderComposer();
    try {
      if (
        await actions.extend(target, {
          requests: Number(match[1]),
          steps: Number(match[2] || 0),
          actions: Number(match[3] || 0),
        })
      ) {
        input.value = "";
        draft();
      }
    } finally {
      sending = false;
      renderComposer();
    }
    return;
  }
  const body = {
    text,
    execution_mode: state.executionMode || "approval",
    attachments: attachmentRefs(state.attachmentDrafts[conversationId]),
    intent: state.mode,
    client_request_id: crypto.randomUUID(),
    ...(state.mode === "follow_up" ? { task_id: state.target } : {}),
    ...(state.context ? { context_task_id: state.context } : {}),
  };
  if (!body.context_task_id && state.mode === "new_task" && /^(continue|resume|restart)(\s+(the\s+)?same(\s+(thing|task|work))?)?[.!]?$/i.test(text)) {
    const previous = Object.values(state.tasks).filter(task => ["cancelled", "interrupted"].includes(task.status)).at(-1);
    if (previous) body.context_task_id = previous.id;
  }
  if (state.pending) {
    const previous = state.pending;
    if (
      previous.conversationId !== conversationId ||
      previous.body.text !== text ||
      previous.body.intent !== body.intent ||
      !sameAttachments(previous.body.attachments, body.attachments)
    ) {
      error(
        "Resolve the previous submission using Retry previous instruction. Your current draft is kept.",
      );
      return;
    }
    body.client_request_id = previous.body.client_request_id;
    body.task_id = previous.body.task_id;
    body.context_task_id = previous.body.context_task_id;
  }
  await sendInstruction({ conversationId, body });
});

$("#retry-instruction").addEventListener("click", async () => {
  if (sending || !state.pending) return;
  sending = true;
  renderComposer();
  try {
    await recoverPending();
    if (state.pending) await sendInstruction(state.pending);
  } catch (cause) {
    error(cause.message);
  } finally {
    sending = false;
    renderComposer();
  }
});

async function recoverPending() {
  const result = await reconcilePending(state, api);
  if (!result) return;
  // A newer draft must survive recovery of an older submission.
  const { conversationId, body } = result.pending;
  clearSentAttachments(state, conversationId, body.attachments);
  if (state.drafts[conversationId]?.trim() === body.text)
    state.drafts[conversationId] = "";
  if (state.selected === conversationId && input.value.trim() === body.text)
    input.value = "";
  if (result.task) mergeTask(state, result.task);
  error(result.status === "retired"
    ? "The previous instruction was already accepted and its history deleted. It will not be sent again."
    : "");
  save();
  renderComposer();
}

async function sendInstruction({ conversationId, body }) {
  const text = body.text;
  sending = true;
  renderComposer();
  error("");
  try {
    state.pending = { conversationId, body };
    await chrome.storage.local.set({ axisWorkspace: persistentState(state) });
    await api.request("/api/conversations", {
      method: "POST",
      body: { id: conversationId },
    });
    const result = await api.request(
      `/api/conversations/${conversationId}/messages`,
      { method: "POST", body },
    );
    state.pending = null;
    clearSentAttachments(state, conversationId, body.attachments);
    if (input.value.trim() === text && state.selected === conversationId) {
      input.value = "";
      state.drafts[conversationId] = "";
    }
    mergeTask(state, result.task);
    if (state.selected === conversationId) {
      await selectConversation(conversationId);
      state.mode = state.active ? "follow_up" : "new_task";
      state.target = state.active?.id || null;
      state.context = null;
      mode.value = state.mode;
    }
    save();
  } catch (cause) {
    if (cause.code && !["unavailable"].includes(cause.code)) {
      state.pending = null;
      save();
    }
    if (cause.detail?.active_task) state.active = cause.detail.active_task;
    error(cause.message);
    // The stream may still be live after a POST response is lost, so do not
    // rely on a later reconnect to discover successful acceptance.
    if (state.pending) {
      try { await recoverPending(); } catch { /* Keep the retry control. */ }
    }
  } finally {
    sending = false;
    renderComposer();
    renderConnection();
    input.focus();
    draft();
  }
}
mountApprovals($("#approval-region"));
$("#execution-mode").addEventListener("change", async () => {
  const next = $("#execution-mode").value;
  const current = state.active?.conversation_id === state.selected ? state.active : null;
  try {
    if (current) {
      const task = await api.request(`/api/tasks/${current.id}/execution-mode`, { method: "PATCH", body: { execution_mode: next } });
      mergeTask(state, task);
    }
    state.executionMode = next;
    save(); draw();
  } catch (cause) { error(cause.message); renderComposer(); }
});
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "PING_SIDEPANEL") {
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "NATIVE_STATUS_CHANGED") {
    bridge = message.status || bridge;
    renderConnection();
    settings?.renderBridge(bridge);
  }
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.axisTheme) applyAppearance();
  if (changes.axisUiToken || changes.axisServiceUrl) {
    state.connected = false;
    streamController?.abort();
    draw();
    api
      .load()
      .then(connect)
      .catch((cause) => error(cause.message));
  }
});
async function refreshBridge() {
  try {
    const response = await chrome.runtime.sendMessage({
      type: "GET_NATIVE_STATUS",
    });
    bridge = response.status || bridge;
  } catch (cause) {
    bridge = { state: "unavailable", error: cause.message };
  }
  renderConnection();
}
async function connect() {
  clearTimeout(reconnectTimer);
  streamController?.abort();
  const controller = new AbortController();
  streamController = controller;
  try {
    const status = await api.request("/api/status");
    if (controller.signal.aborted) return;
    state.connected = true;
    state.pairingRequired = false;
    const feedbackText = $("#workspace-error").textContent;
    if (feedbackText === "Pair the local AXIS service in Settings." ||
        feedbackText === "Pairing credential was rejected." ||
        feedbackText === "AXIS service is offline or unreachable. Check the service address and startup instructions.")
      error("");
    state.active = status.active_task;
    state.stream = "connecting";
    await recoverPending();
    if (controller.signal.aborted) return;
    if (status.storage_error) error(status.storage_error);
    if (
      !state.conversation &&
      !state.messages.length &&
      state.active &&
      !saved.selected
    )
      state.selected = state.active.conversation_id;
    const snapshot = await api
      .request(`/api/conversations/${state.selected}`)
      .catch((cause) => {
        if (cause.code === "not_found") return null;
        throw cause;
      });
    if (controller.signal.aborted) return;
    if (snapshot) {
      const oldActivity = state.activity,
        oldMessages = state.messages,
        oldTasks = state.tasks;
      applySnapshot(state, snapshot);
      for (const [id, page] of Object.entries(oldActivity))
        if (page.older && state.tasks[id]) state.activity[id] = page;
      if (state.readingEarlier) {
        state.messages = oldMessages;
        state.tasks = oldTasks;
        state.activity = oldActivity;
      }
      state.cursor = snapshot.cursor;
    } else state.cursor = status.cursor;
    if (state.active?.conversation_id === state.selected && !state.context) {
      state.mode = "follow_up";
      state.target = state.active.id;
      mode.value = state.mode;
    }
    input.value = state.drafts[state.selected] || input.value;
    state.stream = "live";
    draw();
    scroller.scrollTop =
      state.views[state.selected]?.scroll ?? scroller.scrollHeight;
    await api.stream(
      state.cursor,
      (event) => {
        if (controller.signal.aborted) return;
        if (event.type === "resync") {
          controller.abort();
          reconnectTimer = setTimeout(connect, 100);
          return;
        }
        if (loadingSnapshot) bufferedEvents.push(event);
        if (applyEvent(state, event)) scheduleDraw(true);
        else renderConnection();
        if (state.readingEarlier && event.conversation_id === state.selected)
          $("#new-activity").hidden = false;
      },
      controller.signal,
    );
    if (!controller.signal.aborted)
      throw new Error("Activity stream disconnected.");
  } catch (cause) {
    if (controller.signal.aborted) return;
    state.stream = "reconnecting";
    state.pairingRequired = cause.code === "not_paired" || cause.code === "unauthorized";
    if (
      cause.code === "not_paired" ||
      cause.code === "unauthorized" ||
      cause.code === "unavailable"
    )
      state.connected = false;
    draw();
    reconnectTimer = setTimeout(connect, 3000);
  }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshBridge();
    if (state.stream !== "live") connect();
  }
});
window.addEventListener("pagehide", () => {
  clearTimeout(reconnectTimer);
  streamController?.abort();
  rememberView(state, scroller.scrollTop);
  chrome.storage.local.set({ axisWorkspace: persistentState(state) });
});
draw();
refreshBridge();
connect();
const elapsedTimer = setInterval(() => {
  if (!state.connected || workspace.hidden) return;
  for (const node of timeline.querySelectorAll("[data-live-task]")) {
    const task = state.tasks[node.dataset.liveTask];
    if (task?.created_at)
      node.textContent = `${task.counters?.browser_actions || 0} actions · ${duration(Date.now() - task.created_at * 1000)}`;
  }
}, 1000);
window.addEventListener("pagehide", () => clearInterval(elapsedTimer), {
  once: true,
});
