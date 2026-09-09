# AXIS — a browser agent that drives your real Chrome

AXIS is a two-agent (planner + navigator) browser automation agent. It does not
launch a throwaway test browser. It drives **the Chrome you already have open**,
with your real profile and your already-logged-in sessions, through a local
Chrome extension called **Browser Agent Bridge**.

You give it a task in plain English. It plans, opens/reads/clicks pages, verifies
what it did, and answers you.

```
You: "Open Hacker News and give me the top five stories with points and comments."

AXIS  →  planner decides a goal
      →  navigator observes the page, acts, verifies
      →  planner checks the evidence and answers
```

**What this repository contains**

| Path | What it is |
| --- | --- |
| [axis-agent/](axis-agent/) | The agent: planner, navigator, orchestrator, and the guarded browser tool layer |
| [axis-agent/axis.yaml](axis-agent/axis.yaml) | Every configurable value in one file (budgets, firewall, tool opt-ins) |
| [browser-agent-bridge-main/](browser-agent-bridge-main/) | The Chrome extension + native host that gives the agent access to Chrome |
| [axis-agent/manual_test_prompts.md](axis-agent/manual_test_prompts.md) | 12 ready-made workflow prompts, each with what to watch for in the trace |
| [axis-agent/evaluation_log.md](axis-agent/evaluation_log.md) | **Where you record your evaluation results** (see "Your task" below) |
| [axis-agent/README.md](axis-agent/README.md) | Deep technical reference for the browser tool layer |

---

## Before you start: what you need

You need **five** things. Three you install yourself, two I send you on Slack.

**Install yourself:**

1. **Google Chrome**, version 116 or newer.
2. **Python 3.12** (exactly 3.12 — the project pins it).
3. **uv**, the Python package manager. Install it with:
   - Windows (PowerShell): `powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

**I send you on Slack** (these are credentials, so they are deliberately **not**
in this repository):

4. Your **OCI API signing key** (a `.pem` private key file) and the four
   identity values that go with it.
5. The **AXIS model / project values** that go into your `.env` file.

Exactly which values, and exactly where to put them, is in
[Step 3](#step-3-add-your-credentials) below.

---

## Step 1 — Get the code and install the Python packages

```bash
git clone https://github.com/arshagya-ora/Axis_local_agent.git
cd Axis_local_agent
uv sync
```

`uv sync` reads [pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) and
creates a `.venv/` folder with the exact package versions everyone else is
using. Do not `pip install` anything by hand — the lock file is the source of
truth.

Check it worked:

```bash
uv run python -m pytest axis-agent/tests -q
```

You should see **208 passed**. These tests need no Chrome and no credentials —
if they pass, your Python side is set up correctly.

---

## Step 2 — Install the Chrome extension and its native host

The agent talks to Chrome through a local bridge. This is a one-time setup and
it has two halves: the extension (inside Chrome) and the native host (a small
Python program Chrome is allowed to talk to).

### 2a. Load the extension

1. Open `chrome://extensions` in Chrome.
2. Turn on **Developer mode** (toggle, top right).
3. Click **Load unpacked**.
4. Select the folder `browser-agent-bridge-main/extension` from this repository.
5. Copy the **extension ID** Chrome shows you. It looks like
   `lpemchcojepfkbgjgoehfknibdjjppig`. You need it in the next step.

### 2b. Install the native host

From the repository root, using the extension ID you just copied:

**Windows (PowerShell):**

```powershell
cd browser-agent-bridge-main
powershell -ExecutionPolicy Bypass -File .\scripts\install-native-host-win.ps1 <extension-id>
```

**macOS / Linux:**

```bash
cd browser-agent-bridge-main
./scripts/install-native-host-unix.sh <extension-id>
```

This does not need administrator rights. It registers the host for your user
only, and it generates your bridge access token at:

- Windows: `%USERPROFILE%\.browser-agent-bridge.env`
- macOS / Linux: `~/.browser-agent-bridge.env`

**You do not need to do anything with that token.** AXIS finds and reads it
automatically. It is generated on your own machine, it is unique to you, and it
is never shared or committed.

### 2c. Start the bridge and confirm it is connected

1. Go back to `chrome://extensions` and click **Reload** on the extension.
2. Open the extension's **side panel** (click the extension icon).
3. Accept the permission prompts it shows.
4. Click **Start Bridge**.
5. The side panel must say **Connected**.

Verify from the terminal:

```bash
cd browser-agent-bridge-main
python scripts/doctor.py --skip-live
```

> **Important:** the bridge only runs while the side panel's bridge control is
> started. If you click **Stop Bridge**, or fully quit Chrome, AXIS can no
> longer reach the browser and every run will fail with `BRIDGE_UNAVAILABLE`.
> Starting it again fixes it.

---

## Step 3 — Add your credentials

There are two separate credential systems, and they are unrelated to each other.

### 3a. The OCI signing key (this is the actual secret)

AXIS calls the model through Oracle Cloud's GenAI endpoint. Oracle does not use
a simple API key — it signs each request with a **private key file**, and the
config that points at it lives **outside this repository**, in your home
directory.

Create the folder and file:

- Windows: `C:\Users\<you>\.oci\config`
- macOS / Linux: `~/.oci/config`

Put your `.pem` private key file next to it (for example
`~/.oci/axis_api_key.pem`), then write `~/.oci/config` like this, filling in the
five values I send you on Slack:

```ini
[DEFAULT]
user=<user OCID — from Slack>
fingerprint=<key fingerprint — from Slack>
key_file=<full path to the .pem file you saved, e.g. C:\Users\you\.oci\axis_api_key.pem>
tenancy=<tenancy OCID — from Slack>
region=us-ashburn-1
```

Notes:

- `key_file` must be the **absolute path** on your machine. It is the one value
  that is different for each person.
- On macOS / Linux, lock the key down: `chmod 600 ~/.oci/axis_api_key.pem`.
  Oracle's SDK will refuse a world-readable key.
- **Never** copy the `.pem` file or the `~/.oci/` folder into this repository.
  `.gitignore` blocks `*.pem` and `.oci/` as a safety net, but the real rule is:
  it lives in your home directory, not in the project.

### 3b. The AXIS `.env` file (endpoint and model settings)

```bash
cd axis-agent
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
```

Then open `axis-agent/.env` and fill in the values. Here is what each one is:

| Variable | Required? | What to put | Comes from Slack? |
| --- | --- | --- | --- |
| `AXIS_OCI_GENAI_REGION` | Yes | `us-ashburn-1` — the region the endpoint URL is built from | No, use this value |
| `AXIS_OCI_GENAI_MODEL` | Yes | The model alias, e.g. `openai.gpt-5.4-mini` | **Yes** |
| `AXIS_OCI_GENAI_PROJECT_OCID` | Yes | The GenAI project OCID, a long `ocid1....` string | **Yes** |
| `AXIS_OCI_PROFILE` | No | Which profile in `~/.oci/config` to use. Leave as `DEFAULT` | No |
| `AXIS_OCI_GENAI_BASE_URL` | No | Full endpoint URL. Leave commented out — it is derived from the region | No |
| `AXIS_OCI_GENAI_API_VERSION` | No | Leave commented out | No |
| `BROWSER_AGENT_BRIDGE_HOST` / `_PORT` / `_TOKEN` | No | Leave all three commented out. AXIS defaults to `127.0.0.1:8765` and reads your own token file from Step 2b | No |

`axis-agent/.env` is gitignored. It will never be committed. If AXIS starts and
a required value is missing, it tells you exactly which variable to set.

---

## Step 4 — Run it

Make sure Chrome is open, the bridge side panel says **Connected**, and the tab
you want AXIS to start on is the **active tab**. Then:

```bash
cd axis-agent
uv run python -m axis.cli "what is the capital of France?"
```

That first task needs no browser at all — it proves your model credentials work.
Now try a real browsing task:

```bash
uv run python -m axis.cli --debug "Open Hacker News and give me the top five story titles."
```

`--debug` is the mode you want for evaluation. It prints the full trace: every
planner decision, every navigator step, every browser call with its arguments
and whether it passed, failed, or was refused. It also keeps the session open so
you can type follow-up questions.

### The flags that matter

| Flag | What it does |
| --- | --- |
| `--debug` | Full trace + interactive follow-ups. **Use this for evaluation.** |
| `--verbose` | One short line per event instead of the full trace |
| `--interactive` | Follow-up questions, without the full trace |
| `--max-steps N` | Stop a run after N navigator steps. Useful for capping runaway tasks |
| `--capture` | Force the screenshot/evidence tool on |
| `--diagnose` | Force the console/network diagnostic tool on |
| `--approve` | Ask you for confirmation before consequential actions (upload, drag) |
| `--config PATH` | Use a different config file instead of `axis.yaml` |

### Reading the trace

```
------------------------------------------------------------------------
[00:04.120] >> NAVIGATOR STEP  goal='read the front page story list'
[00:04.150]   [OBSV] tab=main (0.4s)
[00:05.900]   [OK  ] browser_observe.compact (1.7s)
```

| What you see | What it means |
| --- | --- |
| `[mm:ss.mmm]` | Time since the run started |
| `(1.7s)` | How long that one operation took |
| `[OK  ]` / `[FAIL]` | The call ran, and its result |
| `[SKIP]` | The runtime **refused** the call — it never reached Chrome |
| `[OBSV]` | The automatic page reading before each navigator step |
| `[TOOLS]` | An opt-in tool (screenshot, diagnostics, downloads) turned on mid-run |
| `refs invalidated` | The page changed, so the rest of that step was cancelled |

The last line of every run breaks the wall-clock time into `model=` /
`browser=` / `other=`. In practice `model=` dominates, so what makes a run
faster is **fewer round-trips**, not a faster browser.

### Tuning behaviour

Everything tunable is in [axis-agent/axis.yaml](axis-agent/axis.yaml) with a
comment explaining it. The ones you are most likely to touch while evaluating:

- `run.max_total_steps` (default 30) — the hard ceiling on a run
- `run.max_actions_per_step` (default 3) — browser calls allowed per step
- `run.planner_interval_steps` (default 3) — how often the planner re-plans
- `browser.max_open_tabs` (default 60) — raise it if you normally keep many tabs open
- `browser.firewall` — URL and method allow/deny rules

---

## Your task: evaluate AXIS on complex workflows

This is what I need from you. Please treat it as the deliverable, not the setup.

### What to test

Start with [axis-agent/manual_test_prompts.md](axis-agent/manual_test_prompts.md).
It has 12 prompts, and each one says what part of the agent it stresses and what
a healthy trace looks like. Run them all with `--debug`.

Then — this is the more valuable half — **write your own complex workflows** and
run those. What I specifically want stressed:

1. **Multi-step workflows with a real dependency chain** — where step 4 needs
   something the agent read at step 1. Does information survive across planner
   passes and tab switches?
2. **Multi-tab work** — reading three sources and combining them. Does it open
   tabs and read them *within* a step, or does every tab switch waste a step?
3. **Long mechanical sequences** — 10+ repetitive actions. Does planner cadence
   hold, or does it drift and lose the thread?
4. **Forms and verification** — filling something in, then proving it is
   actually on screen before claiming success.
5. **Failure and recovery** — dead links, 500 pages, missing elements, stale
   references after a page re-renders. Does it recover, or does it loop?
6. **Things it should refuse or ask about** — missing credentials, genuinely
   ambiguous requests. It should ask or stop cleanly, not guess and not loop.
7. **Sites we actually care about** — internal tools, dashboards, Oracle
   properties, whatever is realistic for your work. Real sites break agents in
   ways that hacker-news-style test pages never will.
8. **Any public benchmark you want to point at it** — WebArena, Mind2Web,
   WebVoyager, GAIA browsing tasks, or your own suite. Note the harness you
   used so results are comparable.

Please keep tests **non-destructive**: no sending, no purchasing, no posting, no
submitting anything real. Every prompt in `manual_test_prompts.md` is written
that way on purpose.

### How to report back

**Record your results in this repository, in
[axis-agent/evaluation_log.md](axis-agent/evaluation_log.md).** That file has a
template and an example. For each workflow you run, log:

- The exact prompt you gave it
- Pass / partial / fail, and *why*
- The final `steps=` / `planner_passes=` / `browser_actions=` /
  `model_requests=` counts, and the `time:` line
- For a failure: the relevant slice of the `--debug` trace, and what you think
  went wrong
- Anything surprising, even if it passed

Then open a pull request against `main` with just your additions to that file,
or push to a branch named `eval/<your-name>`. Please **don't** commit your
`.env`, your `.pem`, screenshots of credentials, or traces containing personal
account data — check `git status` before you commit. Ping me on Slack when your
PR is up.

If a workflow fails in a way that looks like a real bug rather than a model
limitation, log it in `evaluation_log.md` **and** message me directly — those
are the ones I want to fix first.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `BRIDGE_UNAVAILABLE` on every call | The bridge is not running. Open the extension side panel and click **Start Bridge**. It must say Connected. |
| `NO_MANAGED_TAB` | No usable Chrome tab was found. Make sure Chrome is open with at least one ordinary tab, and that the tab you want is the active one. |
| `Missing provider configuration: set AXIS_...` | That variable is missing from `axis-agent/.env`. See the table in [Step 3b](#3b-the-axis-env-file-endpoint-and-model-settings). |
| An OCI authentication or signing error | `~/.oci/config` is wrong. Most often `key_file` points at a path that does not exist, or the fingerprint does not match the key. |
| `Could not find config file` from the OCI SDK | `~/.oci/config` does not exist yet. See [Step 3a](#3a-the-oci-signing-key-this-is-the-actual-secret). |
| Extension shows an error after `git pull` | Reload the extension at `chrome://extensions`, then Start Bridge again. |
| Native host not found / not registered | Re-run the installer from Step 2b with the current extension ID, then reload the extension. |
| Garbled or missing characters in terminal output | Only affects display. Run with `PYTHONIOENCODING=utf-8` if it bothers you. |
| Tests fail right after `uv sync` | You are probably not on Python 3.12. Check `uv run python --version`. |
| `TAB_LIMIT` errors | You have more tabs open than `browser.max_open_tabs` in `axis.yaml`. Raise it or close tabs. |

---

## What is and is not in this repository

**Committed here:** all source code, all 208 tests, `axis.yaml`, the
`.env.example` template, the vendored Browser Agent Bridge extension, the test
prompts, and this guide.

**Never committed** — and blocked by [.gitignore](.gitignore):

- `axis-agent/.env` — your filled-in environment values
- `~/.oci/config` and any `.pem` private key — these live in your home directory
- `~/.browser-agent-bridge.env` — your locally generated bridge token
- `.venv/`, `__pycache__/`, `.pytest_cache/` — rebuild with `uv sync`
- Run output: screenshots, PDFs, DOM snapshots, traces, downloads, debug logs
- Local editor and agent config (`.claude/`, `.vscode/`, `.idea/`)

If you ever find yourself about to commit a credential, stop and tell me instead.
Rotating a leaked signing key is a bigger job than asking.
