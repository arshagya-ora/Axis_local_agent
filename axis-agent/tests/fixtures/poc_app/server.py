#!/usr/bin/env python3
"""A small, self-contained local test-fixture web application for the
"End-to-End Long-Running Browser Agent POC Validation" scenario.

No such application existed anywhere in this repository, so this one was
built to exercise every phase of that scenario (profile/onboarding form,
component-state validation, file upload, drag-and-drop task board, async
events, controlled diagnostics, and a final summary/audit page) using only
the Python standard library — no extra dependencies, no build step.

Run it with:

    python server.py [--host 127.0.0.1] [--port 8990]

Then open http://127.0.0.1:8990/ in the Chrome tab you bind through the
Browser Agent Bridge. See README.md in this directory for how to fill in
the POC's runtime placeholders (TEST_APP_URL, EXPECTED_TITLE, etc.) once
this is running.

One thing this fixture deliberately does NOT do: trigger a real native
`window.alert()`/`confirm()`/`prompt()` dialog. The Axis browser agent's
seven tools never call `page.acceptDialog` / `page.dismissDialog` (they
aren't in `browser_agent_tools.py`'s allowed-method set), so a real native
dialog would block the tab's main thread forever with no way for the agent
to clear it. The /async-lab page explains this instead of offering a
button that would hang the session.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# =====================================================================
# Shared page chrome
# =====================================================================

NAV_LINKS = [
    ("/", "Dashboard"),
    ("/profile", "Profile"),
    ("/components", "Components"),
    ("/upload", "Upload"),
    ("/board", "Task Board"),
    ("/async-lab", "Async Lab"),
    ("/diagnostics", "Diagnostics"),
    ("/summary", "Summary"),
]

STYLE = """
  :root { color-scheme: light; --fg:#1b1f24; --bg:#ffffff; --muted:#6b7280; --accent:#2563eb; --border:#d7dbe0; --ok:#16a34a; --bad:#dc2626; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; color:var(--fg); background:var(--bg); }
  header.site { border-bottom:1px solid var(--border); padding:12px 20px; display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
  header.site nav { display:flex; gap:14px; flex-wrap:wrap; }
  header.site nav a { color:var(--muted); text-decoration:none; font-size:14px; }
  header.site nav a[aria-current="page"] { color:var(--accent); font-weight:600; }
  main { max-width:900px; margin:0 auto; padding:24px 20px 80px; }
  h1 { font-size:22px; }
  h2 { font-size:17px; margin-top:32px; }
  label { display:block; font-size:13px; color:var(--muted); margin:14px 0 4px; }
  input[type=text], input[type=email], input[type=file], select { width:100%; max-width:360px; padding:8px 10px; border:1px solid var(--border); border-radius:6px; font-size:14px; }
  button { padding:8px 16px; border-radius:6px; border:1px solid var(--border); background:#f4f5f7; cursor:pointer; font-size:14px; }
  button:disabled { opacity:0.5; cursor:not-allowed; }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  .checkbox-row { display:flex; align-items:center; gap:8px; margin:12px 0; }
  .checkbox-row label { margin:0; color:var(--fg); font-size:14px; }
  .hint { color:var(--muted); font-size:13px; }
  .success { color:var(--ok); font-weight:600; margin-top:14px; }
  table { border-collapse:collapse; width:100%; margin-top:10px; }
  th, td { text-align:left; border-bottom:1px solid var(--border); padding:8px 6px; font-size:14px; }
  .tooltip-wrap { position:relative; display:inline-block; }
  .tooltip-bubble { display:none; position:absolute; left:0; top:26px; width:260px; background:#111827; color:#fff; padding:10px 12px; border-radius:6px; font-size:13px; z-index:5; }
  .tooltip-bubble.visible { display:block; }
  .help-icon { border-radius:50%; width:20px; height:20px; display:inline-flex; align-items:center; justify-content:center; background:#e5e7eb; font-size:12px; cursor:default; }
  #hidden-box { display:none; }
  #hidden-box.shown { display:block; }
  ul#component-list { padding-left:18px; }
  .board-spacer { height:1400px; display:flex; align-items:flex-end; padding-bottom:24px; }
  #board-container { display:flex; gap:16px; overflow-x:auto; width:900px; max-width:100%; border:1px solid var(--border); padding:14px; border-radius:8px; }
  .board-column { min-width:350px; background:#f8f9fb; border:1px solid var(--border); border-radius:8px; padding:10px; }
  .board-column h3 { margin:0 0 8px; font-size:14px; }
  .card-list { min-height:120px; }
  .board-card { background:#fff; border:1px solid var(--border); border-radius:6px; padding:10px; margin-bottom:8px; cursor:grab; user-select:none; font-size:13px; }
  .board-card.dragging { opacity:0.6; }
  .count-badge { font-size:12px; color:var(--muted); }
  .limitation-box { border:1px dashed var(--bad); background:#fef2f2; padding:14px; border-radius:8px; font-size:14px; }
  code { background:#f1f2f4; padding:1px 5px; border-radius:4px; font-size:13px; }
"""


def nav_html(active_path: str) -> str:
    items = []
    for href, label in NAV_LINKS:
        current = ' aria-current="page"' if href == active_path else ""
        items.append('<a href="' + href + '"' + current + ">" + label + "</a>")
    return "<nav>" + "".join(items) + "</nav>"


def page(title: str, active_path: str, body_html: str, extra_head: str = "") -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>" + title + "</title>"
        "<style>" + STYLE + "</style>" + extra_head +
        "</head><body>"
        "<header class=\"site\"><strong>Axis POC Test Harness</strong>" + nav_html(active_path) + "</header>"
        "<main>" + body_html + "</main>"
        "</body></html>"
    )


# =====================================================================
# Shared client-side helpers, injected on every page that needs them
# =====================================================================

RUN_ID_HELPERS_JS = """
function axisReadRunId() {
  try {
    var profileRaw = localStorage.getItem('axisPoc.profile');
    if (profileRaw) {
      var profile = JSON.parse(profileRaw);
      if (profile && profile.runId) return profile.runId;
    }
  } catch (e) {}
  return localStorage.getItem('axisPoc.currentRunId') || 'unassigned';
}
"""


# =====================================================================
# Page bodies
# =====================================================================

def home_page() -> str:
    body = """
      <h1 id="main-heading">Axis POC Test Harness</h1>
      <p class="hint">A local test-fixture application for validating the Axis browsing agent end to end. Nothing here is a production system; no real accounts, payments, or messages are involved.</p>
      <main id="dashboard-region" role="region" aria-label="Dashboard">
        <p>Use the navigation above to reach each phase's fixture:</p>
        <ul>
          <li><a href="/profile">Profile</a> &mdash; onboarding form (Phase 3)</li>
          <li><a href="/components">Components</a> &mdash; element-state validation (Phase 4)</li>
          <li><a href="/upload">Upload</a> &mdash; file upload (Phase 5)</li>
          <li><a href="/board">Task Board</a> &mdash; drag-and-drop and scrolling (Phase 6)</li>
          <li><a href="/async-lab">Async Lab</a> &mdash; delayed text/navigation/popup/dialog (Phase 7)</li>
          <li><a href="/page-two">Page Two</a> &mdash; history navigation target (Phase 8)</li>
          <li><a href="/diagnostics">Diagnostics</a> &mdash; controlled console/network events (Phase 9)</li>
          <li><a href="/summary">Summary</a> &mdash; final business-state audit (Phase 12)</li>
        </ul>
      </main>
    """
    return page("Axis POC Test Harness", "/", body)


def page_two_page() -> str:
    body = """
      <h1 id="page-two-heading">Page Two</h1>
      <p>This is a second, distinct page used to validate browser history navigation (back/forward/reload).</p>
      <p><a href="/">Return to dashboard</a></p>
    """
    return page("Axis POC Test Harness — Page Two", "/page-two", body)


def profile_page() -> str:
    body = """
      <h1>Profile &amp; Preferences</h1>
      <p class="hint">Display name and email should follow the run-specific pattern given in the POC instructions so later phases can correlate records to this run.</p>
      <form id="profile-form" novalidate>
        <label for="display-name">Display name</label>
        <input type="text" id="display-name" name="displayName" autocomplete="off">

        <label for="email">Email</label>
        <input type="email" id="email" name="email" autocomplete="off">

        <label for="department">Department</label>
        <select id="department" name="department">
          <option value="Engineering">Engineering</option>
          <option value="Sales">Sales</option>
          <option value="Support">Support</option>
          <option value="Product">Product</option>
        </select>

        <label for="region">Region</label>
        <select id="region" name="region">
          <option value="India">India</option>
          <option value="United States">United States</option>
          <option value="Germany">Germany</option>
          <option value="Japan">Japan</option>
        </select>

        <label for="notification-pref">Notification preference</label>
        <select id="notification-pref" name="notificationPref">
          <option value="Email">Email</option>
          <option value="SMS">SMS</option>
          <option value="Push">Push</option>
        </select>

        <label for="theme-pref">Theme</label>
        <select id="theme-pref" name="themePref">
          <option value="Light">Light</option>
          <option value="Dark">Dark</option>
        </select>

        <div class="checkbox-row">
          <input type="checkbox" id="marketing-opt" name="marketingOpt">
          <label for="marketing-opt">Subscribe to marketing emails (optional)</label>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" id="consent" name="consent">
          <label for="consent">I agree to the test consent terms</label>
          <span class="tooltip-wrap">
            <button type="button" id="help-icon" class="help-icon" aria-label="Help about consent">?</button>
            <span id="help-tooltip" class="tooltip-bubble" role="tooltip">Your test profile is only stored locally in this browser for this proof of concept and is never sent anywhere else.</span>
          </span>
        </div>

        <p style="margin-top:20px;">
          <button type="submit" id="submit-profile" class="primary">Save profile</button>
        </p>
      </form>
      <p id="profile-success" class="success" style="display:none;"></p>
    """
    extra_head = "<script>" + RUN_ID_HELPERS_JS + """
      document.addEventListener('DOMContentLoaded', function () {
        var helpIcon = document.getElementById('help-icon');
        var tooltip = document.getElementById('help-tooltip');
        function showTooltip() { tooltip.classList.add('visible'); }
        function hideTooltip() { tooltip.classList.remove('visible'); }
        helpIcon.addEventListener('mouseenter', showTooltip);
        helpIcon.addEventListener('focus', showTooltip);
        helpIcon.addEventListener('mouseleave', hideTooltip);
        helpIcon.addEventListener('blur', hideTooltip);
        document.addEventListener('keydown', function (e) {
          if (e.key === 'Escape' && tooltip.classList.contains('visible')) hideTooltip();
        });

        document.getElementById('profile-form').addEventListener('submit', function (e) {
          e.preventDefault();
          var displayName = document.getElementById('display-name').value.trim();
          var email = document.getElementById('email').value.trim();
          var department = document.getElementById('department').value;
          var region = document.getElementById('region').value;
          var notificationPref = document.getElementById('notification-pref').value;
          var themePref = document.getElementById('theme-pref').value;
          var marketingOpt = document.getElementById('marketing-opt').checked;
          var consent = document.getElementById('consent').checked;
          if (!displayName || !email || !consent) {
            document.getElementById('profile-success').style.display = 'none';
            return;
          }
          var match = email.match(/^axis-poc-(.+)@example\\.com$/);
          var runId = match ? match[1] : displayName;
          var profile = {
            displayName: displayName, email: email, department: department, region: region,
            notificationPref: notificationPref, themePref: themePref, marketingOpt: marketingOpt,
            consent: consent, runId: runId, savedAt: new Date().toISOString()
          };
          localStorage.setItem('axisPoc.profile', JSON.stringify(profile));
          localStorage.setItem('axisPoc.currentRunId', runId);
          var successEl = document.getElementById('profile-success');
          successEl.textContent = 'Profile saved for ' + displayName + ' (' + email + ')';
          successEl.style.display = 'block';
        });
      });
    """ + "</script>"
    return page("Axis POC Test Harness — Profile", "/profile", body, extra_head)


def components_page() -> str:
    body = """
      <h1>Component States</h1>
      <p class="hint">Static fixtures for visibility, enabled/disabled, attributes, and repeated rows.</p>

      <h2>Visibility</h2>
      <div id="visible-box">Visible component</div>
      <div id="hidden-box">Hidden component revealed</div>
      <p>
        <button type="button" id="show-btn">Show hidden component</button>
        <button type="button" id="hide-btn">Hide hidden component</button>
      </p>

      <h2>Enabled / disabled</h2>
      <p>
        <button type="button" id="enabled-btn">Enabled action</button>
        <button type="button" id="disabled-btn" disabled>Disabled action</button>
      </p>

      <h2>Stable attribute</h2>
      <div id="stable-widget" data-testid="stable-widget" data-status="ready">Stable Widget</div>

      <h2>Repeated rows</h2>
      <ul id="component-list">
        <li class="component-row">Row 1</li>
        <li class="component-row">Row 2</li>
        <li class="component-row">Row 3</li>
        <li class="component-row">Row 4</li>
        <li class="component-row">Row 5</li>
      </ul>
    """
    extra_head = """<script>
      document.addEventListener('DOMContentLoaded', function () {
        var hiddenBox = document.getElementById('hidden-box');
        document.getElementById('show-btn').addEventListener('click', function () { hiddenBox.classList.add('shown'); });
        document.getElementById('hide-btn').addEventListener('click', function () { hiddenBox.classList.remove('shown'); });
      });
    </script>"""
    return page("Axis POC Test Harness — Components", "/components", body, extra_head)


def upload_page() -> str:
    body = """
      <h1>File Upload</h1>
      <p class="hint">Choose the approved file supplied for this run and upload it.</p>
      <input type="file" id="upload-input">
      <p><button type="button" id="upload-btn" class="primary">Upload</button></p>
      <p id="upload-status" class="hint"></p>
      <table>
        <thead><tr><th>Filename</th><th>Run ID</th><th>Uploaded at</th></tr></thead>
        <tbody id="uploaded-files-body"></tbody>
      </table>
    """
    extra_head = "<script>" + RUN_ID_HELPERS_JS + """
      function axisRenderUploads() {
        var body = document.getElementById('uploaded-files-body');
        body.innerHTML = '';
        var uploads = [];
        try { uploads = JSON.parse(localStorage.getItem('axisPoc.uploads') || '[]'); } catch (e) {}
        uploads.forEach(function (u) {
          var tr = document.createElement('tr');
          tr.className = 'uploaded-file-row';
          tr.setAttribute('data-run-id', u.runId);
          tr.innerHTML = '<td>' + u.filename + '</td><td>' + u.runId + '</td><td>' + u.uploadedAt + '</td>';
          body.appendChild(tr);
        });
      }
      document.addEventListener('DOMContentLoaded', function () {
        axisRenderUploads();
        var input = document.getElementById('upload-input');
        var btn = document.getElementById('upload-btn');
        // No disabled-gating here on purpose: the button stays always
        // clickable and the click handler below validates file presence
        // itself, so upload-ability never depends on any particular DOM
        // event (change/input) actually firing after a file is set.
        btn.addEventListener('click', function () {
          if (!input.files || input.files.length === 0) return;
          var file = input.files[0];
          var formData = new FormData();
          formData.append('file', file);
          document.getElementById('upload-status').textContent = 'Uploading ' + file.name + '...';
          fetch('/api/upload', { method: 'POST', body: formData })
            .then(function (res) { return res.json(); })
            .then(function (data) {
              var runId = axisReadRunId();
              var uploads = [];
              try { uploads = JSON.parse(localStorage.getItem('axisPoc.uploads') || '[]'); } catch (e) {}
              uploads.push({ filename: data.filename, size: data.size, runId: runId, uploadedAt: new Date().toISOString() });
              localStorage.setItem('axisPoc.uploads', JSON.stringify(uploads));
              axisRenderUploads();
              document.getElementById('upload-status').textContent = 'Uploaded ' + data.filename;
            })
            .catch(function () {
              document.getElementById('upload-status').textContent = 'Upload failed';
            });
        });
      });
    """ + "</script>"
    return page("Axis POC Test Harness — Upload", "/upload", body, extra_head)


def board_page() -> str:
    body = """
      <div class="board-spacer"><h1>Scroll down to view the task board</h1></div>
      <h2>Task Board</h2>
      <p class="hint">Drag the run's card from "To Do" into "Done". Scroll the board horizontally to see every column.</p>
      <div id="board-container">
        <div class="board-column" id="column-todo" data-column="todo">
          <h3>To Do <span class="count-badge" id="todo-count">0</span></h3>
          <div class="card-list" id="todo-list" data-dropzone="todo"></div>
        </div>
        <div class="board-column" id="column-in-progress" data-column="in-progress">
          <h3>In Progress <span class="count-badge" id="in-progress-count">2</span></h3>
          <div class="card-list" id="in-progress-list" data-dropzone="in-progress">
            <div class="board-card" data-run-id="sample">Sample Task A</div>
            <div class="board-card" data-run-id="sample">Sample Task B</div>
          </div>
        </div>
        <div class="board-column" id="column-done" data-column="done">
          <h3>Done <span class="count-badge" id="done-count">0</span></h3>
          <div class="card-list" id="done-list" data-dropzone="done"></div>
        </div>
      </div>
    """
    extra_head = "<script>" + RUN_ID_HELPERS_JS + """
      function axisSaveBoardState() {
        var state = {};
        ['todo', 'in-progress', 'done'].forEach(function (col) {
          state[col] = Array.prototype.map.call(
            document.querySelectorAll('#' + col + '-list .board-card'),
            function (el) { return el.getAttribute('data-run-id'); }
          );
        });
        localStorage.setItem('axisPoc.board', JSON.stringify(state));
      }
      function axisUpdateCounts() {
        ['todo', 'in-progress', 'done'].forEach(function (col) {
          var count = document.querySelectorAll('#' + col + '-list .board-card').length;
          document.getElementById(col + '-count').textContent = String(count);
        });
      }
      document.addEventListener('DOMContentLoaded', function () {
        var runId = axisReadRunId();
        var state = {};
        try { state = JSON.parse(localStorage.getItem('axisPoc.board') || '{}'); } catch (e) {}
        var alreadyPresent = Object.keys(state).some(function (col) {
          return Array.isArray(state[col]) && state[col].indexOf(runId) !== -1;
        });
        if (alreadyPresent) {
          ['todo', 'in-progress', 'done'].forEach(function (col) {
            if (col === 'in-progress') return;
            (state[col] || []).forEach(function (id) {
              if (id === runId) {
                var card = document.createElement('div');
                card.className = 'board-card';
                card.setAttribute('data-run-id', runId);
                card.textContent = 'Card ' + runId;
                document.getElementById(col + '-list').appendChild(card);
              }
            });
          });
        } else {
          var card = document.createElement('div');
          card.className = 'board-card';
          card.setAttribute('data-run-id', runId);
          card.textContent = 'Card ' + runId;
          document.getElementById('todo-list').appendChild(card);
        }
        axisUpdateCounts();
        axisSaveBoardState();

        var dragState = null;
        document.addEventListener('mousedown', function (e) {
          var card = e.target.closest('.board-card');
          if (!card) return;
          dragState = { card: card };
          card.classList.add('dragging');
        });
        document.addEventListener('mouseup', function (e) {
          if (!dragState) return;
          // Hit-test the whole column, not just the (possibly empty) card-list
          // div itself: .card-list is a SIBLING of the column's <h3> header
          // and count badge, not their ancestor, so a drop that lands on the
          // visible column label (the most natural drag target) would
          // otherwise silently miss and never move the card.
          var dropEl = document.elementFromPoint(e.clientX, e.clientY);
          var column = dropEl ? dropEl.closest('.board-column') : null;
          var dropzone = column ? column.querySelector('.card-list') : null;
          if (dropzone) dropzone.appendChild(dragState.card);
          dragState.card.classList.remove('dragging');
          dragState = null;
          axisUpdateCounts();
          axisSaveBoardState();
        });
      });
    """ + "</script>"
    return page("Axis POC Test Harness — Task Board", "/board", body, extra_head)


def async_lab_page() -> str:
    body = """
      <h1>Asynchronous Event Lab</h1>

      <h2>Delayed text</h2>
      <p><button type="button" id="trigger-delayed-text">Trigger delayed text (1.5s)</button></p>
      <p id="delayed-text-result" class="hint"></p>

      <h2>Delayed navigation</h2>
      <p><button type="button" id="trigger-delayed-nav">Trigger delayed navigation (1.5s)</button></p>

      <h2>Popup</h2>
      <p><button type="button" id="trigger-popup">Trigger delayed popup (1.5s)</button></p>

      <h2>Dialog</h2>
      <div class="limitation-box">
        This fixture intentionally does not trigger a real native
        <code>window.alert()</code>/<code>confirm()</code>/<code>prompt()</code>
        dialog. The Axis browser agent's seven tools never call
        <code>page.acceptDialog</code> or <code>page.dismissDialog</code> (they
        are not in <code>browser_agent_tools.py</code>'s allowed bridge
        methods), so a real dialog would block this tab's main thread
        indefinitely with no way for the agent to clear it. Record this as a
        documented, expected fixture limitation rather than attempting it.
      </div>
    """
    extra_head = """<script>
      document.addEventListener('DOMContentLoaded', function () {
        document.getElementById('trigger-delayed-text').addEventListener('click', function () {
          setTimeout(function () {
            document.getElementById('delayed-text-result').textContent = 'Delayed event completed';
          }, 1500);
        });
        document.getElementById('trigger-delayed-nav').addEventListener('click', function () {
          setTimeout(function () { window.location.href = '/async-lab/target'; }, 1500);
        });
        document.getElementById('trigger-popup').addEventListener('click', function () {
          setTimeout(function () { window.open('/popup-target', 'axisPocPopup'); }, 1500);
        });
      });
    </script>"""
    return page("Axis POC Test Harness — Async Lab", "/async-lab", body, extra_head)


def async_target_page() -> str:
    body = """
      <h1>Async Target Reached</h1>
      <p>This page is the destination of the async-lab's delayed navigation fixture.</p>
      <p><a href="/async-lab">Back to Async Lab</a></p>
    """
    return page("Axis POC Test Harness — Async Target", "/async-lab", body)


def popup_target_page() -> str:
    body = """
      <h1>Popup Target Loaded</h1>
      <p>This page is opened by the async-lab's delayed popup fixture.</p>
    """
    return page("Axis POC Test Harness — Popup Target", "/async-lab", body)


def diagnostics_page() -> str:
    body = """
      <h1>Controlled Diagnostics</h1>
      <p class="hint">Every message and request here is a deliberate, harmless test fixture &mdash; nothing here is a real secret.</p>

      <h2>Console events</h2>
      <p><button type="button" id="log-console">Log info / warning / error</button></p>
      <p id="console-status" class="hint"></p>

      <h2>Network events</h2>
      <p><button type="button" id="fire-requests">Fire success / failure / redaction-check requests</button></p>
      <p id="network-status" class="hint"></p>
    """
    extra_head = """<script>
      document.addEventListener('DOMContentLoaded', function () {
        document.getElementById('log-console').addEventListener('click', function () {
          console.info('Axis POC info: diagnostics check');
          console.warn('Axis POC warning: diagnostics check');
          console.error('Axis POC error: diagnostics check');
          document.getElementById('console-status').textContent = 'Console events logged';
        });
        document.getElementById('fire-requests').addEventListener('click', function () {
          document.getElementById('network-status').textContent = 'Requests in flight...';
          var ping = fetch('/api/ping').catch(function () {});
          var fail = fetch('/api/fail').catch(function () {});
          var echo = fetch('/api/diagnostics-echo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer axis-poc-dummy-token' },
            body: JSON.stringify({
              password: 'dummy-password-not-real',
              access_token: 'dummy-access-token',
              refresh_token: 'dummy-refresh-token',
              api_key: 'dummy-api-key',
              secret: 'dummy-secret-value'
            })
          }).catch(function () {});
          Promise.all([ping, fail, echo]).then(function () {
            document.getElementById('network-status').textContent = 'Requests completed';
          });
        });
      });
    </script>"""
    return page("Axis POC Test Harness — Diagnostics", "/diagnostics", body, extra_head)


def summary_page() -> str:
    body = """
      <h1 id="summary-heading" data-status="final">Final Summary</h1>

      <h2>Profile</h2>
      <table>
        <tbody id="summary-profile-body"></tbody>
      </table>

      <h2>Uploaded files (<span id="summary-upload-count">0</span>)</h2>
      <table>
        <thead><tr><th>Filename</th><th>Run ID</th></tr></thead>
        <tbody id="summary-uploads-body"></tbody>
      </table>

      <h2>Task board</h2>
      <p id="summary-board-status" class="hint">No board state recorded yet.</p>

      <h2>Async lab</h2>
      <p id="summary-async-status" class="hint">No async events recorded yet.</p>
    """
    extra_head = "<script>" + RUN_ID_HELPERS_JS + """
      document.addEventListener('DOMContentLoaded', function () {
        var runId = axisReadRunId();

        var profile = null;
        try { profile = JSON.parse(localStorage.getItem('axisPoc.profile') || 'null'); } catch (e) {}
        var profileBody = document.getElementById('summary-profile-body');
        if (profile) {
          var rows = [
            ['Display name', profile.displayName],
            ['Email', profile.email],
            ['Department', profile.department],
            ['Region', profile.region],
            ['Notification preference', profile.notificationPref],
            ['Theme', profile.themePref],
            ['Marketing subscription', profile.marketingOpt ? 'Enabled' : 'Disabled'],
            ['Consent', profile.consent ? 'Enabled' : 'Disabled'],
            ['Run ID', profile.runId]
          ];
          profileBody.innerHTML = rows.map(function (r) {
            return '<tr><th>' + r[0] + '</th><td class="summary-value">' + r[1] + '</td></tr>';
          }).join('');
        } else {
          profileBody.innerHTML = '<tr><td colspan="2" class="hint">No profile saved yet.</td></tr>';
        }

        var uploads = [];
        try { uploads = JSON.parse(localStorage.getItem('axisPoc.uploads') || '[]'); } catch (e) {}
        var runUploads = uploads.filter(function (u) { return u.runId === runId; });
        document.getElementById('summary-upload-count').textContent = String(runUploads.length);
        document.getElementById('summary-uploads-body').innerHTML = runUploads.map(function (u) {
          return '<tr class="summary-upload-row"><td>' + u.filename + '</td><td>' + u.runId + '</td></tr>';
        }).join('') || '<tr><td colspan="2" class="hint">No files uploaded yet.</td></tr>';

        var board = {};
        try { board = JSON.parse(localStorage.getItem('axisPoc.board') || '{}'); } catch (e) {}
        var boardColumn = null;
        Object.keys(board).forEach(function (col) {
          if (Array.isArray(board[col]) && board[col].indexOf(runId) !== -1) boardColumn = col;
        });
        document.getElementById('summary-board-status').textContent = boardColumn
          ? ('Run card is in column: ' + boardColumn)
          : 'Run card not found on the board yet.';

        var asyncStatus = [];
        if (localStorage.getItem('axisPoc.asyncTextDone') === 'true') asyncStatus.push('delayed text');
        if (localStorage.getItem('axisPoc.asyncNavVisited') === 'true') asyncStatus.push('delayed navigation');
        if (localStorage.getItem('axisPoc.popupOpened') === 'true') asyncStatus.push('popup');
        document.getElementById('summary-async-status').textContent = asyncStatus.length
          ? ('Completed: ' + asyncStatus.join(', '))
          : 'No async events recorded yet.';
      });
    """ + "</script>"
    return page("Axis POC Test Harness — Summary", "/summary", body, extra_head)


PAGES = {
    "/": home_page,
    "/page-two": page_two_page,
    "/profile": profile_page,
    "/components": components_page,
    "/upload": upload_page,
    "/board": board_page,
    "/async-lab": async_lab_page,
    "/async-lab/target": async_target_page,
    "/popup-target": popup_target_page,
    "/diagnostics": diagnostics_page,
    "/summary": summary_page,
}

# Small on-load flags used by the summary page, injected into the two async
# destination pages so localStorage records that they were actually reached
# (same-origin localStorage is shared across tabs/pages automatically).
_ASYNC_FLAG_SCRIPT = {
    "/async-lab/target": "<script>localStorage.setItem('axisPoc.asyncNavVisited', 'true');</script>",
    "/popup-target": "<script>localStorage.setItem('axisPoc.popupOpened', 'true');</script>",
}


# =====================================================================
# Minimal multipart/form-data parsing (upload endpoint only needs the
# first file part's filename and byte length — not a general-purpose
# parser).
# =====================================================================

def parse_multipart_filename(content_type: str, body: bytes) -> tuple[str | None, int]:
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return None, 0
    boundary = ("--" + match.group(1)).encode("utf-8")
    parts = body.split(boundary)
    for part in parts:
        if b"Content-Disposition" not in part:
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        headers = part[:header_end].decode("utf-8", errors="replace")
        filename_match = re.search(r'filename="([^"]*)"', headers)
        if not filename_match or not filename_match.group(1):
            continue
        file_bytes = part[header_end + 4:]
        if file_bytes.endswith(b"\r\n"):
            file_bytes = file_bytes[:-2]
        return filename_match.group(1), len(file_bytes)
    return None, 0


# =====================================================================
# HTTP handler
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "AxisPocFixture/1.0"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path
        if path == "/api/ping":
            self._send_json({"status": "ok", "ts": time.time()})
            return
        if path == "/api/fail":
            self._send_json({"error": "Simulated controlled failure for Axis POC diagnostics"}, status=500)
            return
        builder = PAGES.get(path)
        if builder is None:
            self._send_html(page("Axis POC Test Harness — Not Found", "", "<h1>404 Not Found</h1><p><a href=\"/\">Back to dashboard</a></p>"), status=404)
            return
        html = builder()
        flag_script = _ASYNC_FLAG_SCRIPT.get(path)
        if flag_script:
            html = html.replace("</body>", flag_script + "</body>")
        self._send_html(html)

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""

        if path == "/api/upload":
            content_type = self.headers.get("Content-Type", "")
            filename, size = parse_multipart_filename(content_type, body)
            if not filename:
                self._send_json({"error": "no file part found"}, status=400)
                return
            self._send_json({"filename": filename, "size": size, "status": "ok"})
            return

        if path == "/api/diagnostics-echo":
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                parsed = {}
            # Deliberate, dummy-valued fixture fields — never real secrets —
            # used only to verify the agent's tool layer redacts sensitive
            # keys in captured request/response headers and bodies.
            self._send_json(
                {"received": parsed, "status": "ok"},
                extra_headers={"Set-Cookie": "axis_poc_session=dummy-session-value; Path=/"},
            )
            return

        self._send_json({"error": "not found"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8990)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Axis POC test-fixture app running at http://{args.host}:{args.port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
