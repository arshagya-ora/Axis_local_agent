#!/usr/bin/env python3
"""Freeze and verify the browser tool contract, v1 and v2 (Phase 0 / 1.2).

Derives one deterministic JSON snapshot from
``browser_agent_tools.get_tool_definitions()`` plus the module's public
error codes and forbidden-method set — nothing here duplicates those
schemas by hand.

Two versions exist side by side:

- **v1** (``contracts/browser_tool_contract.json``) — the original
  seven-tool Phase 0 contract. Frozen: it is never regenerated from live
  code again (the code has moved on to eight tools), so ``--version v1``
  only checks the committed file's own internal self-consistency (that its
  stored hash still matches its own content) — a tamper-evidence check, not
  a drift-vs-code check. ``--update --version v1`` is refused.
- **v2** (``contracts/browser_tool_contract_v2.json``) — the active,
  whole-browser/multi-tab contract (adds ``browser_tabs``; eight tools
  total). ``--verify --version v2`` compares live
  ``get_tool_definitions()`` output against this file, exactly like v1
  worked in Phase 0/1. Phase 1.2 and later code must match v2.

Usage:
    python scripts/browser_contract.py                       # verify v2 (default)
    python scripts/browser_contract.py --verify --version v2 # verify v2, explicitly
    python scripts/browser_contract.py --verify --version v1 # v1 self-consistency check
    python scripts/browser_contract.py --update --version v2 # regenerate v2 from live code
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import browser_agent_tools as bat  # noqa: E402

BROWSER_CONTRACT_VERSION = "2.0.0"  # the active (v2) contract version
CONTRACT_PATH_V1 = AGENT_DIR / "contracts" / "browser_tool_contract.json"
CONTRACT_PATH_V2 = AGENT_DIR / "contracts" / "browser_tool_contract_v2.json"
CONTRACT_PATH = CONTRACT_PATH_V2  # back-compat name for anything importing the old symbol

# Names of the module-level error-code constants to pull into the v2
# contract. Listing names (not their string values) means a future rename
# of a code's *value* is still picked up automatically via getattr below;
# only adding or removing a code requires touching this list. Includes the
# Phase 1.2 additions (TAB_NOT_FOUND/TAB_CLOSED/FIREWALL_DENIED) on top of
# v1's original set.
_ERROR_CODE_NAMES = (
    "INVALID_ARGUMENT",
    "UNKNOWN_TOOL",
    "SESSION_NOT_FOUND",
    "TAB_NOT_FOUND",
    "TAB_CLOSED",
    "NO_MANAGED_TAB",
    "SCOPE_DENIED",
    "FIREWALL_DENIED",
    "STALE_OBSERVATION",
    "REF_NOT_FOUND",
    "BRIDGE_UNAVAILABLE",
    "BRIDGE_ERROR",
    "ASSERTION_FAILED",
    "TIMEOUT",
    "SENSITIVE_OPERATION_BLOCKED",
)

# The common response envelope every tool returns (see
# browser_agent_tools._ok / _err). This shape isn't itself an importable
# schema object in browser_agent_tools.py, so it is the one thing this file
# states explicitly rather than derives.
_RESPONSE_ENVELOPE: Dict[str, Any] = {
    "ok": "boolean",
    "browserSessionId": "string|null",
    "data": "object|null (tool-specific payload on success)",
    "error": {
        "code": "string|null (one of errorCodes)",
        "message": "string|null",
        "retryable": "boolean|null",
        "diagnostic": "object|null (tool-specific detail on failure)",
    },
}


def _error_codes() -> List[str]:
    return sorted(getattr(bat, name) for name in _ERROR_CODE_NAMES)


def build_contract() -> Dict[str, Any]:
    """Assemble the v2 contract fields from the live module — no field
    here is hand-copied from a schema; everything is read from
    ``browser_agent_tools`` at call time."""
    tools = bat.get_tool_definitions()
    return {
        "browserContractVersion": BROWSER_CONTRACT_VERSION,
        "toolCount": len(tools),
        "toolNames": [tool["function"]["name"] for tool in tools],
        "tools": tools,
        "errorCodes": _error_codes(),
        "responseEnvelope": _RESPONSE_ENVELOPE,
        "forbiddenBridgeMethods": sorted(bat.FORBIDDEN_METHODS),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_contract_hash(contract: Dict[str, Any]) -> str:
    """SHA-256 hex digest of the contract's canonical (sorted-key, no
    whitespace) JSON form. Independent of dict insertion/key order, so the
    hash only changes when the contract's actual content changes."""
    return hashlib.sha256(_canonical_json(contract).encode("utf-8")).hexdigest()


def generate_snapshot() -> Dict[str, Any]:
    """Generate the v2 snapshot from live code. There is no live-generation
    equivalent for v1 — see module docstring."""
    contract = build_contract()
    snapshot = dict(contract)
    snapshot["contractHash"] = compute_contract_hash(contract)
    return snapshot


def _pretty(snapshot: Dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _contract_path(version: str) -> Path:
    if version == "v1":
        return CONTRACT_PATH_V1
    if version == "v2":
        return CONTRACT_PATH_V2
    raise ValueError(f"Unknown contract version {version!r}; expected 'v1' or 'v2'.")


def load_committed_snapshot(version: str = "v2") -> Optional[Dict[str, Any]]:
    path = _contract_path(version)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def format_drift_report(committed: Optional[Dict[str, Any]], generated: Dict[str, Any]) -> str:
    """Empty string means no drift. Otherwise a human-readable unified diff
    plus what to do about it. Used for v2's live-vs-committed check."""
    if committed is None:
        return (
            f"No committed contract snapshot found at {CONTRACT_PATH_V2}. "
            f"Run 'python scripts/browser_contract.py --update --version v2' to create it."
        )
    if committed == generated:
        return ""
    diff = "\n".join(
        difflib.unified_diff(
            _pretty(committed).splitlines(),
            _pretty(generated).splitlines(),
            fromfile="committed v2 contract",
            tofile="current get_tool_definitions() output",
            lineterm="",
        )
    )
    return (
        "Browser tool contract (v2) drift detected between the committed snapshot at "
        f"{CONTRACT_PATH_V2} and the current implementation:\n{diff}\n\n"
        "If this change is intentional, bump BROWSER_CONTRACT_VERSION in "
        "scripts/browser_contract.py and re-run with --update --version v2. If it isn't, the "
        "eight-tool contract changed unintentionally — fix the code instead."
    )


def _verify_v1_self_consistency() -> bool:
    """v1 is a frozen historical artifact — the live code is v2-shaped now,
    so this does not (and cannot) compare v1 against get_tool_definitions().
    It only confirms the committed file has not been tampered with: its
    stored contractHash must still match a hash recomputed from its own
    content."""
    committed = load_committed_snapshot("v1")
    if committed is None:
        print(f"No committed v1 contract snapshot found at {CONTRACT_PATH_V1}.", file=sys.stderr)
        return False
    stored_hash = committed.get("contractHash")
    content_without_hash = {k: v for k, v in committed.items() if k != "contractHash"}
    recomputed_hash = compute_contract_hash(content_without_hash)
    if stored_hash != recomputed_hash:
        print(
            f"v1 contract self-consistency check FAILED at {CONTRACT_PATH_V1}: "
            f"stored hash {stored_hash!r} does not match recomputed hash {recomputed_hash!r}. "
            "The frozen v1 file appears to have been edited.", file=sys.stderr,
        )
        return False
    print(f"v1 contract self-consistency verified OK at {CONTRACT_PATH_V1} (hash {stored_hash[:12]}...). "
          "v1 is frozen and not compared against current code — see v2 for the active contract.")
    return True


def verify(version: str = "v2") -> bool:
    if version == "v1":
        return _verify_v1_self_consistency()
    generated = generate_snapshot()
    committed = load_committed_snapshot("v2")
    report = format_drift_report(committed, generated)
    if report:
        print(report, file=sys.stderr)
        return False
    print(
        f"Browser tool contract (v2) verified OK against {CONTRACT_PATH_V2} "
        f"(version {generated['browserContractVersion']}, hash {generated['contractHash'][:12]}...)."
    )
    return True


def update(version: str = "v2") -> int:
    if version == "v1":
        print(
            "Refusing to regenerate v1: it is a frozen historical artifact "
            "(the live contract is v2-shaped now). Use --version v2.", file=sys.stderr,
        )
        return 1
    CONTRACT_PATH_V2.parent.mkdir(parents=True, exist_ok=True)
    snapshot = generate_snapshot()
    CONTRACT_PATH_V2.write_text(_pretty(snapshot), encoding="utf-8")
    print(f"Wrote {CONTRACT_PATH_V2} (hash {snapshot['contractHash'][:12]}...).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze/verify the browser tool contract snapshot(s).")
    parser.add_argument("--update", action="store_true", help="Regenerate and overwrite the committed v2 snapshot. Refused for v1.")
    parser.add_argument("--verify", action="store_true", help="Verify the committed snapshot (this is the default).")
    parser.add_argument("--version", choices=("v1", "v2"), default="v2", help="Which contract to operate on (default v2, the active one).")
    args = parser.parse_args(argv)

    if args.update:
        return update(args.version)
    return 0 if verify(args.version) else 1


if __name__ == "__main__":
    raise SystemExit(main())
