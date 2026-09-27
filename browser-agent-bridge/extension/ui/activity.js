import { el } from "./dom.js";

const phases = ["Planning", "Browsing", "Evidence", "Result", "Activity"];

export function activityText(text) {
  const labels = { paused: "Paused", completed: "Completed", cancelled: "Stopped", limit_reached: "Task budget reached" };
  if (Object.hasOwn(labels, text)) return labels[text];
  if (/^observe(?:: [\w ]+)? — completed$/.test(text)) return "Read page content";
  return text;
}

export function activityPhase(item) {
  if (phases.includes(item.payload?.phase)) return item.payload.phase;
  if (item.type === "final") return "Result";
  if (item.type === "planner_decision") return "Planning";
  if (["browser_action", "navigator_step"].includes(item.type)) return "Browsing";
  // Older history predates explicit phase metadata. Match only known public labels.
  const text = item.payload?.text;
  if (["Planning the next step", "Accepted; preparing the agent"].includes(text)) return "Planning";
  if (["Read browser context", "Read attachment or updated workflow progress"].includes(text)) return "Evidence";
  if (["Working in the browser", "Selected a browser tab", "Opened a browser tab", "Visual input unavailable"].includes(text)) return "Browsing";
  return "Activity";
}

export function groupActivity(items) {
  return phases.map(phase => ({ phase, items: items.filter(item => item.payload?.text && activityPhase(item) === phase) }))
    .filter(group => group.items.length);
}

export function renderActivity(taskId, page, state) {
  const root = el("div", "activity-phases");
  const groups = groupActivity(page.items);
  state.activityExpanded ||= {};
  const preferences = state.activityExpanded[taskId] ||= {};
  const latest = page.items.filter(item => item.payload?.text).at(-1);
  if (page.has_more || page.older)
    root.append(el("p", "metadata activity-page-note", "Counts reflect the loaded activity page."));
  for (const group of groups) {
    const details = el("details", "activity-phase");
    details.open = preferences[group.phase] ?? (latest && activityPhase(latest) === group.phase);
    const summary = el("summary");
    summary.dataset.phase = group.phase;
    summary.append(el("span", "phase-name", group.phase),
      el("span", "metadata phase-count", `${group.items.length} ${group.items.length === 1 ? "step" : "steps"}`));
    details.append(summary);
    const list = el("ol", "activity");
    for (const item of group.items) {
      const row = el("li");
      row.append(el("span", "activity-text", activityText(item.payload.text)));
      const date = typeof item.occurred_at === "number" ? new Date(item.occurred_at * 1000) : null;
      if (date && Number.isFinite(date.getTime())) {
        const time = el("time", "metadata", date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }));
        time.dateTime = date.toISOString();
        time.title = date.toLocaleString();
        row.append(time);
      }
      list.append(row);
    }
    details.append(list);
    details.addEventListener("toggle", () => {
      if (details.isConnected) preferences[group.phase] = details.open;
    });
    root.append(details);
  }
  return root;
}
