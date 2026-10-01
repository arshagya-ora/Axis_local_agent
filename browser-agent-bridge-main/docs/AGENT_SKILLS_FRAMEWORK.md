# Agent Skills Framework — Design Addendum

> Companion to `docs/LOCAL_AGENT_ARCHITECTURE.md`. That document designs the
> agent's execution loop (Session Manager / Planner / Navigator / Bridge RPC
> Client). This document designs the piece it left thin: how the agent's
> **capabilities are packaged as pluggable skills**, how **prompts stay small**
> by leaning on those skills instead of being crammed into one system prompt,
> and how **long-running tasks persist state** across two different memory
> mechanisms.
>
> Status: Design / Pre-implementation. Builds on patterns already live in this
> repo — `skills/browser-agent-bridge/` (SKILL.md → `references/protocol.md`
> progressive disclosure) and `runtime/site-patterns/{domain}.md` (durable
> learned knowledge) — instead of inventing new ones.

---

## 1. Problem with "one big prompt"

A single system prompt that teaches the model every tool, every workflow,
and every troubleshooting rule up front does three things badly:

1. It burns context on capabilities irrelevant to the current task.
2. It can't be edited or shipped per-capability — changing browser-bridge
   guidance risks touching shell/http/file guidance in the same blob.
3. It can't be turned off. There's no way to run the agent without, say,
   shell exec, short of hand-editing the prompt.

The fix is the same one `skills/browser-agent-bridge/SKILL.md` already
applies at the single-skill level: **a short index the model always sees,
and deep material it loads only when a skill becomes relevant.** This
doc generalizes that to every capability the agent has, including
non-browser ones (shell, http, file I/O).

---

## 2. Skill = the unit of pluggability

A skill is a self-contained folder. Nothing about the agent core knows what
skills exist at build time — it discovers them at boot by scanning a
directory.

```
agent/skills/<skill-name>/
├── SKILL.md            # required — frontmatter + short quick-start body
├── tools.py             # optional — typed tool functions this skill owns
├── references/          # optional — deep docs, loaded on demand only
│   └── protocol.md
└── memory/               # optional — durable, human/agent-curated knowledge
    └── *.md
```

`SKILL.md` frontmatter is the contract the registry reads:

```yaml
---
name: browser-agent-bridge
description: Control the user's local Chrome via the Browser Agent Bridge JSON-RPC surface.
triggers: ["browse", "click", "screenshot", "extract page", "navigate to"]
capabilities: ["session.*", "page.*", "locator.*", "dom.*", "computer.*"]
enabled: true
version: 1.1.0
---
```

This is deliberately the same shape as the existing
`skills/browser-agent-bridge/SKILL.md` frontmatter, extended with
`triggers` (routing hints) and `capabilities` (the tool/RPC namespaces this
skill owns — see §4).

### Pluggability rules

- **Adding a skill** = dropping a folder under `agent/skills/`. No agent
  core code changes.
- **Removing a skill** = deleting or moving out the folder. Any tool calls
  that depended on it simply stop being offered to the model.
- **Disabling without deleting** = `enabled: false` in frontmatter, or an
  allowlist file `agent/skills.enabled.json` (same shape as this repo's
  existing `.agents/skills.json`, which already lists skill entries by
  path). The allowlist wins for ops-level control; frontmatter wins for
  author-level default state.
- **A broken skill never crashes the agent.** The registry validates each
  `SKILL.md` at scan time (required fields present, `tools.py` importable);
  a failure excludes that one skill and logs a warning, the rest still load.

---

## 3. Prompt organization: index now, body on demand

`agent/src/core/prompt_builder.py` assembles the system prompt from two
layers only:

1. **Always resident** — identity, safety/operating rules, loop mechanics
   (same content shape as this repo's "Operating Rules" section in
   `skills/browser-agent-bridge/SKILL.md`, but capability-agnostic), and the
   **skill index**: one line per enabled skill (`name` + `description`).
   This is the entire capability surface the model sees by default — a
   table of contents, not a manual.

2. **Loaded on activation** — a skill's full `SKILL.md` body (and, if the
   skill itself references deeper material, its `references/*.md`, exactly
   like the browser-bridge skill already points from its quick-start into
   `references/protocol.md`) is injected into context only when that skill
   activates for the current task.

Activation happens when:

- The task prompt or the latest step observation matches one of the
  skill's `triggers`, or
- The model explicitly calls a `skill.activate(name)` meta-tool.

Deactivation (dropping the skill's prompt weight and tool surface) happens
after `N` steps with no calls into that skill's `capabilities`, so a
long-running multi-phase task doesn't accumulate every skill it ever
touched into permanent context. Log every activation/deactivation — that
log is what makes a resumed session cheap to re-hydrate (§5).

This means prompts are organized **by skill folder**, not by a single
growing system-prompt file, and each one is independently reviewable,
versionable, and testable — the same way `references/protocol.md` is
already reviewable independent of `SKILL.md`.

---

## 4. Tools are generated from skills, not hand-maintained in the core

Each skill's `tools.py` (or `tools.json` for skills with no code, just RPC
passthroughs) declares the typed functions/RPC wrappers it owns. The
`capabilities` list in `SKILL.md` frontmatter is the *namespace* claim
(`session.*`, `page.*`, ...); `tools.py` is the concrete implementation.

The Action Dispatcher (per `LOCAL_AGENT_ARCHITECTURE.md` §5.5) only offers
the model tools belonging to **currently active** skills — not the full set
across every installed skill. This is what keeps the function-calling
surface small on a long task that starts with browser work and later needs
a file write: the file-ops skill's tools don't exist in the model's view
until that skill activates.

Practical benefit: unplugging a skill folder removes its tools atomically,
with no separate tool-registry file to edit — one source of truth per
capability.

---

## 5. Two memory mechanisms, two jobs

Long-running tasks need both a resumable execution log and durable
cross-run knowledge. Conflating them (as a single "memory" store) makes
both worse. Keep them separate, matching a split this repo already has
implicitly:

| | SQLite (`agent/data/agent.db`) | `memory/*.md` files |
|---|---|---|
| **Answers** | "What happened, and where do I resume?" | "What did I learn that's reusable next time?" |
| **Written** | Every step, mechanically | End of task, selectively, by the agent deciding something is reusable |
| **Scope** | Per session/task | Per skill, cross-session |
| **Existing precedent in this repo** | none yet — new | `runtime/site-patterns/{domain}.md`, already governed by the "Domain Experience Accumulation" rule in `skills/browser-agent-bridge/SKILL.md` |
| **Analogy** | flight recorder | field notes |

### 5.1 SQLite schema

Extends `LOCAL_AGENT_ARCHITECTURE.md` §5.8 with one new table
(`skill_activations`) that makes resume cheap:

```sql
sessions(id, task, status, bridge_session_id, created_at, updated_at)
messages(id, session_id, role, content, created_at)
agent_steps(id, session_id, step_num, planner_output, navigator_output, created_at)
tool_calls(id, step_id, skill, tool_name, params, result, status, duration_ms)
skill_activations(id, session_id, skill_name, step_num, activated, reason, created_at)
llm_calls(id, session_id, model, tokens_in, tokens_out, cost, created_at)
settings(key, value)
```

`skill_activations` records both activation and deactivation events
(`activated: bool`). Resuming a session replays this table to re-activate
only the skills that were live at the point the session paused, instead of
either reloading every installed skill or none.

### 5.2 memory/*.md files

Each skill owns its own `memory/` subfolder. For `browser-agent-bridge`
this *is* `runtime/site-patterns/` promoted under the skill (or left in
place and referenced — either works, don't duplicate it). The rule the
agent already follows for site patterns generalizes directly to every
skill:

- Write only stable, reusable knowledge (selectors, gotchas, stable
  procedures) — never run-specific values, credentials, or page contents.
- Check for an existing file for the same key (domain, API, project) before
  creating a new one; merge, don't duplicate.
- This is a task-end decision, not a per-step write — keep it out of the
  SQLite step log entirely.

---

## 6. Proposed layout

Extends `LOCAL_AGENT_ARCHITECTURE.md` §10:

```
agent/
├── src/
│   ├── main.py
│   ├── core/
│   │   ├── skill_registry.py   # scans agent/skills/*, validates, builds capability index
│   │   ├── prompt_builder.py   # core rules + skill index; injects active skill bodies
│   │   ├── session.py          # SessionManager — step loop, resume via skill_activations
│   │   └── memory_router.py    # SQLite step writes vs skill memory/*.md writes
│   ├── agents/{planner.py, navigator.py}
│   ├── actions/{dispatcher.py, schemas.py}   # only active-skill tools exposed
│   ├── llm/factory.py
│   └── memory/store.py          # SQLite access layer
├── skills/                       # PLUGGABLE — drop a folder in, it's live
│   ├── browser-agent-bridge/     # wraps skills/browser-agent-bridge (repo root)
│   │   ├── SKILL.md
│   │   ├── references/protocol.md
│   │   ├── tools.py               # typed wrappers over the bridge RPC methods
│   │   └── memory/                # == runtime/site-patterns
│   ├── shell-exec/{SKILL.md, tools.py}
│   ├── http-tools/{SKILL.md, tools.py}
│   └── file-ops/{SKILL.md, tools.py}
├── skills.enabled.json            # ops-level allow/deny, mirrors .agents/skills.json
├── data/agent.db
└── tests/
```

---

## 7. Loading rules (what to actually teach the agent)

1. On boot: load the core rules + skill index only. Nothing skill-specific.
2. When a trigger matches or the model calls `skill.activate(name)`: inject
   that skill's `SKILL.md` body, register its `tools.py` functions, log a
   `skill_activations` row.
3. Deep material (`references/*.md`) loads only if the active skill's own
   body points to it and the step actually needs it — same two-hop
   disclosure `browser-agent-bridge` already does with `protocol.md`.
4. After `N` idle steps (no calls into a skill's `capabilities`),
   deactivate it: drop its prompt body and its tools from the surface, log
   the deactivation. Reactivating later is a cheap re-inject, not a re-scan.
5. At task end, the agent decides per active skill whether anything
   learned belongs in that skill's `memory/*.md` — never written
   automatically, never written per-step.

---

## 8. What this buys you

- **Long-running tasks**: SQLite gives exact resume (which step, which
  skills were live); `memory/*.md` gives the agent something better than a
  cold start on the *next* task against the same site/API, without bloating
  the step log with judgment calls.
- **Prompt discipline**: the system prompt's size is bounded by the number
  of *active* skills, not the number of *installed* ones — it doesn't grow
  as you add capabilities.
- **Pluggability**: a skill is a folder; adding, removing, or disabling one
  is a filesystem/config change, not a code change to the agent core.
- **Consistency with the existing repo**: this isn't a new pattern bolted
  on — it's the `SKILL.md` → `references/protocol.md` → `runtime/site-patterns/`
  structure already proven for the browser bridge, applied uniformly to
  every future capability the local agent gains.
