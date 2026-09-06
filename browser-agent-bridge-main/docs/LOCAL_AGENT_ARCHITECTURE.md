# Local Agent Architecture — Design Document

> **Status:** Design / Pre-implementation
> **Version:** 0.1-draft
> **Date:** 2026-08-25
> **Author:** Arshagya Shrivastava
> **Relates to:** `browser-agent-bridge` (this repo), and supersedes the browser-side
> agent design in `AXIS_v3_Architecture.md` (external reference, 2026-08-09)

---

## Table of Contents

1. [Purpose](#1-purpose)
2. [Key Reframe — the Bridge Already Solved Half of This](#2-key-reframe--the-bridge-already-solved-half-of-this)
3. [Design Principles](#3-design-principles)
4. [High-Level Architecture](#4-high-level-architecture)
5. [Component Reference](#5-component-reference)
6. [Mapping v3 Concepts onto This Repo](#6-mapping-v3-concepts-onto-this-repo)
7. [Data Flow — Full Task Execution](#7-data-flow--full-task-execution)
8. [Security Model](#8-security-model)
9. [Deployment](#9-deployment)
10. [Proposed Repository Layout](#10-proposed-repository-layout)
11. [Open Decisions](#11-open-decisions)

---

## 1. Purpose

An earlier design (`AXIS_v3_Architecture.md`) proposed moving LLM
orchestration out of a Chrome extension and into a local Python agent
connected to the browser over a purpose-built WebSocket bridge. That document
assumed the bridge itself — extension, native host, WS server, DOM
perception, action execution, firewall — still needed to be built.

It doesn't. `browser-agent-bridge` (this repo) **is** that bridge, already
built and more capable than the v3 spec: ref-based accessibility-tree
perception, `whatChanged` action deltas, whole-browser access by default with
opt-in tab-group session isolation, per-method runtime approval, workflow
recording, and a JSON-RPC surface far richer than a 20-verb action enum.

This document designs the piece that's actually missing: **a local agent
process that runs outside the browser and drives it as a client of the
existing bridge**, instead of designing a new browser-control layer.

---

## 2. Key Reframe — the Bridge Already Solved Half of This

| AXIS v3 component | Status in `browser-agent-bridge` |
|---|---|
| WebSocket Server (`127.0.0.1:7770`, auth token) | **Already built** — `native/host.py` serves HTTP + WS JSON-RPC on `127.0.0.1:8765`, bearer-token auth, CORS locked to the extension origin |
| Background Script (message router) | **Already built** — `extension/service-worker.js` + `extension/sw/*` |
| Content Script (DOM relay + action executor) | **Already built, and richer** — `page.accessibilityTree` (ref-minted, `format:"compact"`), `locator.*Ref`, `computer.*`, `dom.*`, all CDP-backed |
| Firewall Service | Present as `extension/sw/policy.js` + `method-policy.js` — chrome://, chrome-extension://, and Chrome Web Store are hard-blocked; sensitive methods require runtime approval. Lives in the extension rather than a separate server process, but is equally non-bypassable from a browser-side client |
| Session isolation | **Configurable, stronger than v3 when enabled** — `session.start` / `session.stop` create Chrome tab groups (organizational by default); `policy.set({unscopedTabAccess:false})` turns the group into an enforced boundary, rejecting calls outside it |
| Action vocabulary | **Richer than v3's 20 flat actions** — locator-style calls (`click`, `fill`, `press`, `check`, `selectOption`, drag/drop) plus ref-based variants, raw `computer.*` for canvas/non-DOM work, and the full `page.*` navigation/wait/emulation surface |
| Planner Agent / Navigator Agent / LLM Factory / Session Manager / Tool Registry / Memory Store | **Does not exist yet** — this is the actual scope of this document |

The new agent is *only* the intelligence layer. It talks to
`ws://127.0.0.1:8765/ws` (or `POST /rpc` for one-shot calls) the same way
`scripts/browser_bridge_client.py` already does — that client is grown into
a full autonomous step loop instead of staying a manual scripting helper.

---

## 3. Design Principles

| Principle | Implementation |
|---|---|
| **Don't rebuild what exists** | The bridge owns browser access, perception, action execution, session isolation, and approval. The agent never talks to Chrome directly — only to the bridge's RPC surface. |
| **Separation of concerns** | Agent = intelligence (LLM calls, planning, memory). Bridge = browser interface. No overlap, no duplicated auth or firewall logic. |
| **Agent is a client, not a server** | No new WebSocket server, no new auth scheme. Reuse the bridge's existing bearer token from `~/.browser-agent-bridge.env`. |
| **Perception via the bridge's a11y model** | Reason over `ref` ids from `page.accessibilityTree` / `page.ariaSnapshot`, not a bespoke DOM-index scheme. |
| **Actions are RPC calls, not a new enum** | The Navigator's action schema is a discriminated union over a curated subset of existing JSON-RPC methods, each dispatched as one bridge call. |
| **No browser-side Docker requirement** | The agent makes no direct browser/DOM/CDP calls, so it doesn't need a container for browser access. It's a plain local process dialing out to `127.0.0.1:8765`. |
| **Framework-agnostic** | LLM orchestration framework (LangGraph, Pydantic AI, raw async Python) is an internal detail — the RPC protocol against the bridge is unaffected by that choice. |

---

## 4. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  LOCAL AGENT PROCESS  (new — plain Python process; no browser     │
│  access of its own, so no Docker requirement for browser control) │
│                                                                    │
│   ┌──────────────┐   ┌───────────────┐   ┌──────────────────┐    │
│   │ Session Mgr  │──►│ Planner Agent │──►│ Navigator Agent  │    │
│   │ (step loop)  │   │ (LLM, every N │   │ (LLM, every step)│    │
│   │              │   │  steps)       │   │                  │    │
│   └──────┬───────┘   └───────────────┘   └────────┬─────────┘    │
│          │                                         │              │
│          │                                 actions[] — mapped     │
│          │                                 onto bridge RPC calls  │
│          ▼                                         ▼              │
│   ┌──────────────┐                        ┌──────────────────┐   │
│   │ Memory Store │                        │ Bridge RPC Client│   │
│   │ (SQLite —    │                        │ (persistent WS   │   │
│   │  agent-owned,│                        │  to /ws, reuses  │   │
│   │  separate    │                        │  bearer token    │   │
│   │  from the    │                        │  from ~/.browser-│   │
│   │  bridge)     │                        │  agent-bridge.env)│  │
│   └──────────────┘                        └────────┬─────────┘   │
│                                                      │             │
│   ┌──────────────┐   ┌───────────────┐              │             │
│   │ LLM Factory  │   │ Tool Registry │              │             │
│   │ OpenAI /     │   │ read_file /   │              │             │
│   │ Gemini /     │   │ write_file /  │              │             │
│   │ Ollama / ... │   │ run_shell /   │              │             │
│   │              │   │ http_request  │              │             │
│   └──────────────┘   └───────────────┘              │             │
└──────────────────────────────────────────────────────┼───────────┘
                                                         │
                                        ws://127.0.0.1:8765/ws
                                        (Bearer token from
                                         browser-agent-bridge)
                                                         │
                                                         ▼
                          ┌────────────────────────────────────────┐
                          │  EXISTING browser-agent-bridge          │
                          │  native/host.py  ◄──stdio──►  extension │
                          │  (unchanged — this repo)                 │
                          │                                          │
                          │  session.*  page.*  locator.*  dom.*     │
                          │  computer.*  keyboard.*  downloads.*     │
                          │  recording.*  trace.*                    │
                          └────────────────────────────────────────┘
                                                         │
                                              Chrome APIs + CDP
                                                         │
                                                         ▼
                                          User's real Chrome browser
                                     (logged-in sessions, cookies, tabs)
```

---

## 5. Component Reference

### 5.1 Bridge RPC Client

**Role:** The agent's only channel to the browser. Wraps the existing
`scripts/browser_bridge_client.py` pattern into a long-lived, event-aware
connection instead of one-shot calls.

**Responsibilities:**
- Open a persistent WebSocket to `ws://127.0.0.1:8765/ws`, auto-loading the
  bearer token from `~/.browser-agent-bridge.env` (or `%USERPROFILE%\.browser-agent-bridge.env` on Windows) — same mechanism `BrowserBridgeClient` already uses.
- Reconnect with backoff if the bridge stops (e.g. user clicks *Stop Bridge*
  in the side panel); surface that state to the Session Manager so a running
  task can pause rather than fail silently.
- Send JSON-RPC requests (`session.start`, `page.accessibilityTree`,
  `locator.clickRef`, ...) and correlate responses by id — same envelope the
  host already speaks, no new protocol.
- Subscribe to the bridge's event stream (`bridge.subscribe` with `tabIds`)
  for console/network events and `whatChanged` deltas relevant to the active
  session's tab.
- Do **not** re-implement auth, CORS, or origin checks — those live in
  `native/host.py` and `extension/sw/policy.js` and are out of scope here.

### 5.2 Session Manager

**Role:** Orchestrates the agent execution loop. One instance per active
task, mapped 1:1 onto a bridge session.

**Responsibilities:**
- `create_session(task_id, prompt)` → calls `session.start` on the bridge to
  get an organizational tab group + `tabId` (whole-browser access is the
  bridge default, so this grouping is for tracking/UI, not isolation unless
  `unscopedTabAccess` has been turned off); stores `sessionId`/`tabId` for the
  life of the task.
- `resume_session(session_id)` → reload conversation + step history from the
  agent's own SQLite; call `session.get` to confirm the bridge session is
  still alive (re-create it if not).
- `pause_session` / `cancel_session` → local state transitions; call
  `session.stop` on cancel.
- Drive the step loop: request perception → call Planner (if interval
  reached) → call Navigator → dispatch actions via the Bridge RPC Client →
  persist step → check completion → repeat.
- Track `current_step`, `max_steps` (default 100), `consecutive_failures`
  (abort after 3), `actions_per_step` (max 10) — same guardrails as v3.

### 5.3 Planner Agent

**Role:** High-level reasoning, run every `planning_interval` steps
(default 3). Unchanged in shape from the v3 design.

**Output schema:**
```python
class PlannerOutput(BaseModel):
    observation: str
    challenges: list[str]
    next_steps: list[str]
    done: bool
    final_answer: str | None
    reasoning: str
```

### 5.4 Navigator Agent

**Role:** Low-level action generation, run every step. **This is where the
design diverges most from v3.**

**Perception input** is the bridge's `page.accessibilityTree` (with
`format: "compact"`) or `page.ariaSnapshot` — a ref-minted role/name tree,
not a custom annotated-DOM walk. The system prompt must teach the model the
bridge's interaction model: act on `ref` ids via `locator.clickRef` /
`fillRef` / `pressRef` / `hoverRef` / `selectOptionRef` rather than
re-deriving CSS selectors, and fall back to `computer.*` coordinate actions
only for canvas/non-DOM surfaces.

**Output schema:**
```python
class NavigatorState(BaseModel):
    next_goal: str

class NavigatorOutput(BaseModel):
    current_state: NavigatorState
    actions: list[BridgeAction]   # discriminated union, see 5.4.1
```

#### 5.4.1 Action vocabulary — mapped onto existing bridge RPC methods

Instead of a bespoke 20-verb enum, the Navigator's action union targets a
curated subset of the bridge's JSON-RPC methods directly:

| Agent action | Bridge RPC method |
|---|---|
| `navigate` | `page.navigate` |
| `go_back` / `go_forward` | `page.goBack` / `page.goForward` |
| `click_ref` | `locator.clickRef` |
| `fill_ref` | `locator.fillRef` |
| `press_ref` | `locator.pressRef` |
| `hover_ref` | `locator.hoverRef` |
| `select_option_ref` | `locator.selectOptionRef` |
| `check` / `uncheck` | `locator.check` / `locator.uncheck` |
| `wait_for_url` | `page.waitForURL` |
| `wait_for_network_idle` | `page.waitForNetworkIdle` |
| `read_text` | `page.readText` |
| `screenshot` | `page.screenshot` |
| `key_press` | `keyboard.press` |
| `computer_click` / `computer_scroll` | `computer.click` / `computer.scroll` (canvas / non-DOM only) |
| `wait_for_download` | `downloads.waitFor` |
| `done` | — (no RPC call; signals task completion) |

Each action dispatch is one bridge RPC call; the response's `whatChanged`
delta (URL, popups, focus, optional a11y diff) is fed back into the step
record instead of a bespoke `action.result` payload.

### 5.5 Action Dispatcher

**Role:** Validates, sequences, and delivers actions to the Bridge RPC
Client. Awaits results.

**Responsibilities:**
1. Validate each `BridgeAction` with its Pydantic schema.
2. Translate it into the corresponding bridge JSON-RPC request (§5.4.1),
   scoped to the session's `tabId`.
3. Send via the Bridge RPC Client, await the response (bridge-side timeout
   is already configurable per-call via `timeoutMs`).
4. On error or approval-pending state, surface it to the Session Manager —
   the bridge itself will have already prompted the user via its approval
   popup if the method required it; the agent does not need its own
   firewall check beyond a cheap client-side reject for obviously
   disallowed schemes (`chrome://`, `javascript:`) to fail fast without a
   round trip.
5. Collect results into `list[ActionResult]` for the step record.

### 5.6 LLM Factory

Unchanged from the v3 design: creates and caches model instances for
Planner and Navigator, reads API keys exclusively from environment
variables, supports OpenAI, Gemini, Ollama, Groq, xAI, OpenRouter, and
custom OpenAI-compatible endpoints. Keys never reach the bridge or the
browser — they don't need to, since the agent never asks the bridge to
carry credentials for external services.

### 5.7 Tool Registry

Unchanged from the v3 design — this is genuinely new capability not
provided by the bridge: `read_file`, `write_file`, `run_shell`
(allowlisted), `http_request`, `cache_lookup`. `list_tabs` can now be
implemented as a thin wrapper over the bridge's `tabs.list` RPC method
instead of a bespoke tool.

### 5.8 Memory Store (SQLite)

Agent-owned, separate from the bridge's own in-memory event buffer.
Same schema shape as the v3 design (`sessions`, `messages`, `agent_steps`,
`llm_calls`, `settings`, `tool_cache`), **minus** `firewall_rules` — the
bridge already owns URL/method policy via `extension/sw/policy.js` and
`method-policy.js`, so the agent does not need a parallel copy.

---

## 6. Mapping v3 Concepts onto This Repo

| v3 concept | This design |
|---|---|
| Extension WS server (7770) | Bridge's existing `native/host.py` server (8765) — reused, not rebuilt |
| Extension-side DOM relay | Bridge's `page.accessibilityTree` / `page.ariaSnapshot` — reused |
| Extension-side action executor | Bridge's `locator.*`, `dom.*`, `computer.*`, `keyboard.*` — reused |
| Firewall Service (server-side, agent process) | Bridge's `policy.js` / `method-policy.js` (extension-side) — functionally equivalent, non-bypassable from a client, so not duplicated |
| Auth token lifecycle | Bridge's `~/.browser-agent-bridge.env` token — reused as-is |
| Session isolation | Bridge's tab-group `session.*` methods — reused; stronger than v3's spec when `unscopedTabAccess` is turned off, organizational-only otherwise (the bridge's default) |
| `event.stream` to a side panel | Agent owns its own UI/output channel (CLI, local web UI, etc.) since it no longer runs inside the browser — see §9 |
| Docker mandate for the whole stack | Only needed if you want to containerize the *agent's* Python deps; the browser-control half needs no container since it already runs as a native host + extension |

---

## 7. Data Flow — Full Task Execution

```
USER          AGENT (Session Mgr)      Planner       Navigator      Bridge RPC Client      browser-agent-bridge
 │                    │                    │              │                  │                      │
 │  task prompt        │                    │              │                  │                      │
 ├───────────────────►│                    │              │                  │                      │
 │                    │ session.start ─────────────────────────────────────────────────────────────►│
 │                    │◄──── sessionId, tabId ─────────────────────────────────────────────────────┤
 │                    │                    │              │                  │                      │
 │                    │  ┌───────────── STEP LOOP ─────────────────────────────────────────────┐    │
 │                    │  │ page.accessibilityTree(tabId, format=compact) ─────────────────────►│    │
 │                    │  │◄──── ref-tagged tree, URL ───────────────────────────────────────────┤    │
 │                    │  │                    │              │                  │               │    │
 │                    │  │ [if planning step] │              │                  │               │    │
 │                    │  ├──────────────────►│              │                  │               │    │
 │                    │  │◄── PlannerOutput ──┤              │                  │               │    │
 │                    │  │                    │              │                  │               │    │
 │                    │  ├───────────────────────────────►│                  │               │    │
 │                    │  │◄──────── NavigatorOutput.actions[] ┤                  │               │    │
 │                    │  │                    │              │                  │               │    │
 │                    │  │  for each action → mapped RPC call ───────────────►│               │    │
 │                    │  │                                                     │  locator.clickRef   │
 │                    │  │                                                     │  / page.navigate /  │
 │                    │  │                                                     │  etc. ──────────────►│
 │                    │  │                                                     │◄── whatChanged ─────┤
 │                    │  │◄──── ActionResult(s) ───────────────────────────────┤                      │
 │                    │  │                    │              │                  │               │    │
 │                    │  │  persist step → agent SQLite       │                  │               │    │
 │                    │  └───────────────── repeat until done or max_steps ─────────────────────┘    │
 │                    │                    │              │                  │                      │
 │                    │ session.stop ──────────────────────────────────────────────────────────────►│
 │  final answer       │                    │              │                  │                      │
 │◄───────────────────┤                    │              │                  │                      │
```

---

## 8. Security Model

| Threat | Mitigation |
|---|---|
| API key theft | Keys live only in the agent process's environment (`.env` or OS env vars) — never sent to the bridge or the browser |
| Unauthorized bridge access | Existing bearer-token check in `native/host.py`; the agent is just another authenticated client, no new surface |
| Malicious navigation / scope escape | The bridge grants whole-browser access by default, so this is **not** mitigated by tab-group isolation out of the box. Mitigations, in order of intended primary use: (1) an agent-side firewall in `axis-agent/browser_agent_tools.py` — planned via its existing `ScopeProvider` seam, not yet implemented — is meant to be the primary place per-task/per-agent domain restrictions are enforced; (2) the bridge's `policy.js` URL/method allow-deny lists (`blockedUrlPatterns`, `blockedMethods`, etc.) are always enforced independent of tab scoping and are the right place for a restriction that must hold no matter which client is calling; (3) `policy.set({unscopedTabAccess:false})` restores the old hard tab-group boundary when nothing but full isolation will do. Method policy cannot be bypassed from the client side in any of these modes |
| Sensitive operations (downloads, cookies, policy changes) | Already gated by the bridge's runtime approval popup — the agent must handle an "approval pending" response gracefully (retry after user action, or surface it to the operator) rather than assuming synchronous completion |
| Runaway agent | Hard limits in the Session Manager: max steps, max actions/step, abort after N consecutive failures — same as v3 |
| Data exfiltration via LLM | All LLM calls logged in the agent's own `llm_calls` table; outbound HTTP from the `http_request` tool should be reviewed/allowlisted separately from bridge policy, since it's a new capability the bridge doesn't gate |
| Local-only network exposure | Both the bridge (`127.0.0.1:8765`) and the agent (no listening port required at all, since it's a pure client) stay off any network interface |

---

## 9. Deployment

Because the agent makes no direct browser/CDP calls, it does **not** need
Docker for browser access — that requirement in the v3 doc was inherited
from an assumption (agent controls the browser directly) that no longer
holds now that the bridge does that job. The agent can be:

- A plain Python process (`venv` + `pip install`) started manually or via
  OS service manager (systemd / launchd / Windows Task Scheduler), reading
  its LLM keys from a local `.env`.
- Optionally containerized purely to pin Python dependency versions —
  in that case the container needs no browser-related mounts, capabilities,
  or ports; it only needs outbound access to `127.0.0.1:8765` on the host
  (via `host.docker.internal` or host networking) and to whichever LLM
  provider APIs are configured.

**Prerequisite:** the bridge must already be running (`Start Bridge` in the
side panel) before the agent process starts — same operational dependency
`scripts/browser_bridge_client.py health` already has today.

---

## 10. Proposed Repository Layout

New top-level directory in this repo (or a sibling repo, if the agent
should ship independently of the bridge's release cadence):

```
agent/                          ← NEW
├── pyproject.toml
├── .env.example                ← LLM provider keys; bridge token is read
│                                  from ~/.browser-agent-bridge.env, not here
├── src/
│   ├── main.py                 ← entry point
│   ├── bridge_client.py        ← persistent-WS wrapper around the pattern
│   │                              in scripts/browser_bridge_client.py
│   ├── session.py               ← SessionManager: step loop, session.start/stop
│   ├── agents/
│   │   ├── planner.py
│   │   └── navigator.py         ← maps NavigatorOutput → bridge RPC calls
│   ├── actions/
│   │   ├── dispatcher.py
│   │   └── schemas.py           ← BridgeAction discriminated union (§5.4.1)
│   ├── llm/
│   │   └── factory.py
│   ├── memory/
│   │   └── store.py             ← agent-owned SQLite
│   ├── tools/
│   │   └── registry.py          ← read_file / write_file / run_shell / http_request
│   └── prompts/
│       ├── planner.py
│       └── navigator.py         ← must teach the model the bridge's
│                                    ref-based locator model, not a
│                                    generic DOM-index model
└── tests/
```

---

## 11. Open Decisions

| Decision | Options | Notes |
|---|---|---|
| Agent process framework | LangGraph / Pydantic AI / raw async Python | Internal detail — bridge protocol is unaffected |
| Where the agent's own UI lives | CLI only / local web UI / desktop app (Electron/Tauri) | No longer constrained to a Chrome side panel since the agent runs outside the browser |
| Containerize the agent | Yes (deps pinning only) / No (plain venv) | No browser-related reason to containerize post-bridge; purely a packaging preference |
| Client-side pre-checks before dispatch | Mirror a minimal deny-list locally for fast-fail / rely entirely on bridge-side `policy.js` | Bridge enforcement can't be bypassed either way; local pre-check is a latency optimization only |
| Repo placement | `agent/` inside `browser-agent-bridge` / separate sibling repo | Depends on whether the agent should version and release independently of the bridge |
| Multi-session concurrency | One agent process per browser session / one process managing several sessions concurrently (async) | Bridge already supports multiple concurrent `session.start` tab groups, so the agent *can* run several tasks in parallel if desired |

---

*Local Agent Architecture — Design Document — v0.1-draft — 2026-08-25*
