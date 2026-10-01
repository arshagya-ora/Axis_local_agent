# AXIS manual workflow prompts

Prompts for exercising AXIS against a live browser and provider. Run each with
the full trace:

```bash
uv run python -m axis.cli --debug
```

Every prompt is **non-destructive by design**: drafts are never sent, carts are
never checked out, nothing is posted or purchased. That is deliberate — a test
prompt that mutates someone's real account is a bad test.

Each entry says what machinery it stresses and what to watch for in the trace.

---

## 1. Multi-tab research and comparison

> Open three separate tabs: the Python `asyncio` docs, the Trio docs, and the
> AnyIO docs. From each one, find how it describes its own concurrency model in
> a sentence or two. Then tell me the three descriptions side by side and which
> library each came from.

**Stresses:** tab creation without step interruption, reading other tabs by
alias, the orchestrator following tab switches, extraction retained across
planner passes.
**Watch for:** creating tabs and reading them should happen **within** a step.
If you see a run of `browser_tabs.activate` calls each ending its own step,
the create/activate interrupt has regressed.
**Benchmark:** ~2 steps / ~6 model requests / ~35s. Thirteen steps means broken.

---

## 2. Search → drill down → verify

> Search Google for "pydantic ai agent spec", open the most relevant result,
> and find the exact YAML field that sets an agent's retry budget. Assert that
> the page actually contains that field name before you tell me the answer.

**Stresses:** fill + Enter search flow, navigation interrupts, `browser_assert`
as real evidence, verification-before-completion.
**Watch for:** the planner should refuse to complete until an assertion or a
post-navigation observation backs the claim. One `browse → assert → complete`
cycle is the good shape.

---

## 3. Form fill with verification (the corrected leave-application flow)

> Open Gmail, start a new draft addressed to nobody, with subject "Leave
> application — [dates TBD]" and a short professional leave request body
> addressed to Mike. Do not send it. Once the draft is populated, verify
> the subject and body are actually on screen, take a screenshot of the compose
> window, and give me the saved file path.

**Stresses:** the opt-in `browser_capture_evidence` tool auto-enabling from the
word "screenshot", mutation tracking, and post-mutation verification.
**Watch for:** `[TOOLS] capture=True` must appear at the very start. The run
should *not* end with "no screenshot tool is available".

---

## 4. Read-only extraction under budget pressure

> Go to Hacker News and give me the title, points, and comment count for the
> top five stories on the front page, as a table.

**Stresses:** a single rich observation vs. many small actions, the 3-action
per-step budget, bounded extraction surviving into the planner's context.
**Watch for:** this should need very few browser actions — mostly one good
observation. Lots of `ACTION_BUDGET_EXHAUSTED` here means the navigator is
over-clicking instead of reading.

---

## 5. Cross-site transcription

> Find the current top-voted answer on the Stack Overflow question about the
> difference between `is` and `==` in Python. Then open a new Gmail draft and
> paste a two-sentence summary of that answer into the body. Don't send it.

**Stresses:** carrying extracted content across a tab boundary and across
planner passes — the exact thing that broke in the original ChatGPT → Gmail run.
**Watch for:** the summary must survive from the reading tab into the compose
tab. If the navigator re-reads the source page after switching tabs, memory
retention is leaking.

---

## 6. Blocked path → replan

> Log into my banking portal and tell me my current balance.

**Stresses:** the `ask_user` / `blocked` path and the planner's authority to
stop. AXIS has no credentials and must not guess.
**Watch for:** a clean `ask_user` or `fail` within one or two planner passes.
An endless loop of retrying a login page is a cadence bug.

---

## 7. Deliberate failure → diagnose opt-in

> Open `https://httpstat.us/500` and tell me exactly what went wrong, including
> anything the browser console or network activity reports.

**Stresses:** the failure path and `browser_diagnose` auto-enabling after a
genuine failure.
**Watch for:** `[TOOLS] ... diagnose=True` appearing only *after* the first
real failure — not at the start. That is the spec's opt-in rule working.

---

## 8. Download handling

> Go to the Python.org downloads page, download the Windows embeddable package
> for the latest 3.12 release, and tell me where it was saved and how big it is.

**Stresses:** the opt-in `browser_downloads` tool, artifact paths, and reporting
a real filesystem location back to the user.
**Watch for:** downloads must be enabled by the request, and the reported path
should be real — spot-check it exists.

---

## 9. Follow-up continuity

Start with:

> Open the Pydantic AI docs and find what `end_strategy` accepts as values.

Then, at the `You (blank to quit) >` prompt, follow up with:

> Now, without starting over, also tell me what the default is and which page
> you found it on.

**Stresses:** `continue_task` — bounded context retention, the live browser and
open tabs surviving, and a forced immediate planner pass.
**Watch for:** the follow-up must **not** re-open the docs from scratch. The
planner should run immediately and reuse the tab it already has.

---

## 10. Long mechanical sequence (cadence stress)

> On Wikipedia, start at the article for "Ada Lovelace" and follow the first
> ordinary link in the body of each article five times in a row. Tell me the
> chain of five article titles you ended up visiting, in order.

**Stresses:** planner cadence over a long run — a planner pass every 3 navigator
steps, repeated goal completion, and `completed_goal_summaries` accumulating.
**Watch for:** roughly 2 planner passes per 3 navigator steps. Also a good
wall-clock benchmark: check the final `time:` line for `model=` dominance.

---

## 11. Ambiguity → ask_user

> Book me a table for dinner.

**Stresses:** `ask_user` when genuinely required information is missing, without
any browsing at all.
**Watch for:** ideally zero browser actions. The planner should recognise it
cannot proceed before touching the browser.

---

## 12. Non-browser passthrough

> What's the difference between a semaphore and a mutex?

**Stresses:** the "no browser needed" fast path.
**Watch for:** `browser_actions=0`, one planner pass, one model request, and
the bridge never contacted at all.

---

## Reading the trace

| Line | Meaning |
|---|---|
| `[mm:ss.mmm]` | wall-clock offset since the run started |
| `(5.5s)` | how long that specific operation took |
| `[OK ]` / `[FAIL]` | the call ran and its result |
| `[SKIP]` | the runtime **refused** the call — it never reached the bridge |
| `[OBSV]` | the automatic fresh observation before a navigator step |
| `[TOOLS]` | an opt-in tool was enabled mid-run |
| `refs invalidated` | the page changed; remaining actions in the step were cancelled |

The automatic `[OBSV]` covers **one** tab. Reading another tab should appear as
`browser_observe` with a different `tab=` alias — not as an `activate`.

Final line breaks wall-clock into `model=` / `browser=` / `other=`. In practice
`model=` dominates, so reducing **round-trips** — not browser speed — is what
makes a run faster.

## Useful flags

```bash
--max-steps N     # cap a long run while testing
--capture         # force the screenshot tool on
--diagnose        # force the diagnostic tool on
--approve         # confirm before consequential actions (upload/drag)
--verbose         # one-line events instead of the full trace
```
