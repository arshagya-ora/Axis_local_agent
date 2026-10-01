import { el, icon, button, feedback } from "./dom.js";

export async function applyAppearance() {
  const { axisTheme = "system" } = await chrome.storage.local.get("axisTheme");
  document.documentElement.dataset.theme = axisTheme;
}
export function mountSettings(
  root,
  api,
  { onBack, onPaired = () => {}, options = false } = {},
) {
  root.replaceChildren();
  const toolbar = el("header", "toolbar"),
    heading = el("div", "row");
  if (!options)
    heading.append(button("Back to workspace", onBack, "back", "icon-button"));
  heading.append(el("strong", "", "Settings"));
  toolbar.append(heading);
  if (!options)
    toolbar.append(
      button(
        "Open Settings in a tab",
        () => chrome.runtime.openOptionsPage(),
        "external",
        "icon-button",
      ),
    );
  const content = el("div", "settings-content");
  root.append(toolbar, content);
  function group(title) {
    const section = el("section", "settings-group");
    section.append(el("h2", "", title));
    content.append(section);
    return section;
  }
  function row(parent, symbol, title, description, end) {
    const node = el("div", "setting-row"),
      copy = el("div", "setting-copy");
    copy.append(el("div", "", title));
    if (description) copy.append(el("p", "", description));
    node.append(icon(symbol), copy);
    if (end) node.append(end);
    parent.append(node);
    return node;
  }
  function field(parent, label, type, value) {
    const wrap = el("label", "", label),
      input = el("input");
    input.type = type;
    input.value = value;
    wrap.append(input);
    parent.append(wrap);
    return input;
  }
  function report(parent) {
    const node = el("p", "feedback");
    node.hidden = true;
    node.setAttribute("role", "status");
    parent.append(node);
    return node;
  }
  async function operation(action, node, control) {
    if (control) control.disabled = true;
    try {
      await action();
    } catch (error) {
      feedback(node, error.message, true);
    } finally {
      if (control) control.disabled = false;
    }
  }
  const connection = group("Connection"),
    agentStatus = el("span", "setting-value", "Checking…"),
    bridgeStatus = el("span", "setting-value", "Checking…");
  row(connection, "globe", "AXIS agent", "Local task service", agentStatus);
  row(
    connection,
    "link",
    "Browser bridge",
    "Native browser connection",
    bridgeStatus,
  );
  const connectionMessage = report(connection),
    bridgeActions = el("div", "row");
  connection.append(bridgeActions);
  let bridgeEnabled = false;
  const bridgeToggle = button("Start bridge", async () => {
    const action = bridgeEnabled ? "STOP_BRIDGE" : "START_BRIDGE";
    // Request directly inside the click gesture; no unrelated asynchronous work first.
    let permission;
    if (!bridgeEnabled)
      permission = chrome.permissions.request({
        permissions: ["tabs", "tabGroups", "downloads"],
      });
    await operation(
      async () => {
        if (permission && !(await permission))
          throw new Error(
            "Chrome permissions are required to start the bridge.",
          );
        if (permission)
          await chrome.storage.local.set({ optionalPermissionsGranted: true });
        const result = await chrome.runtime.sendMessage({
          type: action,
        });
        if (result.ok === false)
          throw new Error(result.error || "Could not change bridge state.");
        renderBridge(result.status);
        feedback(
          connectionMessage,
          result.status?.error || "Bridge state updated.",
          !!result.status?.error,
        );
      },
      connectionMessage,
      bridgeToggle,
    );
  });
  bridgeActions.append(
    bridgeToggle,
    button("Refresh", () => refresh()),
  );
  function renderBridge(status) {
    bridgeEnabled = status?.bridgeEnabled === true;
    bridgeStatus.textContent =
      status?.state === "connected"
        ? "Connected"
        : status?.state === "connecting"
          ? "Connecting…"
          : status?.state === "stopped"
            ? "Stopped"
            : "Disconnected";
    bridgeStatus.classList.toggle("ready", status?.state === "connected");
    bridgeToggle.textContent = bridgeEnabled ? "Stop bridge" : "Start bridge";
    if (status?.error && status.state !== "stopped")
      feedback(connectionMessage, status.error, true);
    else feedback(connectionMessage, "");
  }
  const pairing = el("details");
  pairing.append(el("summary", "", "Pair AXIS service"));
  const pairingForm = el("div", "settings-form");
  pairing.append(pairingForm);
  connection.append(pairing);
  pairingForm.append(
    el(
      "p",
      "metadata",
      "Start the local service, then enter its UI credential. This is separate from your model credentials.",
    ),
  );
  const serviceUrl = field(pairingForm, "Service address", "url", api.url),
    token = field(pairingForm, "UI pairing credential", "password", "");
  token.autocomplete = "off";
  token.placeholder = api.token
    ? "Saved credential (enter to replace)"
    : "From the local pairing file";
  pairingForm.append(el("p", "metadata", `Extension ID: ${chrome.runtime.id}`));
  const pairMessage = report(pairingForm),
    pair = button(
      "Save and connect",
      () =>
        operation(
          async () => {
            const candidate = new api.constructor();
            candidate.configure(serviceUrl.value, token.value || api.token);
            await candidate.request("/api/status");
            await chrome.storage.local.set({
              axisServiceUrl: candidate.url,
              axisUiToken: candidate.token,
            });
            api.configure(candidate.url, candidate.token);
            token.value = "";
            token.placeholder = "Saved credential (enter to replace)";
            feedback(pairMessage, "AXIS service paired.");
            await refresh();
            onPaired();
          },
          pairMessage,
          pair,
        ),
      "",
      "primary",
    );
  pairingForm.append(pair);
  const appearance = group("Appearance"),
    segment = el("div", "segmented");
  segment.setAttribute("role", "radiogroup");
  segment.setAttribute("aria-label", "Theme");
  for (const value of ["light", "dark", "system"]) {
    const label = el("label", "", value[0].toUpperCase() + value.slice(1)),
      input = el("input");
    input.type = "radio";
    input.name = "theme";
    input.value = value;
    input.checked =
      (document.documentElement.dataset.theme || "system") === value;
    label.prepend(input);
    segment.append(label);
  }
  row(appearance, "sun", "Theme", "", segment);
  const appearanceMessage = report(appearance);
  appearance.append(
    button(
      "Save appearance",
      () =>
        operation(async () => {
          const axisTheme = segment.querySelector(":checked").value;
          await chrome.storage.local.set({ axisTheme });
          await applyAppearance();
          feedback(appearanceMessage, "Appearance saved.");
        }, appearanceMessage),
      "",
      "primary full-width",
    ),
  );
  const bridge = group("Browser bridge"),
    approval = el("input");
  approval.type = "checkbox";
  approval.setAttribute("aria-label", "Runtime approvals");
  row(
    bridge,
    "shield",
    "Runtime approvals",
    "Review sensitive browser actions",
    approval,
  );
  const approvalMessage = report(bridge);
  approval.addEventListener("change", () =>
    operation(
      async () => {
        try {
          await chrome.storage.local.set({
            enableRuntimeApproval: approval.checked,
          });
          feedback(approvalMessage, "Approval preference applied.");
        } catch (error) {
          approval.checked = !approval.checked;
          throw error;
        }
      },
      approvalMessage,
      approval,
    ),
  );
  const agent = group("Agent"),
    identity = el("span", "setting-value", "Service unavailable");
  row(agent, "cube", "Model", "Configured provider · read only", identity);
  const limits = el("span", "setting-value", "Service unavailable");
  row(agent, "limits", "Run limits", "Applied by the agent", limits);
  const runDetails = el("details");
  runDetails.append(el("summary", "", "Edit limits for future tasks"));
  const limitsForm = el("div", "settings-form");
  runDetails.append(limitsForm);
  agent.append(runDetails);
  limitsForm.append(
    el(
      "p",
      "metadata",
      "Active and paused tasks retain their original budgets.",
    ),
  );
  const limitInputs = {};
  for (const [name, label] of Object.entries({
    max_total_steps: "Maximum steps",
    max_browser_actions: "Maximum browser actions",
    max_model_requests: "Maximum model requests",
  })) {
    limitInputs[name] = field(limitsForm, label, "number", "");
    limitInputs[name].min = "1";
    limitInputs[name].max = "10000";
  }
  const limitMessage = report(limitsForm);
  limitsForm.append(
    button("Apply to future tasks", () =>
      operation(async () => {
        const run = {};
        for (const [key, input] of Object.entries(limitInputs))
          run[key] = Number(input.value);
        await api.request("/api/settings", { method: "PATCH", body: { run } });
        feedback(limitMessage, "Limits saved for future tasks.");
        await refresh();
      }, limitMessage),
    ),
  );
  const advanced = group("Advanced"),
    advancedDetails = el("details");
  advancedDetails.append(el("summary", "", "Port and diagnostics"));
  advanced.append(advancedDetails);
  const advancedForm = el("div", "settings-form");
  advancedDetails.append(advancedForm);
  const port = field(advancedForm, "Browser bridge port", "number", "8765");
  port.min = "1024";
  port.max = "65535";
  advancedForm.append(
    el(
      "p",
      "metadata",
      "Changing the port reloads the extension and can disrupt an active browser task. Update the Python bridge connection to match.",
    ),
  );
  const reloadLabel = el("label", "check-label"),
    reloadCheck = el("input");
  reloadCheck.type = "checkbox";
  reloadLabel.append(
    reloadCheck,
    document.createTextNode("I understand this reloads the extension."),
  );
  advancedForm.append(reloadLabel);
  const portMessage = report(advancedForm);
  advancedForm.append(
    button("Apply port and reload", () =>
      operation(async () => {
        const bridgePort = Number(port.value);
        if (
          !Number.isInteger(bridgePort) ||
          bridgePort < 1024 ||
          bridgePort > 65535
        )
          throw new Error("Enter a whole port number from 1024 to 65535.");
        if (!reloadCheck.checked)
          throw new Error("Acknowledge the extension reload before applying.");
        await chrome.storage.local.set({ bridgePort });
        chrome.runtime.reload();
      }, portMessage),
    ),
  );
  const cspLabel = el("label", "check-label"),
    csp = el("input");
  csp.type = "checkbox";
  cspLabel.append(
    csp,
    document.createTextNode("Allow temporary CSP bypass for target sites"),
  );
  advancedForm.append(cspLabel);
  const cspMessage = report(advancedForm);
  csp.addEventListener("change", () =>
    operation(
      async () => {
        try {
          const response = await chrome.runtime.sendMessage({
            type: "SET_CSP_BYPASS",
            enabled: csp.checked,
          });
          if (response.ok === false)
            throw new Error(response.error || "CSP setting was not applied.");
        } catch (error) {
          csp.checked = !csp.checked;
          throw error;
        }
        feedback(cspMessage, "CSP preference applied.");
      },
      cspMessage,
      csp,
    ),
  );
  async function refresh() {
    await Promise.allSettled([
      (async () => {
        try {
          const result = await chrome.runtime.sendMessage({
            type: "GET_NATIVE_STATUS",
          });
          renderBridge(result.status);
        } catch (error) {
          bridgeStatus.textContent = "Unavailable";
          feedback(connectionMessage, error.message, true);
        }
      })(),
      (async () => {
        try {
          const status = await api.request("/api/status");
          agentStatus.textContent = status.storage_error
            ? "Storage error"
            : "Connected";
          agentStatus.classList.toggle("ready", !status.storage_error);
          const settings = await api.request("/api/settings");
          identity.textContent = settings.model || "Configured provider";
          limits.textContent = `${settings.run.max_total_steps} steps · ${settings.run.max_browser_actions} actions`;
          for (const [key, input] of Object.entries(limitInputs))
            if (document.activeElement !== input)
              input.value = settings.run[key];
        } catch (error) {
          agentStatus.textContent =
            error.code === "not_paired" ? "Not paired" : "Offline";
          agentStatus.classList.remove("ready");
          identity.textContent = "Service unavailable";
          limits.textContent = "Service unavailable";
        }
      })(),
    ]);
  }
  operation(async () => {
    const values = await chrome.storage.local.get([
      "bridgePort",
      "enableRuntimeApproval",
    ]);
    port.value = values.bridgePort || 8765;
    approval.checked = values.enableRuntimeApproval !== false;
    const bypass = await chrome.runtime.sendMessage({ type: "GET_CSP_BYPASS" });
    csp.checked = bypass.enabled === true;
  }, connectionMessage);
  refresh();
  return { refresh, renderBridge };
}
