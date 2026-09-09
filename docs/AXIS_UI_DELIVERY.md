# AXIS UI delivery — 10 September 2026

The extension now provides the graphite/mint workspace, one expandable card per
task, history drawer, and shared panel/options Settings. A loopback HTTP/SSE
service owns the existing Python orchestrator and persists local history in
SQLite. There is no frontend framework or build step. Playwright is a development
dependency for verification.

Start with [startup and pairing](AXIS_UI.md). Reload the unpacked extension, run
`uv run python -m axis.ui_service` from `axis-agent`, and pair through Settings.
The test services and profiles were temporary; the production service has not
been left running or paired into the user's browser profile.

## Capability checklist

| Phase | Delivered and checked |
| --- | --- |
| 0 — Existing runtime | Reused bridge messages, runtime construction, task IDs and enforcement; initial baseline 396 Python and 327 bridge tests. CLI/service share an ownership lock. |
| 1 — Usable extension | Responsive workspace, history dialog, shared settings/options, native bridge start/stop and restored approvals. Empty storage has no sample tasks. |
| 2 — Execution | Authenticated submissions and events, one runtime owner, durable retry IDs, bounded public activity, accurate Stop acknowledgment and real results. |
| 3 — Continuity | Pause/resume and one serialized follow-up retain task identity. Accepted/applied input is distinguished. Explicit budget extension uses the existing runtime entry point. |
| 4 — History | SQLite snapshots/events, reconnect deduplication, paging, rename/search/delete, draft/scroll restoration and interrupted status after restart. |
| 5 — Settings and polish | Light/Dark/System, validated future run limits, original advanced bridge controls, focus restoration, narrow layouts and zoom. |

Your current source-note and `/extend` work is integrated. The UI preserves cited
answers as inert text up to 64,000 characters; existing runtime citation validation
remains authoritative. Extending an exhausted live task retains its tabs, memory,
evidence, ID and accumulated usage. An uncertain retry cannot add the same budget
twice. No fresh budget is granted by an ordinary follow-up.

## Verification evidence

- **444 Python tests passed**, including 11 UI adapter/store tests and the current
  research/runtime regressions. One upstream Starlette/AnyIO deprecation warning.
  [Python summary](../artifacts/axis-ui/python-tests.txt).
- **336 bridge/JavaScript tests passed**, including four UI state tests.
  [JavaScript summary](../artifacts/axis-ui/node-tests.txt).
- **10 unpacked-MV3 browser scenarios passed, no page errors**: actual extension
  origin, worker, settings/pairing and UI; a controlled Python runner provides
  repeatable pause/extend/reconnect timing. Two reconnects retain one copy of
  missed activity; 70 events keep the rendered activity at 30 rows and preserve
  the reader's position. [Browser evidence](../artifacts/axis-ui/verification.json).
- **Six real native-host scenarios passed**: start, in-panel approval, popup
  fallback, pending-prompt restoration, cross-window reconciliation/session
  decisions and stop. [Native evidence](../artifacts/axis-ui/native-verification.json).
- **Real configured-provider fixture smoke passed** through HTTP submission,
  SSE and the existing native bridge. It correctly returned the exact heading
  “Fixture Home”; independent checks found observed page evidence, no mutations,
  no false completion and zero duplicate actions. A duplicate submission returned
  the same task. [Fixture evidence](../artifacts/axis-ui/real-fixture-result.json).

The UI tests also check malicious titles as inert text, Escape/focus restoration,
actual 14px body text, collapsed Stop, shared options, and reachable toolbar and
composer controls at 200% zoom. Eleven checked text/background pairs have contrast
ratios of **5.57:1–16.34:1**; this is a text-color check, not a full accessibility
certification. [Measured pairs](../artifacts/axis-ui/text-contrast.json).

## Screenshots

| View | 320 × 640 | 400 × 900 | 480 × 900 |
| --- | --- | --- | --- |
| Workspace | [Image](../artifacts/axis-ui/workspace-320x640.png) | [Image](../artifacts/axis-ui/workspace-400x900.png) | [Image](../artifacts/axis-ui/workspace-480x900.png) |
| History | [Image](../artifacts/axis-ui/history-320x640.png) | [Image](../artifacts/axis-ui/history-400x900.png) | [Image](../artifacts/axis-ui/history-480x900.png) |
| Settings | [Image](../artifacts/axis-ui/settings-320x640.png) | [Image](../artifacts/axis-ui/settings-400x900.png) | [Image](../artifacts/axis-ui/settings-480x900.png) |

The full set includes widths 320/360/400/480 at heights 640/900,
[dark workspace](../artifacts/axis-ui/workspace-dark-400x900.png),
[dark settings](../artifacts/axis-ui/settings-dark-400x900.png),
[200% zoom](../artifacts/axis-ui/workspace-200-percent.png), and
[a restored real bridge approval](../artifacts/axis-ui/approval-restored-400x900.png).
These are the registered extension documents in isolated Chromium, sized to panel
viewports, without Chrome's surrounding sidebar chrome. The task screenshot
content is an explicit development fixture, never production seeded history.

## Remaining limits

Python restart restores history and marks owned work Interrupted; it does not
restore a live run or replay browser commands. V1 has one runtime owner, including
while paused or exhausted. Stop waits for an in-flight synchronous call to return
and cannot undo completed actions. Saved artifacts are descriptive records, not
arbitrary filesystem links. This work does not establish safe simultaneous manual
browsing in an agent-controlled tab.

Chrome's first-time optional-permission consent remains a manual browser-owned
step. The automated native test pre-grants those existing permissions in a
disposable manifest copy because headless Chrome cannot show the consent dialog;
the production manifest retains its original permission declarations and key.
