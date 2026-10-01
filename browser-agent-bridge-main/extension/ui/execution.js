import { el, button } from "./dom.js";

export function renderExecutionApproval(root, task, api, onError) {
  const pending = task?.pending_approval;
  root.hidden = !pending;
  if (!pending) { root.replaceChildren(); delete root.dataset.approval; return; }
  if (root.dataset.approval === pending.id) return;
  root.dataset.approval = pending.id;
  const card = el("div", "permission-card execution-card");
  card.append(el("h2", "", "Approve website change"), el("p", "", task.title));
  for (const action of pending.actions) {
    card.append(el("p", "", `${action.operation} · ${action.current_url || action.origin || "Destination not established"}`));
    if (action.target) card.append(el("p", "", `Target: ${action.target}`));
    const details = el("details");
    details.append(el("summary", "", "Review exact change and verification"));
    details.append(el("pre", "permission-params", JSON.stringify({ arguments: action.arguments,
      observed_target: action.observed_target, files: action.files, verification: action.verification }, null, 2)));
    card.append(details);
  }
  const controls = el("div", "permission-actions");
  const decide = async decision => {
    controls.querySelectorAll("button").forEach(node => node.disabled = true);
    try {
      await api.request(`/api/tasks/${task.id}/approvals/${pending.id}`, { method: "POST", body: { decision } });
      card.append(el("p", "metadata", decision === "approve" ? "Approved; checking task state…" : "Declined; pausing task…"));
    } catch (cause) {
      onError(cause.message);
      controls.querySelectorAll("button").forEach(node => node.disabled = false);
    }
  };
  controls.append(button("Approve change", () => decide("approve"), null, "primary"),
    button("Decline", () => decide("deny")));
  card.append(controls);
  root.replaceChildren(card);
}
