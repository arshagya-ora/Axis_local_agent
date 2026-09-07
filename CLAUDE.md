# AXIS Project Instructions

## Project goal

AXIS is a production-grade browser automation agent built with Pydantic AI. It operates one approved Agent-managed Chrome session through a secure semantic browser-tool layer.

Build simple, effective, powerful code. Optimize for working software, reliability, maintainability, and developer time.

## Primary engineering rule

Implement the smallest production-worthy solution that satisfies the current task.

* Reuse existing code before creating new abstractions.
* Prefer a few cohesive modules over deep package hierarchies.
* Do not introduce factories, registries, repositories, services, protocols, or dependency-injection layers without an immediate requirement.
* Do not implement speculative future functionality.
* Do not refactor unrelated working code.
* Avoid duplicate implementations and unnecessary compatibility wrappers.
* Prefer readable code over clever code.
* Ask a question only when the answer would materially change the implementation.
* Never think IN terms of work arounds. always think in terms of fundamental fixes and real root cause finding

## Phase discipline

* Implement only the phase or task explicitly requested.
* Do not automatically start the next phase.
* Treat the current user prompt as the authoritative scope.
* Read existing code, tests, contract snapshots, manifests, and lockfiles before changing anything.
* Preserve all contracts established by completed phases.
* Do not add later-phase infrastructure early merely because it appears in an architecture document.

## Browser contract

The model-facing browser API contains exactly seven semantic tools:

1. `browser_observe`
2. `browser_act`
3. `browser_navigate`
4. `browser_wait`
5. `browser_assert`
6. `browser_capture_evidence`
7. `browser_diagnose`

These names and their frozen public schemas are compatibility contracts.

* Never expose raw Browser Agent Bridge methods to the model.
* Never expose model-selectable tab, window, group, bridge-session, snapshot, or frame identifiers.
* Never add arbitrary JavaScript or coordinate-based computer control.
* Keep `browser_bridge_client.py` thin.
* Keep AXIS behavior, validation, and policy in the AXIS layer.
* Do not update the frozen browser-contract snapshot unless explicitly requested.
* Any intentional contract change requires updated tests and explicit approval.

## Browser safety invariants

* Operate only on the server-bound Agent-managed browser session.
* Never trust a browser-session ID supplied by model output.
* Authorize every browser operation before bridge execution.
* Observe before element-targeted actions.
* Use only refs from the latest valid compact observation.
* Re-observe after navigation or major page replacement.
* Never blindly retry a stale ref.
* Perform one semantic interaction per `browser_act`.
* A successful action or wait does not prove task completion.
* Verify the requested outcome through observation or `browser_assert`.
* Keep external browser mutations serialized per session.
* Preserve existing redaction, output bounds, allowlists, and forbidden-method checks.

These guarantees must be enforced by code and tests, not only by prompts.

## Pydantic AI agent

* Use one authoritative root `Agent`.
* Use typed dependencies for trusted runtime state.
* Use typed result models for agent outcomes.
* Keep browser functionality behind the existing thin capability adapter.
* Use the exact dependency versions pinned by the project.
* Use only provider features confirmed by the Phase 0 compatibility probe.
* Use local or model-agnostic behavior for unsupported or inconclusive provider features.
* Do not introduce Agent Specs until multiple real deployment profiles require them.
* Do not introduce multiple agents unless explicitly required by a later phase.

## Runtime limits

* Runtime limits come from trusted application configuration or a typed manifest.
* The model cannot modify request, tool-call, token, or wall-time limits.
* Stop safely when a limit is reached.
* Convert expected runtime failures into typed results.
* Never continue browser effects after cancellation, timeout, or exhausted limits.

## Durable execution

When working on durable workflows:

* Workflow code must remain deterministic.
* Put model calls, browser calls, network calls, filesystem access, and other side effects in activities.
* Persist intended effects before executing external mutations.
* Treat an interrupted mutation as having an unknown outcome.
* Reconcile unknown outcomes through fresh observation and assertions before retrying.
* Do not assume Temporal provides exactly-once external side effects.
* Use bounded retry policies.
* Keep one writer lease per mutable browser session.
* Implement only the minimum workflow, signals, and recovery behavior required by the current phase.

## UI and API boundaries

* Durable job state remains authoritative on the server.
* Treat client-provided identity, policy, workflow state, authorization, and browser identifiers as untrusted.
* UI reconnects must replay persisted events without repeating effects.
* Pause, resume, cancellation, approval, and steering commands must be authenticated and validated.
* The frontend presents state; it does not define security decisions.

## Python conventions

* Follow the project’s existing Python version, formatter, type-checker, and dependency manager.
* Use type hints on public functions and important internal boundaries.
* Use Pydantic models at external, model, persistence, and configuration boundaries.
* Do not create Pydantic models for trivial internal values when a normal function or dataclass is clearer.
* Use asynchronous code only for genuinely asynchronous operations.
* Keep functions focused and names explicit.
* Avoid broad exception handling unless converting errors at a defined boundary.
* Never silently swallow failures.
* Never log credentials, tokens, cookies, full provider responses, sensitive page content, or raw browser identifiers.

## Dependencies

* Use the existing dependency-management system.
* Do not introduce a second package manager.
* Do not upgrade pinned dependencies unless the task requires it.
* Before upgrading Pydantic AI, its provider SDK, OCI authentication, or related packages:

  1. Run the frozen browser-contract tests.
  2. Run the complete automated suite.
  3. Run and compare the provider compatibility probe.
* Do not add a dependency when the standard library or an existing dependency solves the problem clearly.

## Testing

Before considering work complete:

* Run targeted tests for the changed behavior.
* Run the complete existing automated suite.
* Preserve Phase 0 contract verification.
* Keep tests deterministic and independent of live OCI, Chrome, and Browser Agent Bridge unless explicitly marked as live tests.
* Mock external systems in unit and component tests.
* Test failure paths, not only success paths.
* Never weaken, delete, skip, or rewrite a valid test merely to make implementation pass.
* Run live canaries only when the required non-production environment is configured.
* Never fabricate a test or canary result.
* Never think IN terms of work arounds. always think in terms of fundamental fixes and real root cause finding

For every change, report the exact commands executed and their actual results.

## Repository hygiene

* Inspect current repository status before editing.
* Preserve unrelated user changes.
* Make focused changes only.
* Do not create duplicate implementations or abandoned temporary modules.
* Do not commit generated artifacts, credentials, local paths, caches, logs, or unredacted probe output.
* Do not create or modify documentation unless the task explicitly requests documentation.
* Do not commit, push, create branches, or open pull requests unless explicitly requested.
* Do not use destructive Git or filesystem commands.

## Definition of done

A task is complete only when:

* The requested behavior is implemented.
* Never think IN terms of work arounds. always think in terms of fundamental fixes and real root cause finding
* No secrets or sensitive identifiers were introduced.
* No unnecessary architecture or dependencies were added.
* The final response states:

  * files changed;
  * tests and results;
  * live checks performed or skipped;
  * assumptions and blockers;
  * confirmation that later-phase functionality was not introduced.
