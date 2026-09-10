# AXIS browser workspace

The existing Manifest V3 extension now hosts the AXIS conversation, task activity,
history drawer, and shared panel/options Settings view. It uses packaged vanilla
JavaScript and CSS. The native browser RPC protocol and enforcement policy remain
in the bridge. Python owns execution, so closing the panel does not stop a task.

## Start and pair

1. Follow the existing root README for provider credentials and native-host setup.
   Reload the unpacked extension from `browser-agent-bridge-main/extension` after
   updating this checkout. Its identity and permission declarations are preserved.
2. From the repository root, run `uv sync`. Start the UI service **instead of** an
   interactive CLI against this runtime:

   ```powershell
   cd axis-agent
   uv run python -m axis.ui_service
   ```

3. Open AXIS → Settings → Connection → **Pair AXIS service**. The default service
   address is `http://127.0.0.1:8766`. Enter the `token` value from
   `axis-agent/.axis-ui/pairing.json`, then choose **Save and connect**. Keep this
   local credential private. It authorizes UI history and task controls; it is
   neither the browser RPC token nor a provider credential.
4. Choose **Start bridge** in Settings and approve Chrome's existing optional
   permissions when requested. The workspace says Connected only when both the
   AXIS service and native bridge are ready.

`--port`, `--config`, `--data-dir`, and `--extension-id` are available on the service.
The default extension ID is derived from the existing manifest key. Only loopback
binding is supported. The service checks the Host header, expected extension
origin, and bearer credential on commands, history reads, and event streams.
The credential never appears in an event URL. Pairing data and the SQLite store
are ignored by Git. Chrome stores the UI credential only in trusted extension
contexts. To rotate it, stop the service, remove its local pairing file, restart,
and pair the extension again.

The CLI remains `uv run python -m axis.cli ...`. An OS-held lock prevents the CLI
and UI service in this checkout from simultaneously owning the browser. Low-level
scripts that construct an orchestrator directly must coordinate their own use.

### Terminal debug output

Use the same debug flag as the CLI:

```powershell
cd D:\Products\AXIS\Axis-phase-3\axis-agent
uv run python -m axis.ui_service --debug
```

This reuses the CLI's formatter for planner/navigator outputs, browser tool
arguments, successful/failed/skipped actions, evidence and errors. Each returned
result includes the full answer, limitations, steps, planner passes, action/model
request counts, token usage and timing. Task IDs and operation headings distinguish
new tasks, follow-ups, resume and budget extension. Unexpected runner exceptions
include their traceback. Output is UTF-8 and flushed line by line for Windows and
redirected logs.

`--verbose` provides compact events and result totals. Without either flag, the
service stays quiet apart from startup and errors. Debug mode writes diagnostics
only to the local terminal; the panel's bounded activity, persisted display events
and approval rules are unchanged. It does not enable HTTP request-body logging or
provider SDK debug logging. Continue sending instructions through the extension.

## Attach files

Use **Attach files** or drag files onto the composer. AXIS reads XLSX, DOCX, PPTX,
searchable PDF, MD, TXT, and CSV files and can upload preserved originals to a
website during a task. Attach files and describe the goal: there is no required
per-file role selector. New files use `auto`; AXIS can read a file and upload its
preserved original in the same task. Uploads do not wait for extraction, while
required reads do. The UI shows processing status and removal controls.

The planner declares task-scoped uses with quoted user-request evidence. File
contents are reference data unless the user delegates instruction execution.
Requests to configure every entry require source-linked coverage and verified
workflow results. Legacy upload-only files remain restricted. Unreadable content
cannot support a summary; three unsuccessful document attempts stop with a
specific blocker, retaining the task context.

### Execution modes

The compact composer selector defaults to **Approval** and remembers the user's
preference. Each new task snapshots its mode. In both modes, inspection and local
preparation run automatically.

- **Approval:** before a browser change, review its exact destination, target,
  arguments/content, original-file IDs and verification condition. Approvals are
  action-sized batches, not blanket permission for the remainder of a task.
  Creating a remote draft, autosaving its fields, and uploading files are changes.
- **Automatic:** clearly scoped changes can proceed without the extra execution
  prompt. Ambiguous destinations or effects still require review. Browser
  permissions, mandatory configured approvals and prohibitions still apply.
  A draft-only request does not authorize sending.

Changing an active task's mode is an explicit UI action. It applies at the next
action boundary, invalidates outstanding approvals, and never undoes earlier
changes. If the change interrupts a waiting approval, resume the preserved task.
Stop invalidates approvals. A sidebar reconnect restores a pending prompt; a
service restart invalidates grants and marks live work interrupted. Decisions are
authenticated and idempotent; approval is consumed before execution, never replayed.

API additions are `execution_mode: approval | automatic` on message submission,
`PATCH /api/tasks/{id}/execution-mode`, and
`POST /api/tasks/{id}/approvals/{approval_id}` with `decision: approve | deny`.
Task JSON fields and the approval table are additive; old history stays readable.
Legacy tasks without a mode retain their existing runtime policy.

Interrupted tasks with a saved document workflow provide **Recover workflow**.
Recovery retains verified steps and reconciles the last running step with the
website. Ordinary tasks retain the restart behavior described below.
See the [attachment guide](../axis-agent/axis/attachments/README.md) for setup,
limits, retrieval, original-file uploads, and verification commands.

## Controls and continuity

- **New conversation** creates a separate workspace. **New task** creates fresh
  agent task state in the selected conversation. Only one task owns the runtime.
  Paused, needs-input, and exhausted tasks retain ownership until resumed or stopped.
- **Current task** explicitly targets the live task. One pending follow-up is
  accepted at a time. AXIS closes the action gate, waits for the running entry point
  to return, and then calls the existing continuation. The UI distinguishes
  accepted, applied, and not-applied instructions. Stop takes precedence.
- **Pause / Resume** retain identity, memory, tabs, evidence, counters, and budgets.
  Pausing and Stopping remain visible until the runner returns. In-flight browser
  calls and model calls can take time to finish. Stop closes the gate to further
  actions; it cannot undo an action that already ran. The UI service deliberately
  waits for synchronous tool completion rather than abandoning a worker thread.
- **Extend budget** calls the existing `extend_budget()` entry point with explicit
  additions to model requests, steps, and browser actions. The composer also accepts
  `/extend REQUESTS [STEPS ACTIONS]`. Extensions have durable request IDs so retries
  cannot grant the same budget twice. They preserve accumulated usage and source
  evidence. A follow-up alone never grants more budget.
- **Use saved outcome** starts a fresh task with at most 800 characters of the
  chosen outcome as context, bounded together with the new instruction. Historical
  tasks never continue an unrelated live orchestrator. For cancelled/interrupted
  work, restarting instead carries the original request, attachments, inferred
  uses, restrictions and verified workflow progress, with fresh approvals.
  A bare “continue the same thing” targets the latest cancelled/interrupted task
  in the current conversation when there is no live task.
- History supports title search, paging, load, rename, and delete. Deleting an
  owned conversation requires stopping its task first. Deletion removes the chat
  and events; request-ID tombstones prevent an old network retry from executing
  deleted work again.
- Panel closure and stream reconnection reload authoritative task snapshots.
  Unconfirmed submissions are checked against the service by request ID when
  reconnecting. Accepted submissions are cleared automatically, including those
  in another conversation or older history pages. Otherwise the composer shows
  the previous instruction and **Retry previous instruction**. Retry uses the
  original conversation, task target, and request ID and preserves a newer draft.
  Activity uses resumable authenticated fetch/SSE, sequence deduplication, and
  bounded batches. Slow subscribers receive a resync instruction. The rendered
  view retains at most 50 messages and 30 activity entries per loaded task; older
  pages remain available. Drafts, selected conversation, scroll, and card expansion
  are saved separately in extension storage.

Restarting Python restores history but marks owned work **Interrupted**. It does
not reconstruct live browser objects, continuation tokens, or model run state, and
never replays browser actions. For ordinary tasks, Resume and budget extension require the same live
Python process. Saved cited answers are retained as inert text, up to the agent's
64,000-character maximum; no private planner reasoning or raw page dumps are
forwarded as activity. Reported artifacts are descriptive records, not arbitrary
filesystem download links.

## Settings and approvals

Light, Dark, and System appearance have their own save action. The options tab
reuses the same settings code and Chrome storage. Model identity is read-only.
The finite editable allowlist is maximum steps, browser actions, and model
requests, validated through `AxisConfig` and persisted for future tasks only.

Runtime approval, bridge start/stop, and CSP bypass retain the existing bridge
messages. A port change requires an explicit reload acknowledgment and **Apply
port and reload**. The Python bridge port must then match. Appearance changes do
not reload the extension. Generic provider keys, OCI credentials, shell commands,
and arbitrary YAML are not UI settings.

The panel and fallback popup share approval rendering. Pending prompts are restored
through `GET_PENDING_PERMISSION_PROMPTS`; decisions use original prompt IDs and
existing allow/deny/session semantics. Approvals appear in the current panel view,
including Settings or History, and reconcile decisions made in another window.
Prompts without a trustworthy task association are labelled **Browser bridge
approval**. Agent-side configured gates retain their existing policy; the UI does
not create a blanket bypass. Task execution approvals described above are an
additional gate and cannot override bridge decisions.

## Sidebar polish

The sidebar uses a unified light/dark header, compact task cards, consistent spacing,
and quieter history controls. Task titles are not repeated inside the activity view,
and empty workflow counters are omitted. Cards place a plain status indicator and
timing above the current action, group activity into a quiet inset, and separate
task controls in the footer. The composer has one focus border, with
the attachment button, task selector, and send button in one bottom row; its hint
sits below the input surface. Running uses a mint badge; completed uses a neutral
badge with a green indicator. Existing task controls and conversation flows remain.

Expanded task activity is grouped into collapsible Planning, Browsing, Evidence,
and Result sections. Other lifecycle events appear under Activity. Each section
counts its loaded activity entries as steps (not model requests or browser actions),
and each entry shows its recorded local time. Older pages retain their paging
controls; counts describe only the loaded page. Section choices and keyboard focus
survive live updates while the panel is open. New events carry a bounded display
category, and older history uses known public event types and labels.

Completed tasks place their answer before a compact activity summary. Activity
collapses on completion and can be reopened. Assistant answers render a safe
Markdown subset: paragraphs, headings, emphasis, lists, quotations, code, and
HTTP(S) links. Rendering creates DOM nodes rather than inserting HTML; raw HTML
and unsupported link protocols remain text. Stored answer content is unchanged.

The HTML, JavaScript, and CSS were already valid UTF-8; the reported mojibake was
PowerShell decoding output incorrectly. Extension editor settings now explicitly
specify UTF-8. Source checks reject invalid UTF-8 and common mojibake sequences;
the browser check also asserts actual arrows, ellipses, separators, and apostrophes.

## Verification

From the repository root:

```powershell
uv run pytest -q --basetemp tmp/pytest-ui
node --test browser-agent-bridge-main/tests/*.mjs
uv run python axis-agent/scripts/ui_browser_check.py
uv run python axis-agent/scripts/ui_native_check.py
uv run python axis-agent/scripts/ui_fixture_smoke.py
uv run python axis-agent/scripts/attachment_fixture_check.py --executable PATH_TO_CHROMIUM
```

Create the `tmp` directory first if absent. Playwright is a development-only
dependency; install its Chromium with `uv run playwright install chromium`, or
pass `--executable PATH_TO_CHROMIUM` to the browser scripts to reuse an installed
binary. No frontend build step is needed.

The browser check loads the actual unpacked MV3 extension in a temporary profile,
uses a temporary authenticated service and a deterministic runner, and captures
workspace/history/settings at 320, 360, 400, and 480 pixels, at both 640 and 900
pixels tall. It also exercises 200% extension zoom, themes, shared options,
draft restoration, task controls, budget extension, and inert untrusted text.
The native check uses a disposable manifest copy with the existing optional
permissions pre-granted because headless Chrome cannot display its permission
dialog. Its worker, native host, approval routing, and all UI code are real; the
production manifest is unchanged. First-time Chrome permission consent remains a
manual browser-owned step. The real fixture smoke uses the configured provider
and bridge, with a firewall restricting it to local fixture tabs.

Screenshots and JSON evidence are written to `artifacts/axis-ui/`. Test records
never seed production conversations. UI screenshots from the deterministic test
are development fixtures, not claims of completed browser work.

The attachment fixture uses a scripted model, the real serialized UI runner and
approval endpoints, and a Playwright-backed local RPC adapter. It extracts a
PowerPoint, creates a local mail draft, autosaves its source-grounded summary,
and checks the uploaded original's SHA-256. Approval mode requires three exact
action approvals; Automatic needs none. Neither run sends. This is a local
regression test, not a test against a Gmail account or a live model provider.

### Delivery checklist

- [x] Phase 0: reconcile newer runtime; baseline 396 Python / 327 bridge tests.
- [x] Phase 1: responsive graphite/mint shell, drawer, shared options and bridge settings.
- [x] Phase 2: real HTTP/SSE task seam, correlated IDs, idempotency, single owner, Stop.
- [x] Phase 3: safe serialized follow-ups, public resume, explicit budget extension.
- [x] Phase 4: SQLite history, replay/paging, view restoration, interrupted-process status.
- [x] Phase 5: appearance, validated future limits, approval restoration, responsive checks.

The final test counts, screenshots and native-check results are recorded in the
[delivery report](AXIS_UI_DELIVERY.md).
Process-crash recovery and automatic replay are intentionally unsupported.

Implementation references: [Chrome side panel API](https://developer.chrome.com/docs/extensions/reference/api/sidePanel)
and [MDN SSE framing](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events).
