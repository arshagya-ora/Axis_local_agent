# Local workflow evaluations

From `axis-agent`, with the existing development dependencies installed:

```powershell
..\.venv\Scripts\python.exe -m evals.run --preflight
..\.venv\Scripts\python.exe -m evals.run
..\.venv\Scripts\python.exe -m evals.run --case grounded_final_answer --case multi_tab_memory --repeat 3
..\.venv\Scripts\python.exe -m evals.run --config axis.yaml --output evals/results/comparison --judge
```

The default runs all 12 cases three times, sequentially. A connected Chrome
extension and bridge plus the usual AXIS provider configuration are required.
`--preflight` checks bridge readiness, bridge authentication, and local provider
setup without sending a model request or creating tabs. Provider availability
is ultimately tested by the workflow request itself.

Each repetition starts the existing fixture server on a fresh loopback origin.
Only evaluation-created tabs are exposed to AXIS, navigation is restricted to
that origin, and those tabs and the server are closed afterward. Download
fixtures create a small CSV through the browser's configured download behavior.
Existing user tabs are never closed. The canvas case uses automatic visual
recovery; a configuration with `run.visual_mode: off` can measure the DOM-only
baseline.

The original eight quality cases have executable pages, with additional cases
for memory across tabs, asynchronous SPA changes, completed downloads, and a
canvas control. Outcomes use independent fixture state, fresh browser reads,
and actual interaction counts. Repeated form/button actions fail the duplicate
action check even if the final state is correct. An agent's `completed` status
with failing fixture checks is a false completion.

Reports are `results.json` and `results.md` under the ignored
`evals/results/<timestamp>` directory unless `--output` is supplied. They include
task success, false completions, duplicate actions, latency, model requests,
browser actions, and input/output tokens. Unavailable prerequisites are shown
separately and excluded from performance averages. Exit codes are `0` for all
tasks passing, `1` for task failures, and `2` for unavailable prerequisites.

The optional `--judge` constructs an LLM judge using the configured AXIS model
and writes its Pydantic Evals report to `pydantic-evals.json`. The judge does not
replace deterministic checks. Screenshots and image bytes are removed from all
persisted reports. No model judge is constructed during an ordinary run.
