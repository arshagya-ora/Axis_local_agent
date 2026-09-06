# Phase 0 — Baseline Freeze and Provider Compatibility

Status: complete. This document is the single source of truth for what
Phase 0 delivered; see "Out of scope" for what it deliberately does not
cover.

## 1. Current seven-tool browser contract

`browser_agent_tools.py` exposes exactly seven model-facing tools —
`browser_observe`, `browser_act`, `browser_navigate`, `browser_wait`,
`browser_assert`, `browser_capture_evidence`, `browser_diagnose` — unchanged
by Phase 0. Their JSON schemas, the twelve public error codes
(`INVALID_ARGUMENT`, `UNKNOWN_TOOL`, `SESSION_NOT_FOUND`, `NO_MANAGED_TAB`,
`SCOPE_DENIED`, `STALE_OBSERVATION`, `REF_NOT_FOUND`, `BRIDGE_UNAVAILABLE`,
`BRIDGE_ERROR`, `ASSERTION_FAILED`, `TIMEOUT`, `SENSITIVE_OPERATION_BLOCKED`),
the common response envelope (`ok` / `browserSessionId` / `data` /
`error.code` / `error.message` / `error.retryable` / `error.diagnostic`),
and the 21-method forbidden-bridge-method set are now frozen as one
deterministic snapshot:

```
axis-agent/contracts/browser_tool_contract.json
```

- **Browser contract version:** `1.0.0`
- **Current contract hash:** `d1f506b13088dbc1ce08b386b7833f607ddd88e9b9e79e00413282bfe98bbdb6`

The snapshot is derived entirely from `browser_agent_tools.get_tool_definitions()`
and its module-level constants (`scripts/browser_contract.py`) — nothing in
it is hand-copied. The hash is a SHA-256 digest of the contract's
canonicalized (sorted-key, whitespace-free) JSON form, so it only changes
when the contract's actual content changes.

## 2. Existing automated test coverage

- `tests/test_browser_agent_tools.py` — 114 mocked unit tests, unchanged by
  Phase 0. Covers tool schemas, dispatch, the bridge method allowlist,
  forbidden methods, session binding, scope checks, observations, actions,
  navigation, waits, assertions, evidence capture, diagnostics, redaction,
  and bridge-error classification.
- `tests/test_phase0_baseline.py` — 30 new tests (added by Phase 0). Proves:
  exactly seven tools are exposed with unchanged names; model-facing schemas
  expose no raw bridge method selection or raw tab/window/group/frame/
  bridge-session identifiers; the generated contract matches the committed
  snapshot; contract drift produces a readable diff-based message; `verify()`
  never rewrites the committed file; the contract hash is deterministic and
  content-sensitive; provider configuration fails clearly when incomplete;
  the `.env` loader populates missing variables without ever overriding a
  real one, and a missing `.env` file is not an error; the probe's Pydantic
  result/report/manifest models validate and serialize correctly; secret-
  and OCID-shaped values are redacted and diagnostics are bounded; and one
  failing feature probe never stops the others.

Combined: **144 tests**, all passing, no live Chrome/bridge/OCI access
required for any of them.

## 3. Existing manual and end-to-end test assets

- `tests/_manual_workflows.py` — a scripted, assertion-based smoke test
  against a live Browser Agent Bridge and a real external site, covering all
  seven tools including the ref-rejection edge cases (check/uncheck/upload/
  drag correctly reject a `ref`).
- `tests/pydandic_agents_test.py` — an interactive Pydantic AI proof of
  concept (terminal chatbot) wiring the seven tools to an OCI-hosted
  Responses-API model. Demonstrates connectivity and tool execution; it is
  **not** a reproducible compatibility test (that is what the Phase 0 probe
  is for — see §4).
- `tests/fixtures/poc_app/` — a self-contained, stdlib-only local web app
  (`server.py`) plus `README.md`, `SIMPLE_TEST_PROMPTS.md`, and
  `POC_TEST_PROMPT.md` (a rigorous 12-phase validation scenario) purpose-built
  to exercise every tool/action/condition/assertion combination end to end,
  and `sample_upload.txt`, the approved fixture file for upload tests.

None of these were modified by Phase 0 except `tests/pydandic_agents_test.py`
(see §6).

## 4. Reproducing the baseline

Run from `axis-agent/` using the project's `.venv` (created via `uv sync`
at the repo root — **not** whatever `python` resolves to on `PATH`, which on
this machine is an unrelated system installation):

```bash
cd axis-agent

# Existing browser-tool tests (unchanged)
../.venv/Scripts/python.exe -m unittest tests/test_browser_agent_tools.py

# New Phase 0 tests
../.venv/Scripts/python.exe -m unittest tests/test_phase0_baseline.py

# Verify the committed contract snapshot (default action; exits 1 on drift)
../.venv/Scripts/python.exe scripts/browser_contract.py
../.venv/Scripts/python.exe scripts/browser_contract.py --verify   # equivalent, explicit

# Regenerate the snapshot after an intentional contract change
../.venv/Scripts/python.exe scripts/browser_contract.py --update
```

(On macOS/Linux, substitute `../.venv/bin/python`.)

## 5. Running the provider compatibility probe

```bash
cd axis-agent
../.venv/Scripts/python.exe scripts/provider_compatibility_probe.py --environment local
```

Writes a sanitized, deterministic-shape JSON report to
`axis-agent/reports/provider_compatibility_report.json` (gitignored — this
is per-run output, not a frozen artifact like the contract snapshot) and
prints a one-line-per-feature summary to stdout. `--out PATH` writes
elsewhere; `--environment LABEL` is just a label recorded in the report.

### Local values via `.env`

`axis-agent/.env` (gitignored) is loaded automatically by `provider_config.py`
on import — a real exported environment variable always takes precedence
over the file. `axis-agent/.env.example` is the committed template (copy it
to `.env` and fill in real values); it also documents the optional
`BROWSER_AGENT_BRIDGE_*` variables `browser_bridge_client.py` already reads
on its own.

### Provider configuration variables

| Variable | Required | Meaning |
| --- | --- | --- |
| `AXIS_OCI_GENAI_BASE_URL` | one of this or `..._REGION` | Full Responses-API-compatible endpoint URL. Takes precedence over `..._REGION` if both are set. |
| `AXIS_OCI_GENAI_REGION` | one of this or `..._BASE_URL` | OCI region (e.g. `us-ashburn-1`); used to derive the default endpoint URL when `..._BASE_URL` is not set. |
| `AXIS_OCI_GENAI_MODEL` | yes | Model alias/id (e.g. `openai.gpt-5.4-mini`). |
| `AXIS_OCI_GENAI_PROJECT_OCID` | yes | GenAI project OCID passed to the OpenAI client's `project` field. |
| `AXIS_OCI_PROFILE` | no (default `DEFAULT`) | OCI CLI profile name used by `OciUserPrincipalAuth`. |
| `AXIS_OCI_GENAI_API_VERSION` | no | Recorded in the report/manifest only if you already know it; the Responses API does not report its own version in a standard field. |

Credentials themselves are never read from these variables — authentication
stays with `oci_openai.OciUserPrincipalAuth`, which uses the OCI CLI config
(`~/.oci/config`) exactly as before this change.

Missing configuration does not fail the probe: `run_probe()` catches
`ProviderConfigError` and returns one `inconclusive` result per feature with
a clear diagnostic, so local runs and CI never need live credentials.

## 6. Latest compatibility matrix

The live probe was **not run against a real OCI/Grok endpoint** in this
session: no `AXIS_OCI_GENAI_*` environment variables were set (verified —
`env | grep AXIS_OCI` returned nothing), so there was no configuration to
test against, per the instruction not to invent configuration or claim a
live test that didn't happen. Running the probe under these conditions was
itself exercised and produced the expected, correct result: 12/12 features
`inconclusive`, each citing "Provider configuration missing", with a
top-level warning `"Live probe skipped: provider configuration missing."`

| Feature | Status | Reason |
| --- | --- | --- |
| basic_request | inconclusive | provider configuration missing |
| structured_output | inconclusive | provider configuration missing |
| strict_function_schema | inconclusive | provider configuration missing |
| single_tool_call | inconclusive | provider configuration missing |
| parallel_tool_calls | inconclusive | provider configuration missing |
| streaming | inconclusive | provider configuration missing |
| usage_reporting | inconclusive | provider configuration missing |
| reasoning_controls | inconclusive | provider configuration missing |
| conversation_continuation | inconclusive | provider configuration missing |
| background_execution | inconclusive | provider configuration missing |
| cancellation | inconclusive | provider configuration missing |
| provider_native_compaction | inconclusive | provider configuration missing |

To produce a real matrix, set the variables in §5 and re-run the command
above; then replace this table and commit the (already-sanitized) report
alongside the code change that prompted the re-check.

## 7. Known limitations

- The probe calls the raw `openai.AsyncOpenAI` Responses API
  (`client.responses.create/retrieve/cancel`) directly, using parameter and
  response-field names as documented for OpenAI's Responses API
  (`text.format` for structured output, `reasoning`, `previous_response_id`,
  `background`, `parallel_tool_calls`, `truncation`, streaming `delta`/
  `completed` events, `response.usage.input_tokens`/`output_tokens`). It has
  never been run against the actual OCI/Grok compatibility layer, so if that
  layer names or shapes any of these differently, the affected probe will
  correctly degrade to `inconclusive` with the raw exception text (by
  design — see `_classify_param_error`) rather than crash, but the specific
  wording of "why" should be re-checked once a live run is possible.
- `provider_native_compaction` and `reasoning_controls` are close to
  untestable via response inspection alone (accepting a parameter is not
  evidence it did anything) and are expected to land on `inconclusive` even
  against a fully-compliant provider unless it exposes an explicit signal.
- `pydantic-ai-harness` is **not installed** in this environment and was not
  added for Phase 0 (recorded as `"not installed"` in every probe report's
  `packageVersions`/`RunManifest.dependencyVersions`, per instruction not to
  install or integrate it just for Phase 0).
- The plain `pydantic-ai` package (non-slim) is also not installed; the
  project uses `pydantic-ai-slim[openai]` only, matching the existing POC.
- `bind_active_managed_tab()` and `BROWSER_AGENT_INSTRUCTIONS` in
  `browser_agent_tools.py` still describe the browser-agent-bridge's old
  tab-group-only access model ("there is no unscoped 'list every tab'
  escape hatch, by design"). The bridge itself was separately changed to
  grant whole-browser access by default; `browser_agent_tools.py` was
  intentionally left untouched here because Phase 0 does not authorize
  refactoring it beyond contract extraction, but this documentation is now
  stale and worth reconciling in a later phase.

## 8. Dependency-upgrade policy

Dependencies are pinned with exact versions in the root `pyproject.toml`
(the project's existing `uv`-managed format; `uv.lock` regenerated to
match, no second package manager introduced):

```toml
httpx==0.28.1
oci-openai==1.1.0
oci==2.185.1
openai==3.8.0
pydantic==2.13.5
pydantic-ai-slim[openai]==2.40.0
```

> Pydantic AI, Harness, OpenAI SDK, OCI authentication, or provider-model
> upgrades require the existing browser contract tests to pass and a new
> compatibility report to be compared with the previous accepted report.

## 9. Provider fallback decision

> Any capability not confirmed as supported must use a local or
> model-agnostic implementation in later phases.

Given §6, every probed capability is currently unconfirmed pending a live
run with real provider configuration — later phases should not assume any
of the twelve probed features (structured output, strict schemas, parallel
tool calls, streaming, usage reporting, reasoning controls, conversation
continuation, background execution, cancellation, provider-native
compaction) works against this deployment until a compatibility report
says `supported`.

## Out of scope (confirmed not introduced)

No root agent, planning, task intent, effect ledger, approvals, durable
workflows, Temporal, AG-UI, long-term context persistence, attachments,
Markdown/workbook capabilities, memory, skills, capability registry, tool
search, code mode, subagents, dynamic workflows, production observability
infrastructure, or database persistence was added. `browser_agent_tools.py`
was not refactored. No `axis/` production package was created.
