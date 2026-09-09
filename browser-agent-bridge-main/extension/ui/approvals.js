import { el, button, feedback } from "./dom.js";
const descriptions = {
  read_tabs: "Read the titles and addresses of your open tabs.",
  cookies: "Read cookies, including sensitive session tokens.",
  tab_control: "Close tabs or stop browser sessions.",
  read_downloads: "Read download history and local file locations.",
  page_script: "Run JavaScript in the current page.",
  page_screenshot: "Capture the page or its structure.",
  page_input: "Type text or use keyboard shortcuts.",
  page_action: "Interact with controls or upload files.",
  page_logs: "Read console and network diagnostics.",
  policy_admin: "Change the local browser security policy.",
  recording_data: "Access recorded browser activity.",
};
export function mountApprovals(root, { popup = false } = {}) {
  let prompts = [],
    sending = false,
    loaded = false,
    refreshing = false;
  function render() {
    const prompt = prompts[0];
    root.hidden = !prompt && !popup;
    if (!prompt) {
      root.replaceChildren(el("p", "", "No pending approval requests."));
      if (popup && loaded) window.close();
      return;
    }
    if (root.dataset.prompt === String(prompt.promptId) && root.children.length)
      return;
    root.dataset.prompt = String(prompt.promptId);
    const card = el("div", "permission-card");
    card.append(
      el("h2", "", "Browser bridge approval"),
      el(
        "p",
        "",
        descriptions[prompt.category] || "Review this browser action.",
      ),
      el("p", "metadata", prompt.method),
    );
    const details = el("details");
    details.append(
      el("summary", "", "Action details"),
      el("pre", "permission-params", JSON.stringify(prompt.params, null, 2)),
    );
    card.append(details);
    const message = el("p", "feedback");
    message.setAttribute("role", "status");
    card.append(message);
    const actions = el("div", "permission-actions");
    for (const [label, response] of [
      ["Allow once", "allow"],
      ["Allow for session", "session_allow"],
      ["Deny", "deny"],
    ])
      actions.append(
        button(
          label,
          async () => {
            if (sending) return;
            sending = true;
            actions
              .querySelectorAll("button")
              .forEach((node) => (node.disabled = true));
            try {
              const result = await chrome.runtime.sendMessage({
                type: "PERMISSION_RESPONSE",
                promptId: prompt.promptId,
                response,
              });
              if (result.ok === false)
                throw new Error(
                  result.error || "The approval response was not accepted.",
                );
              await refresh();
            } catch (error) {
              feedback(
                message,
                `Response not confirmed: ${error.message}`,
                true,
              );
              await refresh();
            } finally {
              sending = false;
              actions
                .querySelectorAll("button")
                .forEach((node) => (node.disabled = false));
            }
          },
          null,
          response === "allow" ? "primary" : "",
        ),
      );
    card.append(actions);
    root.replaceChildren(card);
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const result = await chrome.runtime.sendMessage({
        type: "GET_PENDING_PERMISSION_PROMPTS",
      });
      if (result.ok === false)
        throw new Error("Could not restore pending approvals.");
      prompts = result.prompts || [];
      loaded = true;
      if (
        !prompts.some(
          (prompt) => String(prompt.promptId) === root.dataset.prompt,
        )
      )
        root.dataset.prompt = "";
      render();
    } catch (error) {
      if (!loaded) {
        root.hidden = false;
        root.replaceChildren(
          el("p", "notice", `Approvals unavailable: ${error.message}`),
          button("Retry approvals", refresh),
        );
      }
    } finally {
      refreshing = false;
    }
  }
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "PROMPT_PERMISSION") refresh();
    if (!popup && message?.type === "PING_SIDEPANEL") return undefined;
  });
  const interval = setInterval(refresh, 2000);
  window.addEventListener("pagehide", () => clearInterval(interval), {
    once: true,
  });
  refresh();
  return { refresh };
}
