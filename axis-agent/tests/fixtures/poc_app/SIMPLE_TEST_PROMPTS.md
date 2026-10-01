# Simple test prompts

Short, natural-language prompts for the running chatbot (`uv run python
axis-agent/tests/pydandic_agents_test.py`), one per workflow, against the
fixture app at `http://127.0.0.1:8990/`. Paste one at a time as a normal
chat message — no ledger, no phase numbers, no fixed tool sequence. Each
one exercises a different part of the seven browser tools.

**Profile form**
> Go to http://127.0.0.1:8990/profile and fill out the profile form for me: display name "Axis POC Demo", email "axis-poc-demo@example.com", department Engineering, region India, notification preference Email, theme Dark. After filling each field, re-observe and confirm that field's value is exactly what you just set before moving to the next one — don't assume it took. Leave marketing emails off but check the consent box, hover the help icon to see what the tooltip says, then submit it and confirm it actually saved.

**Component states**
> Open http://127.0.0.1:8990/components and check that the hidden component really does toggle — show it and confirm it's visible, then hide it again and confirm it's gone (re-observe the page fresh before each check rather than reusing an earlier snapshot). Also confirm the disabled button really is disabled, the stable widget's data-status attribute is "ready", and there are exactly 5 rows in the list — count elements matching the `.component-row` class specifically, not a text search for "Row" (which can also match the containing list's combined text).

**File upload**
> Head to http://127.0.0.1:8990/upload and upload the file at d:/Products/AXIS/Axis-phase-3/axis-agent/tests/fixtures/poc_app/sample_upload.txt. Wait for it to finish, confirm it shows up in the uploaded-files table with the right name, then try reading the raw response body of that upload request and tell me what happens.

**Drag-and-drop board**
> Go to http://127.0.0.1:8990/board, scroll down until the board is visible, then drag the card in the To Do column into the Done column. Confirm it actually moved and that both columns' counts updated.

**Async lab**
> On http://127.0.0.1:8990/async-lab, trigger the delayed text button and wait for the result text to show the exact string "Delayed event completed" — don't guess the wording, wait for that specific text. Then trigger the delayed navigation and confirm you land on the async target page. Then trigger the popup and tell me what you learned about it. Skip the dialog button — it's intentionally not wired up.

**History navigation**
> Starting from http://127.0.0.1:8990/, navigate to /page-two, go back, go forward again, then reload the page — and tell me at each step which page you're actually looking at.

**Diagnostics**
> Go to http://127.0.0.1:8990/diagnostics, trigger both the console logging and the network requests, then read back the console and network diagnostics and tell me whether the dummy password/token/secret fields got redacted properly.

**Evidence capture**
> On whatever page you're currently on, take a full-page screenshot, a DOM snapshot, and an accessibility snapshot, and tell me where each one got saved.

**Small end-to-end flow**
> Starting fresh at http://127.0.0.1:8990/, fill in and submit the profile form, upload the sample file, and move the board card to Done — then check the /summary page and tell me whether everything you just did actually shows up there.

For the full, rigorous 12-phase version of this (progress ledger, exact
tool-coverage matrix, pass/fail report), see
[POC_TEST_PROMPT.md](POC_TEST_PROMPT.md) instead.
