# End-to-End Long-Running Browser Agent POC Validation — ready-to-paste prompt

This is the full validation scenario, with the runtime-inputs block already
filled in for the local test-fixture app in this directory
(`server.py`, running at `http://127.0.0.1:8990/`). Copy everything below
the `---` and paste it as your message into the running chatbot
(`uv run python axis-agent/tests/pydandic_agents_test.py`) after it reports
a successful `/bind`.

If you re-run this later, generate a fresh `Run ID` (anything short and
unique, e.g. `poc-YYYYMMDD-HHMM`) — the fixture app's state lives in the
bound tab's `localStorage` and old records from a previous run with the
same Run ID will still be there otherwise.

---

You are performing a comprehensive validation of the Axis browser agent in an authorized, non-production test application.

Your objective is to complete a realistic, multi-stage business workflow while exercising every public browser tool and all major supported operations. This is also a long-running-task test: maintain progress, recover from expected failures, preserve important state, and continue until every phase has either passed or been documented as blocked.

## Runtime inputs

Use only the values supplied below. Do not invent browser-session identifiers, URLs, element references, selectors, trace identifiers, request identifiers, or file paths.

```text
Browser session ID: (already known to you — see your own instructions)
Test application URL: http://127.0.0.1:8990/
Approved upload file: d:/Products/AXIS/Axis-phase-3/axis-agent/tests/fixtures/poc_app/sample_upload.txt
Run ID: poc-20260905-1842
Expected application title: Axis POC Test Harness
Expected ARIA snapshot: (not supplied — skip that specific assertion and record it as not applicable for this run)
```

The host application has already bound the browser session to an approved Agent-managed Chrome tab.

## Safety boundaries

* Operate only within the bound Agent-managed tab and the supplied test application.
* Do not attempt to access arbitrary existing Chrome tabs.
* Do not use arbitrary JavaScript, raw bridge methods, cookie access, header modification, policy changes, extension reloads, CSP bypasses, or unrestricted network interception.
* Do not submit real purchases, messages, external communications, credentials, or production changes.
* Use only the approved upload file.
* Do not bypass scope denials or disabled sensitive operations.
* Do not claim a step succeeded unless its expected result is visible or confirmed by `browser_assert`.
* Never invent an element ref. Use only refs returned by the latest compact observation.
* Perform exactly one semantic interaction in each `browser_act` call.
* After navigation or substantial page replacement, call `browser_observe` again before interacting with the new page.

## Long-running-task requirements

Maintain an internal progress ledger throughout the run with:

```text
Run ID
Current phase
Completed steps
Pending steps
Failed or blocked steps
Important values discovered
Current page and workflow state
Evidence IDs and saved artifact paths
Active trace ID
Last successful checkpoint
Recovery actions taken
```

Persist or checkpoint this ledger after every major phase using the agent's session-memory or checkpoint facility.

Do not place the complete ledger into every model request. Keep a compact working summary and retain detailed historical events in persistent session storage.

If execution is interrupted or restarted:

1. Load the latest checkpoint.
2. Verify the currently visible page.
3. Reconcile the visible state with the checkpoint.
4. Do not repeat completed destructive or state-changing operations unless verification shows they did not occur.
5. Continue from the first incomplete step.
6. Mention the interruption and recovery in the final report.

## General execution policy

For every phase:

1. Observe before element-targeted interaction.
2. Use the latest observation and its current refs.
3. Perform one interaction at a time.
4. Use a meaningful wait when asynchronous behavior is expected.
5. Assert the intended outcome.
6. Re-observe when the page changes or new refs are needed.
7. Record the result in the progress ledger.
8. Save a checkpoint at the end of the phase.

A successful action only proves that the interaction was attempted. A successful wait only proves that the awaited event occurred. Use assertions to prove the required outcome.

If an operation fails because an observation or ref is stale, observe again and retry once with a fresh ref.

For other unexpected failures:

1. Do not immediately repeat the same call multiple times.
2. Observe the current page.
3. Use the smallest relevant diagnostic source.
4. Retry at most once if diagnostics indicate a transient condition.
5. Otherwise record the step as blocked and continue with independent phases.
6. Never manufacture a successful result.

## Phase 1: Establish the initial state and begin tracing

1. Open the test application URL using `browser_navigate`.
2. Wait for the page-load condition.
3. Wait for network idle.
4. Observe the page in compact mode.
5. Record the returned URL, title, observation ID, snapshot ID, and whether the observation was truncated.
6. Assert that the page title matches the expected application title.
7. Assert that the application's main heading or dashboard region is visible.
8. Start a browser trace using `browser_capture_evidence`.
9. Record the returned trace ID.
10. Query trace status using `browser_diagnose` and confirm that tracing is active.
11. Capture an initial page screenshot and save it to disk as:

```text
{RUN_ID}-01-initial-page.png
```

12. Save the first checkpoint.

This phase must exercise:

```text
browser_navigate: open
browser_wait: page_load
browser_wait: network_idle
browser_observe: compact
browser_assert: title
browser_assert: visible
browser_capture_evidence: trace_start
browser_capture_evidence: page_screenshot
browser_diagnose: trace_status
```

## Phase 2: Inspect the application using all observation modes

1. Observe the current page in compact mode and retain its current actionable refs.
2. Observe the page in ARIA mode.
3. Observe the page in text mode.
4. Compare the three results internally:

   * Compact mode should be used for actionable refs.
   * ARIA mode should expose accessibility-oriented structure.
   * Text mode should provide readable content without being treated as an action map.
5. If any observation is truncated, repeat only that observation with an appropriately increased bounded `maxNodes`.
6. Do not use refs from ARIA or text mode unless the tool explicitly returned actionable refs.
7. Return to a fresh compact observation before beginning the next phase.
8. Save a checkpoint.

This phase must exercise:

```text
browser_observe: compact
browser_observe: aria
browser_observe: text
browser_observe: maxNodes handling
```

## Phase 3: Complete the profile and preferences workflow

Navigate through the test application's profile or onboarding workflow and configure a test profile using the following data:

```text
Display name: Axis POC {RUN_ID}
Email: axis-poc-{RUN_ID}@example.com
Department: Engineering
Region: India
Notification preference: Email
Theme or interface preference: Dark
Optional marketing subscription: Disabled
Terms/test-consent checkbox: Enabled
```

Complete these operations individually:

1. Observe the form and identify the required controls.
2. Fill the display-name field.
3. Fill the email field.
4. Use a keyboard press such as `Tab` to move focus.
5. Use another appropriate key or shortcut where the application provides a safe keyboard interaction.
6. Select the department.
7. Select the region.
8. Select the notification preference.
9. Check the required consent checkbox.
10. If the marketing checkbox is initially checked, uncheck it. If it is already unchecked, check it and then uncheck it so both operations are validated without changing the requested final state.
11. Hover over a help or information element and wait for its tooltip to become visible.
12. Assert that:

    * The display-name field is editable.
    * The display-name field has the expected value.
    * The email field has the expected value.
    * The consent checkbox is checked.
    * The marketing checkbox is not checked or reflects the application's equivalent disabled state.
    * The selected department and region are visible in the resulting form state.
    * The tooltip contains the expected explanatory text.
    * The submit control is enabled.
13. Submit or save the profile with one click.
14. Wait for a meaningful success condition, such as success text becoming visible.
15. Assert that the success message contains the Run ID or otherwise proves that this run's profile was saved.
16. Observe the updated page and record the resulting state.
17. Capture an element screenshot of the saved-profile confirmation area as:

```text
{RUN_ID}-02-profile-confirmation.png
```

18. Save a checkpoint.

This phase must exercise:

```text
browser_act: fill
browser_act: press
browser_act: select
browser_act: check
browser_act: uncheck
browser_act: hover
browser_act: click
browser_wait: element_state
browser_wait: text
browser_assert: editable
browser_assert: value
browser_assert: checked
browser_assert: text
browser_assert: enabled
browser_capture_evidence: element_screenshot
```

## Phase 4: Validate element states, attributes, and counts

Use the test application's component or validation page.

1. Navigate to the component-testing section through the visible UI.
2. Observe the destination page before interacting.
3. Locate examples of:

   * Visible element
   * Initially hidden element
   * Enabled control
   * Disabled control
   * Element with a stable test attribute
   * Repeated list or table rows
4. Trigger the application's safe show/hide demonstration.
5. Wait for the hidden element to become visible.
6. Assert that it is visible.
7. Trigger the hide operation.
8. Wait for it to become hidden.
9. Assert that it is hidden.
10. Assert that the enabled control is enabled.
11. Assert that the disabled control is disabled.
12. Assert that the designated element contains the expected attribute and value.
13. Count the designated list or table entries and assert the documented expected count.
14. Assert the expected text of at least one component.
15. Save a checkpoint.

This phase must exercise:

```text
browser_wait: element states attached, visible, hidden or detached where available
browser_assert: visible
browser_assert: hidden
browser_assert: enabled
browser_assert: disabled
browser_assert: attribute
browser_assert: count
browser_assert: text
```

If the test application does not expose all four element states, test every available state and document the missing fixture rather than inventing a result.

## Phase 5: Upload and verify an approved file

1. Open the test application's upload page.
2. Observe the page.
3. Upload only the approved upload file given above.
4. Use the supported semantic locator required by the upload operation. Do not invent or attempt a nonexistent ref-based upload method.
5. Wait for the upload request and response conditions exposed by the test fixture.
6. Wait for the uploaded filename or completion message to appear.
7. Assert that the uploaded-file row contains the expected filename.
8. Assert that exactly one file associated with the Run ID is present, or use the application's documented equivalent.
9. Inspect bounded network diagnostics for the upload request.
10. Record the relevant request ID, status, method, and sanitized URL.
11. Attempt response-body diagnosis only if explicitly required for this POC.
12. Because response-body access is expected to be disabled by default:

    * Treat `SENSITIVE_OPERATION_BLOCKED` as the expected security result.
    * Do not attempt to bypass the restriction.
    * Record whether the sensitive-operation boundary worked correctly.
13. Capture an element screenshot of the uploaded-file result as:

```text
{RUN_ID}-03-upload-result.png
```

14. Save a checkpoint.

This phase must exercise:

```text
browser_act: upload
browser_wait: request
browser_wait: response
browser_wait: text
browser_assert: text
browser_assert: count
browser_diagnose: network
browser_diagnose: response_body security boundary
browser_capture_evidence: element_screenshot
```

## Phase 6: Perform drag-and-drop and scrolling

Use the application's test task board or sortable-list page.

1. Navigate to the task-board page through the UI.
2. Observe the board.
3. Identify the test card associated with the Run ID, or create it safely if the fixture requires that preparation.
4. Scroll vertically until the destination column or target region is visible.
5. If the page supports horizontal board scrolling, perform a bounded horizontal scroll as well.
6. Drag the test card from its source column to the specified destination column using supported source and target locators.
7. Do not attempt a nonexistent ref-based drag method if the bridge requires locators.
8. Wait for the board-update request and response if the fixture exposes them.
9. Observe the updated board.
10. Assert that:

    * The card appears in the destination column.
    * The card no longer appears in the source column, where a supported assertion can prove this.
    * The relevant destination count increased or matches the expected count.
    * The relevant source count decreased or matches the expected count.
11. Capture a page screenshot as:

```text
{RUN_ID}-04-board-after-drag.png
```

12. Save a checkpoint.

This phase must exercise:

```text
browser_act: scroll
browser_act: drag
browser_wait: request or response
browser_assert: visible
browser_assert: hidden or count
browser_capture_evidence: page_screenshot
```

## Phase 7: Validate delayed events, popup, and dialog handling

Use the test application's asynchronous-event laboratory.

### Delayed text

1. Observe the page.
2. Trigger the control that schedules delayed text.
3. Wait for the expected text.
4. Assert that the resulting text is correct.

### Delayed URL change or navigation

5. Trigger the safe delayed-navigation fixture.
6. Wait for the navigation condition.
7. Wait for the expected URL.
8. Observe the destination page.
9. Assert its title or identifying content.

### Popup

10. Return to the asynchronous-event page if necessary.
11. Observe again.
12. Trigger the fixture that opens a popup after a documented delay.
13. Wait for the popup condition.
14. Record only bounded popup metadata returned by the tool.
15. Do not attempt to escape the managed browser scope.

### Dialog

16. Trigger the fixture that opens a browser dialog after a documented delay.

17. Wait for the dialog condition.

18. Record the dialog type and sanitized message.

19. Handle it only through supported test-application behavior. Do not invent unsupported dialog controls.

20. Save a checkpoint.

This phase must exercise:

```text
browser_wait: text
browser_wait: navigation
browser_wait: url
browser_wait: popup
browser_wait: dialog
browser_assert: title or text
```

If a popup or dialog fixture is unavailable, record the exact limitation and continue. Do not simulate a successful event.

**Note for this run:** the fixture app's `/async-lab` page intentionally does not offer a working dialog trigger — see its "Dialog" section for why (a real dialog would hang the tab with no way for the agent's tools to clear it). Record the dialog sub-step as an expected, documented limitation.

## Phase 8: Validate browser history and page-reference invalidation

1. Record the current URL.
2. Use `browser_navigate` to open a second supplied test-application page (the fixture's `/page-two`).
3. Observe the second page.
4. Use `browser_navigate` to go back.
5. Observe again before interacting.
6. Assert that the original page is restored.
7. Use `browser_navigate` to go forward.
8. Observe again.
9. Assert that the second page is restored.
10. Reload the current page.
11. Wait for page load and network idle.
12. Observe again.
13. Confirm through normal operation that refs from observations made before navigation are no longer reused.
14. Do not deliberately bypass stale-ref checks.
15. If the POC harness provides an approved stale-ref test mode, attempt one old ref and confirm that it returns `STALE_OBSERVATION`; then observe again and continue normally.
16. Save a checkpoint.

This phase must exercise:

```text
browser_navigate: open
browser_navigate: back
browser_navigate: forward
browser_navigate: reload
browser_wait: page_load
browser_wait: network_idle
browser_observe after every navigation
stale-observation protection
```

## Phase 9: Run controlled diagnostics

Navigate to the application's diagnostic test page, which should intentionally produce safe test-only console and network events.

1. Observe the diagnostic page.
2. Trigger the fixture that logs:

   * One informational console entry
   * One warning
   * One controlled error
3. Read bounded console diagnostics.
4. Confirm that the expected test messages are present.
5. Trigger the fixture that performs:

   * One successful request
   * One controlled failed request
6. Wait for the relevant request or response.
7. Read bounded network diagnostics.
8. Identify the controlled success and failure without exposing secrets.
9. Capture a diagnostic DOM snapshot.
10. Query current trace status using the active trace ID.
11. Verify that sensitive fields are redacted if the diagnostic fixture deliberately emits test fields named:

    * Authorization
    * Cookie
    * Set-Cookie
    * password
    * access_token
    * refresh_token
    * api_key
    * secret
12. Confirm only that redaction occurred. Never reproduce the original values in the final report.
13. Do not retrieve response bodies after the expected default denial in Phase 5.
14. Save a checkpoint.

This phase must exercise:

```text
browser_diagnose: console
browser_diagnose: network
browser_diagnose: dom
browser_diagnose: trace_status
diagnostic bounding
diagnostic redaction
```

## Phase 10: Capture structural and document evidence

Capture the following evidence from the final validated application state:

1. Page screenshot:

```text
{RUN_ID}-05-final-page.png
```

2. Element screenshot of the final summary region:

```text
{RUN_ID}-06-final-summary.png
```

3. DOM snapshot.
4. Accessibility snapshot.
5. PDF:

```text
{RUN_ID}-07-final-report.pdf
```

6. Verify the expected ARIA structure using the supplied expected snapshot if it is available and applicable.
7. If the application cannot produce a PDF or a specific snapshot type, record the bridge or application limitation without claiming success.
8. Ensure binary evidence is saved to disk.
9. Do not return large base64 or data-URL payloads in the final response.
10. Record evidence IDs, types, timestamps, URLs, trace IDs, and saved paths.
11. Save a checkpoint.

This phase must exercise:

```text
browser_capture_evidence: page_screenshot
browser_capture_evidence: element_screenshot
browser_capture_evidence: dom_snapshot
browser_capture_evidence: accessibility_snapshot
browser_capture_evidence: pdf
browser_assert: aria_snapshot
```

## Phase 11: Stop and export the trace

1. Query trace status and verify that the trace is still active.
2. Stop the trace using the recorded trace ID.
3. Export the trace as HTML:

```text
{RUN_ID}-08-trace.html
```

4. Record the exported trace evidence metadata and saved path.
5. Query trace status again if supported and verify that the trace is no longer active or is marked completed.
6. Do not return the trace's complete encoded content to the model or final response.
7. Save a checkpoint.

This phase must exercise:

```text
browser_diagnose: trace_status
browser_capture_evidence: trace_stop
browser_capture_evidence: trace_export
```

## Phase 12: Final business-state verification

Return to the application's final summary or audit page.

Perform final assertions that prove, where supported, that:

1. The profile belongs to `Axis POC {RUN_ID}`.
2. The expected email is saved.
3. Department is Engineering.
4. Region is India.
5. Required consent is enabled.
6. Marketing subscription is disabled.
7. The approved file is listed.
8. The test card is in the destination board column.
9. The controlled asynchronous operation completed.
10. The application shows the expected final status.
11. The final page title is correct.
12. The expected number of run-specific records exists.
13. The designated summary element has the expected stable attribute.
14. The final accessibility structure matches the expected snapshot when one was supplied.

If the application is still transitioning, use a meaningful wait before asserting. Do not replace assertions with waits.

Save the final checkpoint with status:

```text
COMPLETED
COMPLETED_WITH_LIMITATIONS
or
FAILED
```

## Required final response

Return a concise but complete test report with the following sections.

### 1. Overall result

```text
Run ID:
Status:
Start page:
Final page:
Resumed after interruption: yes/no
Total completed phases:
Total blocked phases:
```

### 2. Business workflow result

State whether the profile, preferences, upload, drag-and-drop operation, asynchronous events, and final summary were successfully completed and asserted.

### 3. Tool coverage matrix

Provide one row for each public tool:

```text
Tool
Operations tested
Passed operations
Failed operations
Blocked or unavailable operations
Evidence or diagnostic reference
```

The matrix must contain exactly these tools:

```text
browser_observe
browser_act
browser_navigate
browser_wait
browser_assert
browser_capture_evidence
browser_diagnose
```

### 4. Action coverage

Report the result for:

```text
click
fill
press
hover
select
check
uncheck
upload
drag
scroll
```

### 5. Navigation coverage

Report the result for:

```text
open
reload
back
forward
```

### 6. Wait coverage

Report the result for:

```text
element_state
url
navigation
page_load
network_idle
text
popup
dialog
request
response
```

### 7. Assertion coverage

Report the result for:

```text
visible
hidden
enabled
disabled
editable
checked
value
text
count
attribute
title
aria_snapshot
```

### 8. Evidence coverage

Report the result and saved artifact metadata for:

```text
page_screenshot
element_screenshot
dom_snapshot
accessibility_snapshot
pdf
trace_start
trace_stop
trace_export
```

Do not include base64 or complete data URLs.

### 9. Diagnostic and security results

Report:

* Console diagnostic result
* Network diagnostic result
* DOM diagnostic result
* Trace-status result
* Response-body blocking result
* Redaction result
* Scope-enforcement result
* Stale-observation protection result
* Confirmation that no forbidden raw method was attempted

### 10. Long-running behavior

Report:

* Checkpoints created
* Last successful checkpoint
* Whether compact memory was maintained
* Whether execution resumed after interruption
* Whether completed operations were safely avoided during recovery
* Any context-loss, repetition, looping, or state-consistency problems

### 11. Failures and limitations

For every failure or unavailable fixture, include:

```text
Phase
Operation
Error code
Retryable
Recovery attempted
Final disposition
```

Do not convert expected assertion failures or security denials into implementation crashes.

### 12. Final conclusion

State one of:

```text
POC PASS
POC PASS WITH LIMITATIONS
POC FAIL
```

A pass requires:

* All seven public tools invoked successfully at least once.
* All available required operations tested.
* Important business outcomes asserted.
* Evidence saved without returning large encoded payloads.
* Diagnostics bounded and redacted.
* Sensitive response-body access blocked by default.
* No raw bridge method exposed to or selected by the model.
* Managed-tab scope preserved.
* Progress checkpoints maintained throughout the long-running task.
* No false claims of success.

Do not claim POC PASS if required functionality was skipped, simulated, or inferred without verification.
