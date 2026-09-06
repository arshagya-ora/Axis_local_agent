#!/usr/bin/env python3
"""Freeze and verify the seven-tool browser contract (Phase 0).

Derives one deterministic JSON snapshot from
``browser_agent_tools.get_tool_definitions()`` plus the module's public
error codes and forbidden-method set — nothing here duplicates those
schemas by hand. The snapshot is committed at
``contracts/browser_tool_contract.json`` and is the thing later phases
diff against to detect an accidental change to the model-facing contract.

Usage:
    python scripts/browser_contract.py            # verify (default)
    python scripts/browser_contract.py --verify   # verify, explicitly
    python scripts/browser_contract.py --update   # regenerate and overwrite
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

BROWSER_CONTRACT_VERSION = "1.0.0"
CONTRACT_PATH = AGENT_DIR / "contracts" / "browser_tool_contract.json"

# Names of the module-level error-code constants to pull into the contract.
# Listing names (not their string values) means a future rename of a code's
# *value* is still picked up automatically via getattr below; only adding or
# removing a code requires touching this list.
_ERROR_CODE_NAMES = (
    "INVALID_ARGUMENT",
    "UNKNOWN_TOOL",
    "SESSION_NOT_FOUND",
    "NO_MANAGED_TAB",
    "SCOPE_DENIED",
    "STALE_OBSERVATION",
    "REF_NOT_FOUND",
    "BRIDGE_UNAVAILABLE",
    "BRIDGE_ERROR",
    "ASSERTION_FAILED",
    "TIMEOUT",
    "SENSITIVE_OPERATION_BLOCKED",
)

# The common response envelope every one of the seven tools returns (see
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
    """Assemble the contract fields from the live module — no field here is
    hand-copied from a schema; everything is read from
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
    contract = build_contract()
    snapshot = dict(contract)
    snapshot["contractHash"] = compute_contract_hash(contract)
    return snapshot


def _pretty(snapshot: Dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def load_committed_snapshot() -> Optional[Dict[str, Any]]:
    if not CONTRACT_PATH.exists():
        return None
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def format_drift_report(committed: Optional[Dict[str, Any]], generated: Dict[str, Any]) -> str:
    """Empty string means no drift. Otherwise a human-readable unified diff
    plus what to do about it."""
    if committed is None:
        return (
            f"No committed contract snapshot found at {CONTRACT_PATH}. "
            f"Run 'python scripts/browser_contract.py --update' to create it."
        )
    if committed == generated:
        return ""
    diff = "\n".join(
        difflib.unified_diff(
            _pretty(committed).splitlines(),
            _pretty(generated).splitlines(),
            fromfile="committed contract",
            tofile="current get_tool_definitions() output",
            lineterm="",
        )
    )
    return (
        "Browser tool contract drift detected between the committed snapshot at "
        f"{CONTRACT_PATH} and the current implementation:\n{diff}\n\n"
        "If this change is intentional, bump BROWSER_CONTRACT_VERSION in "
        "scripts/browser_contract.py and re-run with --update. If it isn't, the "
        "seven-tool contract changed unintentionally — fix the code instead."
    )


def verify() -> bool:
    generated = generate_snapshot()
    committed = load_committed_snapshot()
    report = format_drift_report(committed, generated)
    if report:
        print(report, file=sys.stderr)
        return False
    print(
        f"Browser tool contract verified OK against {CONTRACT_PATH} "
        f"(version {generated['browserContractVersion']}, hash {generated['contractHash'][:12]}...)."
    )
    return True


def update() -> None:
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = generate_snapshot()
    CONTRACT_PATH.write_text(_pretty(snapshot), encoding="utf-8")
    print(f"Wrote {CONTRACT_PATH} (hash {snapshot['contractHash'][:12]}...).")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze/verify the seven-tool browser contract snapshot.")
    parser.add_argument("--update", action="store_true", help="Regenerate and overwrite the committed snapshot.")
    parser.add_argument("--verify", action="store_true", help="Verify the committed snapshot (this is the default).")
    args = parser.parse_args(argv)

    if args.update:
        update()
        return 0
    return 0 if verify() else 1


if __name__ == "__main__":
    raise SystemExit(main())
