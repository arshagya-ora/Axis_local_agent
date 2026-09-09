# AXIS evaluation log

**This is where you record your evaluation results.** One section per person,
one entry per workflow you ran. Append — don't rewrite anyone else's section.

Setup instructions are in the [root README](../README.md). The 12 starter
prompts are in [manual_test_prompts.md](manual_test_prompts.md). Write your own
complex workflows too — those are the more valuable half.

Run everything with the full trace so the numbers below are available:

```bash
cd axis-agent
uv run python -m axis.cli --debug "<your prompt>"
```

## Before you commit

- Check `git status`. Never commit `.env`, a `.pem` key, or `~/.oci/` contents.
- Redact anything personal from a pasted trace: real email addresses, account
  names, internal URLs you shouldn't share, session tokens.
- Open a PR against `main` with only your additions to this file, or push a
  branch named `eval/<your-name>`. Then ping Arshagya on Slack.

## Entry template

Copy this block for each workflow.

```markdown
### <short name for the workflow>

- **Date:** YYYY-MM-DD
- **Model:** <value of AXIS_OCI_GENAI_MODEL>
- **Category:** multi-step chain | multi-tab | long sequence | form + verify |
  failure recovery | should-refuse | real site | public benchmark
- **Prompt:**
  > <the exact prompt, verbatim>
- **Result:** PASS | PARTIAL | FAIL
- **What happened:** <2-4 sentences. For PARTIAL/FAIL, say what it got wrong,
  not just that it failed.>
- **Counts:** steps=__ planner_passes=__ browser_actions=__ model_requests=__
- **Time:** total=__s model=__s browser=__s other=__s
- **Suspected cause (FAIL only):** model limitation | orchestrator/cadence bug |
  browser tool bug | bridge/extension issue | prompt was ambiguous | unclear
- **Trace excerpt (FAIL only):**
  ```
  <the 10-30 relevant lines from the --debug output>
  ```
- **Notes:** <anything surprising, including on a PASS>
```

The `Counts:` and `Time:` lines come straight from the last two lines AXIS
prints at the end of every run.

---

## Example entry (delete nothing — this one is here as a reference)

### Hacker News top-five extraction

- **Date:** 2026-09-09
- **Model:** openai.gpt-5.4-mini
- **Category:** multi-step chain
- **Prompt:**
  > Go to Hacker News and give me the title, points, and comment count for the
  > top five stories on the front page, as a table.
- **Result:** PASS
- **What happened:** Navigated to the front page, took one compact observation,
  and produced the table from that single read. No redundant clicking.
- **Counts:** steps=2 planner_passes=2 browser_actions=3 model_requests=4
- **Time:** total=31.4s model=27.1s browser=3.2s other=1.1s
- **Suspected cause (FAIL only):** n/a
- **Notes:** Good shape — a rich single observation instead of many small
  actions is exactly what this prompt is checking for. Watch for
  `ACTION_BUDGET_EXHAUSTED` here; it would mean the navigator is over-clicking.

---

# Results

<!-- Add your own H2 section below with your name, then your entries under it. -->

## <your name>

_No entries yet._
