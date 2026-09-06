# Axis POC test-fixture application

**Looking for prompts to paste into the chatbot?**

- [SIMPLE_TEST_PROMPTS.md](SIMPLE_TEST_PROMPTS.md) — short, natural-language, one-paragraph prompts, one per workflow. Start here.
- [POC_TEST_PROMPT.md](POC_TEST_PROMPT.md) — the full rigorous 12-phase validation scenario with a progress ledger and pass/fail report.

A small, self-contained local web app (Python standard library only — no
extra dependencies, no build step) built to exercise every phase of the
"End-to-End Long-Running Browser Agent POC Validation" scenario. Nothing
like this existed in the repo before, so it was written specifically for
that scenario's fixtures:

| Page | Exercises |
| --- | --- |
| `/` | Phase 1–2 (title, main heading, observation modes) |
| `/profile` | Phase 3 (onboarding form: fill/press/select/check/uncheck/hover, tooltip) |
| `/components` | Phase 4 (visible/hidden/enabled/disabled/attribute/count) |
| `/upload` | Phase 5 (file upload, request/response waits) |
| `/board` | Phase 6 (drag-and-drop, vertical + horizontal scroll) |
| `/async-lab`, `/async-lab/target`, `/popup-target` | Phase 7 (delayed text/navigation/popup) |
| `/page-two` | Phase 8 (history back/forward/reload) |
| `/diagnostics` | Phase 9 (controlled console/network events, redaction fixtures) |
| `/summary` | Phase 12 (final business-state audit) |

## One deliberate omission: no real dialog

`/async-lab`'s "Dialog" section does **not** trigger a real
`window.alert()`/`confirm()`/`prompt()`. The Axis agent's seven tools never
call `page.acceptDialog` / `page.dismissDialog` — they aren't in
`browser_agent_tools.py`'s allowed bridge-method set — so a real native
dialog would block the tab's main thread forever with no way for the agent
to clear it. The page explains this in place of a button that would hang
the session; treat Phase 7's dialog step as a documented, expected
limitation rather than something to force.

## Running it

```bash
cd axis-agent/tests/fixtures/poc_app
python server.py --port 8990
```

Then bind an Agent-managed Chrome tab to `http://127.0.0.1:8990/` (see the
main [axis-agent README](../../../README.md) / the chatbot's `/bind`
command) before starting the POC run.

## Filling in the POC prompt's runtime placeholders

```text
Browser session ID: already known to the agent — the chatbot's own dynamic
                     instructions tell it the bound browserSessionId every
                     turn (see browsing_instructions() in
                     tests/pydandic_agents_test.py). You don't need to type
                     a value here; leave it out or write
                     "(use the browserSessionId already given to you)".
Test application URL: http://127.0.0.1:8990/
Approved upload file: <absolute path to>/axis-agent/tests/fixtures/poc_app/sample_upload.txt
Run ID: any short label, e.g. poc-20260906-1400 (date + time is fine —
        it only needs to be unique enough to spot in the profile
        email/display name and later records)
Expected application title: Axis POC Test Harness
Expected ARIA snapshot: optional — leave blank, or have the agent capture
        one first via browser_capture_evidence(accessibility_snapshot) on
        `/` and reuse that as the expected value on a later run
```

The profile page (Phase 3) derives the run's canonical ID from the email
field, matching the pattern the POC prompt already specifies:
`axis-poc-{{RUN_ID}}@example.com`. Everything created afterwards (uploaded
files, the task-board card) tags itself with that same ID via
`localStorage`, so later phases can correlate records to the run without
any extra input — as long as Phase 3 runs before Phases 5, 6, and 12, which
is the order the POC prompt already uses.

## Notes

- All state (profile, uploads, board) lives in the browser's `localStorage`
  for this origin — restarting `server.py` does not clear it; reload
  `http://127.0.0.1:8990/` in the same Chrome profile to reset by clearing
  site data, or use a fresh `Run ID` per run to avoid ambiguity.
- `/api/ping` (200), `/api/fail` (500), and `/api/diagnostics-echo` (200,
  with a dummy `Set-Cookie` header and dummy `password`/`access_token`/
  `refresh_token`/`api_key`/`secret` fields in its echoed JSON body) exist
  solely to give Phase 9 real, harmless network events to read and verify
  redaction against. None of the values are real secrets.
