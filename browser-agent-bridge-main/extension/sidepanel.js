const statusEl = document.querySelector('#status');
const hostNameEl = document.querySelector('#host-name');
const lastCheckedEl = document.querySelector('#last-checked');
const errorEl = document.querySelector('#error');
const bypassCspEl = document.querySelector('#bypass-csp');
const bridgePortEl = document.querySelector('#bridge-port');
const savePortBtn = document.querySelector('#save-port-btn');
const bridgeToggleBtn = document.querySelector('#bridge-toggle-btn');
// Unused settings checkboxes removed from UI
const enableRuntimeApprovalEl = document.querySelector('#enable-runtime-approval');

const permissionCard = document.querySelector('#permission-card');
const permissionDetails = document.querySelector('#permission-details');
const permAllowBtn = document.querySelector('#perm-allow-btn');
const permSessionAllowBtn = document.querySelector('#perm-session-allow-btn');
const permDenyBtn = document.querySelector('#perm-deny-btn');

const REQUIRED_OPTIONAL_PERMISSIONS = ['tabs', 'tabGroups', 'downloads'];

document.querySelector('#refresh').addEventListener('click', refresh);
bridgeToggleBtn.addEventListener('click', async () => {
  bridgeToggleBtn.disabled = true;
  try {
    const startingBridge = !latestBridgeEnabled;
    if (startingBridge) {
      const alreadyGranted = await chrome.permissions.contains({ permissions: REQUIRED_OPTIONAL_PERMISSIONS });
      if (!alreadyGranted) {
        const granted = await chrome.permissions.request({ permissions: REQUIRED_OPTIONAL_PERMISSIONS });
        if (!granted) {
          errorEl.textContent = 'Required Chrome permissions were not granted. The bridge cannot control tabs, tab groups, or downloads without them.';
          bridgeToggleBtn.disabled = false;
          return;
        }
        await chrome.storage.local.set({ optionalPermissionsGranted: true });
      }
    }
    const messageType = latestBridgeEnabled ? 'STOP_BRIDGE' : 'START_BRIDGE';
    const response = await chrome.runtime.sendMessage({ type: messageType });
    renderStatus(response.status);
  } catch (error) {
    errorEl.textContent = error?.message || 'Failed to update bridge state';
    bridgeToggleBtn.disabled = false;
  }
});

let activePrompts = [];
let currentPrompt = null;

function appendLabeledValue(parent, label, value, valueTag = 'span') {
  const strong = document.createElement('strong');
  strong.textContent = label;
  parent.appendChild(strong);
  const valueEl = document.createElement(valueTag);
  valueEl.textContent = value;
  parent.appendChild(valueEl);
  parent.appendChild(document.createElement('br'));
}

function showNextPrompt() {
  if (currentPrompt) return;
  if (activePrompts.length === 0) {
    permissionCard.style.display = 'none';
    return;
  }

  currentPrompt = activePrompts.shift();
  permissionDetails.textContent = '';
  appendLabeledValue(permissionDetails, 'Method: ', currentPrompt.method, 'code');

  const descriptionLabel = document.createElement('strong');
  descriptionLabel.textContent = 'Description:';
  permissionDetails.appendChild(descriptionLabel);
  permissionDetails.appendChild(document.createElement('br'));
  permissionDetails.appendChild(document.createTextNode(currentPrompt.labels.en));
  permissionDetails.appendChild(document.createElement('br'));

  const paramsLabel = document.createElement('strong');
  paramsLabel.textContent = 'Params:';
  permissionDetails.appendChild(paramsLabel);
  const paramsPre = document.createElement('pre');
  paramsPre.className = 'permission-params';
  paramsPre.textContent = JSON.stringify(currentPrompt.params, null, 2);
  permissionDetails.appendChild(paramsPre);
  permissionCard.style.display = 'block';
}

function resolveCurrentPrompt(responseValue) {
  if (!currentPrompt) return;
  chrome.runtime.sendMessage({
    type: 'PERMISSION_RESPONSE',
    promptId: currentPrompt.promptId,
    response: responseValue
  }).catch(() => {});

  currentPrompt = null;
  showNextPrompt();
}

function getCategoryLabel(category) {
  const descriptions = {
    read_tabs: 'Read tab list (access titles and URLs of all your open tabs)',
    cookies: 'Read this page\'s cookies, including httpOnly session tokens (sensitive)',
    tab_control: 'Close tabs or stop browser sessions managed by the Agent',
    read_downloads: 'Read downloads history (access list of downloaded files and local paths)',
    page_script: 'Run custom JavaScript in the current page',
    page_screenshot: 'Capture visual screenshot or DOM tree snapshots of the page',
    page_input: 'Type text or send keyboard shortcuts to the current page',
    page_action: 'Click, drag, select controls, or set file uploads in the current page',
    page_logs: 'Read console logs and network request summaries of the current page',
    policy_admin: 'Modify local security policy (allow/block URLs or RPC methods)',
    recording_data: 'Read, stop, export, or delete recorded page action history (may include step screenshots)'
  };
  return { en: descriptions[category] || category };
}

let initialized = false;
let latestBridgeEnabled = false;

initializePanel();

function initializePanel() {
  if (initialized) return;
  initialized = true;

  // Load settings on open
  chrome.runtime.sendMessage({ type: 'GET_CSP_BYPASS' }).then(response => {
    if (response && 'enabled' in response) {
      bypassCspEl.checked = response.enabled;
    }
  });

  // Load bridge port on open
  chrome.storage.local.get('bridgePort').then(response => {
    if (response && 'bridgePort' in response) {
      bridgePortEl.value = response.bridgePort;
    } else {
      bridgePortEl.value = 8765;
    }
  });

  // Load reading settings on open
  chrome.storage.local.get(['enableRuntimeApproval']).then(response => {
    enableRuntimeApprovalEl.checked = response.enableRuntimeApproval !== false;
  });

  // Update settings when toggled
  bypassCspEl.addEventListener('change', async () => {
    await chrome.runtime.sendMessage({
      type: 'SET_CSP_BYPASS',
      enabled: bypassCspEl.checked
    });
  });

  // System permissions requested dynamically in agreeBtn handler

  enableRuntimeApprovalEl.addEventListener('change', async () => {
    await chrome.storage.local.set({ enableRuntimeApproval: enableRuntimeApprovalEl.checked });
  });

  savePortBtn.addEventListener('click', async () => {
    const port = parseInt(bridgePortEl.value, 10);
    if (Number.isInteger(port) && port >= 1024 && port <= 65535) {
      await chrome.storage.local.set({ bridgePort: port });
      chrome.runtime.reload();
    } else {
      alert('Please enter a valid port number between 1024 and 65535.');
    }
  });

  permAllowBtn.addEventListener('click', () => resolveCurrentPrompt('allow'));
  permSessionAllowBtn.addEventListener('click', () => resolveCurrentPrompt('session_allow'));
  permDenyBtn.addEventListener('click', () => resolveCurrentPrompt('deny'));

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'PING_SIDEPANEL') {
      sendResponse({ ok: true });
      return true;
    }
    if (message?.type === 'NATIVE_STATUS_CHANGED') {
      renderStatus(message.status);
    }
    if (message?.type === 'PROMPT_PERMISSION') {
      const labels = getCategoryLabel(message.category);
      activePrompts.push({
        promptId: message.promptId,
        category: message.category,
        method: message.method,
        params: message.params,
        labels
      });
      showNextPrompt();
    }
  });

  refresh();
}

async function refresh() {
  const response = await chrome.runtime.sendMessage({ type: 'GET_NATIVE_STATUS' });
  renderStatus(response.status);
}

function renderStatus(status) {
  const connected = status?.state === 'connected';
  const stopped = status?.state === 'stopped';
  latestBridgeEnabled = status?.bridgeEnabled === true;
  statusEl.textContent = connected ? 'Connected' : stopped ? 'Stopped' : 'Disconnected';
  statusEl.classList.toggle('connected', connected);
  statusEl.classList.toggle('disconnected', !connected);
  hostNameEl.textContent = status?.hostName || '-';
  lastCheckedEl.textContent = status?.lastChecked ? new Date(status.lastChecked).toLocaleString() : '-';
  errorEl.textContent = status?.error || '-';
  bridgeToggleBtn.disabled = false;
  bridgeToggleBtn.textContent = latestBridgeEnabled ? 'Stop Bridge' : 'Start Bridge';
  bridgeToggleBtn.classList.toggle('primary-btn', !latestBridgeEnabled);
  bridgeToggleBtn.classList.toggle('danger-btn', latestBridgeEnabled);
}
