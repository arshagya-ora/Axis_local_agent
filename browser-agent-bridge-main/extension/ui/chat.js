import { el, icon, button } from "./dom.js";
import { STATUS, TERMINAL, ownsRuntime } from "./state.js";
import { renderActivity, activityText } from "./activity.js";
import { renderMarkdown } from "./markdown.js";

export function duration(ms) {
  const seconds = Math.floor((ms || 0) / 1000);
  return seconds >= 60
    ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
    : `${seconds}s`;
}
export function renderChat(root, state, actions) {
  const nodes = [];
  if (state.hasMore)
    nodes.push([
      "earlier",
      () =>
        button(
          "Load earlier messages",
          actions.earlierMessages,
          null,
          "load-earlier",
        ),
      "earlier",
    ]);
  if (!state.messages.length && !Object.keys(state.tasks).length) {
    nodes.push([
      "empty",
      () => {
        const empty = el("div", "empty-state"),
          symbol = el("div", "empty-symbol");
        symbol.append(icon("browser"));
        empty.append(
          symbol,
          el("h2", "", "Your browser workspace"),
          el(
            "p",
            "",
            "Give AXIS a browser task. Its activity and outcome will appear here.",
          ),
        );
        if (!state.connected)
          empty.append(button("Set up connection", actions.settings));
        return empty;
      },
      String(state.connected),
    ]);
  }
  const shown = new Set();
  const lastAnswer = new Map();
  for (const message of state.messages)
    if (message.role === "assistant" && state.tasks[message.task_id]?.status === "completed")
      lastAnswer.set(message.task_id, message.id);
  for (const message of state.messages) {
    nodes.push([
      `message-${message.id}`,
      () => {
        const node = el("article", `message ${message.role}`);
        node.setAttribute(
          "aria-label",
          message.role === "user" ? "You" : "AXIS",
        );
        let text = message.text;
        if (message.intent === "extend_budget") {
          const values = JSON.parse(message.text);
          text = `Extend budget: +${values.requests} model requests, +${values.steps} steps, +${values.actions} actions`;
        }
        node.append(message.role === "assistant" ? renderMarkdown(text) : document.createTextNode(text));
        for (const ref of message.attachments || []) {
          const file = state.tasks[message.task_id]?.attachments?.find(x => x.id === ref.attachment_id);
          node.append(el("p", "metadata", `${file?.filename || 'Attached file'} · ${ref.role}`));
        }
        if (["follow_up", "extend_budget"].includes(message.intent))
          node.append(
            el(
              "span",
              "metadata",
              message.delivery === "accepted"
                ? "Accepted · waiting for a safe step"
                : message.delivery === "not_applied"
                  ? "Not applied · task stopped or could not continue"
                  : "Applied to task",
            ),
          );
        return node;
      },
      JSON.stringify(message),
    ]);
    if (
      message.task_id &&
      !shown.has(message.task_id) &&
      state.tasks[message.task_id] &&
      (!lastAnswer.has(message.task_id) || lastAnswer.get(message.task_id) === message.id)
    ) {
      shown.add(message.task_id);
      addTask(state.tasks[message.task_id]);
    }
  }
  for (const task of Object.values(state.tasks))
    if (!shown.has(task.id)) addTask(task);
  function addTask(task) {
    nodes.push([
      `task-${task.id}`,
      () => taskCard(task, state, actions),
      JSON.stringify([
        task,
        state.expanded[task.id],
        state.activity[task.id],
        state.active?.id,
      ]),
    ]);
  }
  // Keyed reconciliation leaves the composer and unchanged cards/focus intact.
  const existing = new Map(
    [...root.children].map((node) => [node.dataset.key, node]),
  );
  let previous = null;
  for (const [key, create, version] of nodes) {
    let node = existing.get(key);
    if (!node || node.dataset.version !== version) {
      const focused = node?.contains(document.activeElement)
        ? document.activeElement
        : null;
      const focusLabel =
        focused?.getAttribute("aria-label") || focused?.textContent;
      const focusPhase = focused?.dataset.phase;
      const replacement = create();
      replacement.dataset.key = key;
      replacement.dataset.version = version;
      if (node) node.replaceWith(replacement);
      node = replacement;
      if (focused) {
        const match =
          [...node.querySelectorAll("button, summary")].find(
            (item) =>
              focusPhase ? item.dataset.phase === focusPhase :
                (item.getAttribute("aria-label") || item.textContent) === focusLabel,
          ) || node.querySelector(".disclosure");
        match?.focus({ preventScroll: true });
      }
    }
    if (
      node !== (previous ? previous.nextElementSibling : root.firstElementChild)
    )
      root.insertBefore(
        node,
        previous ? previous.nextSibling : root.firstChild,
      );
    previous = node;
    existing.delete(key);
  }
  for (const node of existing.values()) node.remove();
}
function taskCard(task, state, actions) {
  const expanded = state.expanded[task.id] === true;
  const card = el("article", `task-card${expanded ? "" : " collapsed"}${task.status === "completed" ? " task-completed" : ""}`);
  card.setAttribute(
    "aria-label",
    `${task.title}: ${STATUS[task.status] || task.status}`,
  );
  const summary = el("div", "task-summary");
  summary.append(el("p", "task-current", activityText(task.current_activity)));
  const metrics = el("div", "task-metrics");
  metrics.append(
    el("span", `badge ${task.status}`, STATUS[task.status] || task.status),
    el(
      "p",
      "metadata task-meta",
      `${task.counters?.browser_actions || 0} actions · ${duration(task.duration_ms)}`,
    ),
  );
  card.append(metrics);
  if (task.execution_mode) card.append(el("p", "metadata", task.execution_mode === "automatic" ? "Automatic mode" : "Approval mode"));
  if (task.status !== "completed" || expanded) card.append(summary);
  if (["active", "pausing", "stopping"].includes(task.status))
    card.querySelector(".task-meta").dataset.liveTask = task.id;
  if (expanded) {
    const page = state.activity[task.id] || { items: [] };
    if (page.has_more)
      card.append(
        button(
          "Load earlier activity",
          () => actions.earlierActivity(task),
          null,
          "load-earlier",
        ),
      );
    if (page.older)
      card.append(
        button(
          "Show latest activity",
          () => actions.latestActivity(task),
          null,
          "load-earlier",
        ),
      );
    if (page.items.length) {
      card.append(renderActivity(task.id, page, state));
    }
    if (task.result?.limitations?.length)
      for (const limitation of task.result.limitations)
        card.append(el("p", "notice", limitation));
    // Only outputs actually reported by the service are shown. No arbitrary path links.
    for (const artifact of task.artifacts || []) {
      const tile = el("div", "artifact");
      tile.append(icon("file"), el("p", "", artifact.label));
      card.append(tile);
    }
  }
  const controls = el("div", "task-controls"),
    disclosure = button(
      expanded ? "Hide activity" : "Show activity",
      () => actions.expand(task.id, !expanded),
      expanded ? "up" : "down",
      "disclosure",
    );
  disclosure.setAttribute("aria-expanded", String(expanded));
  controls.append(disclosure);
  const buttons = el("div");
  if (task.status === "interrupted" && Object.keys(task.workflow?.counts || {}).length && !state.active)
    buttons.append(button("Recover workflow", () => actions.control(task, "resume")));
  if (task.workflow && (task.status !== "completed" || expanded)) {
    const counts = task.workflow.counts;
    const uncovered = task.workflow.uncovered_sections?.length || 0;
    if (Object.values(counts).some(value => value > 0) || uncovered)
      card.append(el("p", "metadata workflow-summary", `Workflow: ${counts.verified || 0} verified · ${(counts.pending || 0) + (counts.running || 0)} pending · ${counts.skipped || 0} not applicable${uncovered ? ` · ${uncovered}${task.workflow.more_uncovered ? '+' : ''} sections to plan` : ''}`));
  }
  if (state.active?.id === task.id && ownsRuntime(task)) {
    if (task.status === "active")
      buttons.append(button("Pause", () => actions.control(task, "pause")));
    if (task.status === "paused")
      buttons.append(button("Resume", () => actions.control(task, "resume")));
    if (task.status === "limit_reached") {
      const details = el("details", "budget-extension");
      details.append(el("summary", "", "Extend budget"));
      const form = el("form", "settings-form");
      form.append(
        el(
          "p",
          "metadata",
          "Add to this task’s budget while retaining its tabs, sources, and evidence.",
        ),
      );
      const inputs = {};
      for (const [key, label, value, min] of [
        ["requests", "Additional model requests", 10, 1],
        ["steps", "Additional steps", 10, 0],
        ["actions", "Additional browser actions", 30, 0],
      ]) {
        const field = el("label", "", label),
          input = el("input");
        input.type = "number";
        input.min = min;
        input.max = 10000;
        input.value = value;
        input.required = true;
        field.append(input);
        form.append(field);
        inputs[key] = input;
      }
      const submit = el("button", "primary", "Apply extension and resume");
      submit.type = "submit";
      form.append(submit);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          await actions.extend(
            task,
            Object.fromEntries(
              Object.entries(inputs).map(([key, input]) => [
                key,
                Number(input.value),
              ]),
            ),
          );
        } finally {
          submit.disabled = false;
        }
      });
      details.append(form);
      card.append(details);
    }
    const stop = button(task.status === "stopping" ? "Stopping…" : "Stop", () =>
      actions.control(task, "stop"),
    );
    stop.disabled = task.status === "stopping";
    buttons.append(stop);
  } else if (TERMINAL.has(task.status))
    buttons.append(
      button("Use saved outcome", () => actions.savedContext(task)),
    );
  controls.append(buttons);
  card.append(controls);
  return card;
}
