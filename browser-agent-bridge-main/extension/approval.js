const permissionDetails = document.querySelector('#permission-details');
const permAllowBtn = document.querySelector('#perm-allow-btn');
const permSessionAllowBtn = document.querySelector('#perm-session-allow-btn');
const permDenyBtn = document.querySelector('#perm-deny-btn');

let activePrompts = [];
let currentPrompt = null;
let initialPromptsLoaded = false;

function appendLabeledValue(parent, label, value, valueTag = 'span') {
  const strong = document.createElement('strong');
  strong.textContent = label;
  parent.appendChild(strong);
  const valueEl = document.createElement(valueTag);
  valueEl.textContent = value;
  parent.appendChild(valueEl);
  parent.appendChild(document.createElement('br'));
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
  return descriptions[category] || category;
}

function renderPrompt(prompt) {
  const description = getCategoryLabel(prompt.category);
  permissionDetails.textContent = '';
  appendLabeledValue(permissionDetails, 'Method: ', prompt.method, 'code');

  const descriptionLabel = document.createElement('strong');
  descriptionLabel.textContent = 'Description:';
  permissionDetails.appendChild(descriptionLabel);
  permissionDetails.appendChild(document.createElement('br'));
  permissionDetails.appendChild(document.createTextNode(description));
  permissionDetails.appendChild(document.createElement('br'));

  const paramsLabel = document.createElement('strong');
  paramsLabel.textContent = 'Params:';
  permissionDetails.appendChild(paramsLabel);
  const paramsPre = document.createElement('pre');
  paramsPre.className = 'permission-params';
  paramsPre.textContent = JSON.stringify(prompt.params, null, 2);
  permissionDetails.appendChild(paramsPre);
}

function showNextPrompt() {
  if (currentPrompt) return;
  currentPrompt = activePrompts.shift() || null;
  if (!currentPrompt) {
    permissionDetails.textContent = 'No pending approval requests.';
    if (initialPromptsLoaded) window.close();
    return;
  }
  renderPrompt(currentPrompt);
}

function enqueuePrompt(prompt) {
  if (currentPrompt?.promptId === prompt.promptId) return;
  if (activePrompts.some(item => item.promptId === prompt.promptId)) return;
  activePrompts.push(prompt);
  showNextPrompt();
}

async function resolveCurrentPrompt(responseValue) {
  if (!currentPrompt) return;
  const prompt = currentPrompt;
  currentPrompt = null;
  await chrome.runtime.sendMessage({
    type: 'PERMISSION_RESPONSE',
    promptId: prompt.promptId,
    response: responseValue
  }).catch(() => {});
  showNextPrompt();
}

permAllowBtn.addEventListener('click', () => resolveCurrentPrompt('allow'));
permSessionAllowBtn.addEventListener('click', () => resolveCurrentPrompt('session_allow'));
permDenyBtn.addEventListener('click', () => resolveCurrentPrompt('deny'));

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === 'PROMPT_PERMISSION') {
    enqueuePrompt({
      promptId: message.promptId,
      category: message.category,
      method: message.method,
      params: message.params
    });
  }
});

chrome.runtime.sendMessage({ type: 'GET_PENDING_PERMISSION_PROMPTS' }).then(response => {
  for (const prompt of response?.prompts || []) {
    enqueuePrompt(prompt);
  }
  initialPromptsLoaded = true;
  showNextPrompt();
}).catch(() => {});
