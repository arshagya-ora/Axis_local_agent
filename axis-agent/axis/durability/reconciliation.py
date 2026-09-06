"""Deterministic reconciliation classification for an `unknown_after_crash`
effect.

Pure, tiny, and unit-testable in isolation: the actual fresh
`browser_observe`/`browser_assert` calls happen through the *same* durable
agent/tool machinery any other browser call uses (driven by a reconciliation
prompt in `AxisJobWorkflow`). The shared durable result guard in
`axis.durability.runtime` records which required criteria a matching
assertion actually passed or explicitly failed onto the
`DurableEffectRecord` — this module only classifies the outcome from those
two bounded id sets. No raw browser arguments ever reach this function.
"""
from __future__ import annotations

from typing import Literal, Set

ReconciliationOutcome = Literal["applied", "not_applied", "inconclusive"]


def classify_reconciliation(
    required_ids: Set[str], passed_ids: Set[str], failed_ids: Set[str],
    *, dispatch_proven_not_started: bool = False,
) -> ReconciliationOutcome:
    """`applied` only when every required criterion is satisfied by a
    matching, passing, same-tab assertion (already enforced by
    `criterion_matches_assertion` before an id ever lands in `passed_ids`).
    `not_applied` only when a trusted runtime execution record proves
    dispatch never began. Failed, absent, or mixed postconditions are all
    `inconclusive`, which never triggers an automatic retry."""
    if required_ids.issubset(passed_ids):
        return "applied" if required_ids else "inconclusive"
    if dispatch_proven_not_started:
        return "not_applied"
    return "inconclusive"
