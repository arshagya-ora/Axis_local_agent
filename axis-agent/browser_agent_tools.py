"""Axis Agent browser tool layer.

Wraps the Browser Agent Bridge's ~150-method JSON-RPC surface (see
``browser-agent-bridge-main/extension/service-worker.js``, the authoritative
method registry) behind seven bounded, LLM-facing semantic tools:

    browser_observe          (browser.observe)
    browser_act               (browser.act)
    browser_navigate           (browser.navigate)
    browser_wait                (browser.wait)
    browser_assert                (browser.assert)
    browser_capture_evidence         (browser.capture_evidence)
    browser_diagnose                    (browser.diagnose)

The LLM never selects a raw bridge method, a Chrome tabId/windowId/groupId, a
snapshotId, or a frameId. It supplies a ``browserSessionId`` (minted by
:meth:`BrowserAgentTools.bind_active_managed_tab`) and, for element-targeted
operations, a ``ref`` (minted by :meth:`BrowserAgentTools.browser_observe`) or
a bounded semantic ``locator``. Everything else is resolved internally.

This module is intentionally a single cohesive file (see the repository's
implementation brief) rather than a package of one-class-per-concept modules.
It is organized into the sections below; search for the ``# ===`` banners.

    # Imports and constants
    # Common results, errors and validation
    # LLM-facing JSON tool definitions
    # Scope-provider interface
    # Browser session and observation state helpers
    # Bridge-method mappings
    # BrowserAgentTools implementation
    # Tool registry and dispatcher
    # Agent instructions

Security note: the Browser Agent Bridge itself remains the final enforcement
boundary — it already refuses RPCs that target a tab outside an Agent-managed
tab group (see ``extension/sw/tab-scope.js`` and ``sessions.js``). This module
adds an Axis Agent-side layer on top of that boundary: it never lets the LLM
choose a raw method or tab, and it runs every call through a replaceable
:class:`ScopeProvider` so a future external firewall can be substituted
without changing the seven public tools.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError

# =====================================================================
# Imports and constants
# =====================================================================

DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_NODES = 1000
DEFAULT_DIAGNOSTIC_LIMIT = 100
MAX_DIAGNOSTIC_LIMIT = 500
MAX_TEXT_CHARS = 20000            # bound on snapshot/text/html content returned to the LLM
MAX_DIAGNOSTIC_BODY_CHARS = 4000  # bound on a single response body returned to the LLM
MAX_LIST_ITEMS = 200              # bound on list length anywhere in a returned payload
MAX_REDACT_DEPTH = 8

REDACTED = "<redacted>"

# Error codes. See the module-level docstring / README for the response
# contract these are used in. ASSERTION_FAILED is reserved for callers that
# want to represent a definitive, non-retryable assertion failure through the
# error channel; browser_assert itself reports assertion outcomes through
# ok:true / data.passed so agents can distinguish "the condition was
# evaluated and is false" from "the tool call itself failed".
INVALID_ARGUMENT = "INVALID_ARGUMENT"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
NO_MANAGED_TAB = "NO_MANAGED_TAB"
SCOPE_DENIED = "SCOPE_DENIED"
STALE_OBSERVATION = "STALE_OBSERVATION"
REF_NOT_FOUND = "REF_NOT_FOUND"
BRIDGE_UNAVAILABLE = "BRIDGE_UNAVAILABLE"
BRIDGE_ERROR = "BRIDGE_ERROR"
ASSERTION_FAILED = "ASSERTION_FAILED"
TIMEOUT = "TIMEOUT"
SENSITIVE_OPERATION_BLOCKED = "SENSITIVE_OPERATION_BLOCKED"

# Recursive redaction keys away wherever they appear (headers, cookies,
# JSON bodies, etc.) in any value returned to the LLM. Case-insensitive.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(authorization|proxy-authorization|cookie|set-cookie|"
    r"(?:access|refresh|bearer|auth|session|csrf|xsrf)[-_]?token|"
    r"password|passwd|secret|api[-_]?key|apikey|client[-_]?secret|"
    r"private[-_]?key)",
    re.IGNORECASE,
)

# Methods the seven semantic tools must NEVER be able to route to, even if a
# future mapping bug asked for one of them. Checked defensively before every
# RPC in addition to the per-tool bridge-method mapping tables below.
FORBIDDEN_METHODS = frozenset({
    "cookies.get",
    "page.executeJavaScript",
    "page.addInitScript",
    "page.removeInitScript",
    "page.waitForFunction",
    "page.setExtraHTTPHeaders",
    "page.setUserAgent",
    "extension.reload",
    "extension.getCspBypass",
    "policy.set",
    "policy.get",
    "policy.checkUrl",
    "network.setBlockedUrls",
    "network.setInterceptors",
    "network.routeFromHAR",
    "network.interceptors.clear",
    "network.interceptors.clearEvents",
    "session.stop",
    "session.start",
    "tabs.close",
    "tabs.create",
})

# The only bridge methods this module ever calls. Cross-checked against the
# per-tool mapping tables in a test to catch drift between the two.
_ALLOWED_METHODS = frozenset({
    "tabs.list", "session.list", "session.get",
    "page.accessibilityTree", "page.ariaSnapshot", "page.readText",
    "locator.clickRef", "locator.fillRef", "locator.pressRef", "locator.hoverRef",
    "locator.selectOptionRef", "locator.click", "locator.fill", "locator.press",
    "locator.selectOption", "locator.check", "locator.uncheck",
    "locator.setInputFiles", "locator.dragTo", "dom.hover", "dom.scroll",
    "page.navigate", "page.reload", "page.goBack", "page.goForward",
    "locator.waitFor", "page.waitForURL", "page.waitForNavigation",
    "page.waitForLoad", "page.waitForNetworkIdle", "page.waitForText",
    "page.waitForPopup", "page.waitForDialog", "page.waitForRequest",
    "page.waitForResponse",
    "expect.locator.toBeVisible", "expect.locator.toBeHidden",
    "expect.locator.toBeEnabled", "expect.locator.toBeDisabled",
    "expect.locator.toBeEditable", "expect.locator.toBeChecked",
    "expect.locator.toHaveValue", "expect.locator.toHaveText",
    "expect.locator.toHaveCount", "expect.locator.toHaveAttribute",
    "expect.page.toHaveTitle", "expect.page.toMatchAriaSnapshot",
    "page.screenshot", "locator.screenshot", "page.domSnapshot", "page.pdf",
    "trace.start", "trace.stop", "trace.exportHtml",
    "console.read", "network.read", "network.getResponseBody", "trace.status",
})

# Native-messaging-host operations this module is allowed to invoke. These
# bypass BrowserBridgeClient.rpc() (they go through the client's dedicated
# save_data_url() helper instead), so _assert_method_allowed() never sees
# them — _save_data_url() checks this allowlist explicitly for that reason.
_ALLOWED_NATIVE_METHODS = frozenset({"native.saveDataUrl"})


class BridgeMethodNotAllowedError(RuntimeError):
    """Raised internally when a bridge method fails the forbidden-method or
    positive-allowlist guard. This should never surface in normal operation
    (the per-tool mapping tables only ever produce an allowed method) — it
    is a defense-in-depth check, not the primary access-control mechanism."""


class _ToolArgError(Exception):
    """Internal control-flow exception carrying a pre-built error envelope.
    Raised by the ``_build_*_call`` helpers when an action/condition/
    assertion/capture/diagnostic's specific argument requirements are not
    met, and caught by the corresponding public tool method."""

    def __init__(self, response: Dict[str, Any]):
        message = ((response.get("error") or {}).get("message")) or "invalid arguments"
        super().__init__(message)
        self.response = response


# =====================================================================
# Common results, errors and validation
# =====================================================================

def _redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive keys and bound the size of any value
    before it is returned to the LLM. Applied to every tool's ``data`` and
    ``error.diagnostic`` payload by :func:`_ok` / :func:`_err` so redaction
    and size-bounding cannot be forgotten by an individual tool."""
    if depth > MAX_REDACT_DEPTH:
        return "<max depth exceeded>"
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, sub in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_PATTERN.search(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_value(sub, depth + 1)
        return redacted
    if isinstance(value, list):
        truncated = len(value) > MAX_LIST_ITEMS
        items = [_redact_value(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if truncated:
            items.append(f"... [{len(value) - MAX_LIST_ITEMS} more items truncated]")
        return items
    if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
        return value[:MAX_TEXT_CHARS] + f"... [truncated {len(value) - MAX_TEXT_CHARS} characters]"
    return value


def _ok(browser_session_id: Optional[str], data: Any) -> Dict[str, Any]:
    return {
        "ok": True,
        "browserSessionId": browser_session_id,
        "data": _redact_value(data),
        "error": None,
    }


def _err(
    browser_session_id: Optional[str],
    code: str,
    message: str,
    retryable: bool,
    diagnostic: Any = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "browserSessionId": browser_session_id,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "diagnostic": _redact_value(diagnostic) if diagnostic is not None else None,
        },
    }


def _safe_session_id(args: Any) -> Optional[str]:
    if isinstance(args, dict):
        value = args.get("browserSessionId")
        if isinstance(value, str):
            return value
    return None


_TRANSPORT_ERROR_HINTS = (
    "connection refused", "econnrefused", "name or service not known",
    "getaddrinfo failed", "urlopen error", "[errno", "unreachable",
    "connection reset", "no route to host",
)

# BrowserBridgeClient._request() formats a non-2xx HTTP response as
# "HTTP {code}: {payload}" (see browser_bridge_client.py). Matching that
# prefix lets us tell "the bridge process answered but rejected the
# request" (an HTTP status) apart from "we never got a response at all"
# (a socket-level failure, matched by _TRANSPORT_ERROR_HINTS above).
_HTTP_STATUS_PATTERN = re.compile(r"^http (\d{3})\b", re.IGNORECASE)


def _extract_http_status(message: str) -> Optional[int]:
    match = _HTTP_STATUS_PATTERN.match(message.strip())
    return int(match.group(1)) if match else None


def _looks_like_transport_error(message: str) -> bool:
    lower = message.lower()
    return any(hint in lower for hint in _TRANSPORT_ERROR_HINTS)


def _looks_like_timeout(message: str) -> bool:
    lower = message.lower()
    return "timed out" in lower or "timeout" in lower


# The content script (extension/content/accessibility-tree.js) throws these
# exact messages when a ref-based locator.*Ref call arrives with a snapshotId
# that no longer matches the page's current accessibility snapshot, or a ref
# that no longer resolves to a live element. Both mean exactly one thing: the
# stored ref is no longer usable and browser_observe must be called again —
# never retry the old ref.
_STALE_REF_HINTS = (
    "stale accessibility ref snapshot",
    "accessibility ref not found or stale",
)


def _looks_like_stale_ref_error(message: str) -> bool:
    lower = message.lower()
    return any(hint in lower for hint in _STALE_REF_HINTS)


def _bridge_error_response(browser_session_id: Optional[str], error: BrowserBridgeError) -> Dict[str, Any]:
    """Classify a raised BrowserBridgeError into the common error contract.
    Used by every tool except browser_assert, which instead treats a bridge
    error raised by an ``expect.*``/``locator.waitFor`` call as an assertion
    that did not hold (see :meth:`BrowserAgentTools.browser_assert` and
    :func:`_is_assertion_mismatch_error`)."""
    message = str(error)

    if _looks_like_stale_ref_error(message):
        return _err(
            browser_session_id, STALE_OBSERVATION,
            f"The bridge rejected this ref as stale: {message}. Call browser_observe again "
            f"and act on a ref from the new observation.", True, error.data,
        )

    http_status = _extract_http_status(message)
    if http_status in (401, 403):
        # The bridge was reached and answered — it rejected our credentials,
        # which is a configuration problem, not "the bridge is unreachable".
        return _err(
            browser_session_id, BRIDGE_ERROR,
            f"The Browser Agent Bridge rejected the request (HTTP {http_status}); "
            f"check BROWSER_AGENT_BRIDGE_TOKEN. {message}", False, error.data,
        )
    if http_status is not None and http_status >= 500:
        return _err(browser_session_id, BRIDGE_UNAVAILABLE,
                    f"The Browser Agent Bridge reported a server error: {message}", True, error.data)
    if http_status is not None:
        return _err(browser_session_id, BRIDGE_ERROR, message, False, error.data)

    if _looks_like_transport_error(message):
        return _err(browser_session_id, BRIDGE_UNAVAILABLE,
                    f"Could not reach the Browser Agent Bridge: {message}", True, error.data)
    if _looks_like_timeout(message):
        return _err(browser_session_id, TIMEOUT, message, True, error.data)
    return _err(browser_session_id, BRIDGE_ERROR, message, False, error.data)


# Bridge codes (see error.code assignments in extension/sw/locator.js and
# extension/sw/page.js) that specifically mean "the expect.* condition was
# evaluated and did not hold within the timeout" — as opposed to e.g.
# LOCATOR_STRICT_MODE_VIOLATION, LOCATOR_REF_NOT_ACTIONABLE, a
# permission/tab-scope rejection, or an invalid-argument error, none of
# which represent an assertion outcome.
_ASSERTION_MISMATCH_CODES = frozenset({
    "LOCATOR_EXPECT_TIMEOUT",
    "PAGE_EXPECT_TITLE_TIMEOUT",
    "PAGE_EXPECT_ARIA_SNAPSHOT_TIMEOUT",
})

# expect.locator.toBeVisible/toBeHidden go through locator.waitFor, which
# (unlike the other expect.* handlers) throws a plain Error with no `.code`
# on timeout. Recognize its message shape as the one uncoded case that is
# still a genuine assertion mismatch rather than an infrastructure error.
_LOCATOR_WAITFOR_TIMEOUT_PATTERN = re.compile(
    r"timed out waiting for locator .* to be (attached|visible|hidden|detached)", re.IGNORECASE,
)


def _is_assertion_mismatch_error(error: BrowserBridgeError) -> bool:
    """True only for a bridge failure that represents "the condition was
    evaluated and is false/not-yet-true", which browser_assert reports as
    ok:true/data.passed:false. Anything else (TAB_NOT_ALLOWED-style scope
    rejections, invalid parameters, debugger/actionability failures,
    transport errors, ...) must fall through to the normal ok:false error
    contract instead."""
    data = error.data if isinstance(error.data, dict) else None
    code = data.get("code") if data else None
    if code:
        return code in _ASSERTION_MISMATCH_CODES
    if _looks_like_transport_error(str(error)) or _extract_http_status(str(error)) is not None:
        return False
    return bool(_LOCATOR_WAITFOR_TIMEOUT_PATTERN.search(str(error)))


# --- minimal JSON-Schema validation helper -----------------------------
# Not a general JSON-Schema implementation: it covers exactly the subset the
# TOOL_DEFINITIONS schemas below use (object/string/integer/number/boolean/
# array-of-string, enum, minimum/maximum, default, additionalProperties). It
# is deliberately reused by every tool instead of hand-rolled per-tool
# validation so the schemas stay the single source of truth.

def _validate_value(key: str, value: Any, subschema: Dict[str, Any]) -> Optional[str]:
    expected_type = subschema.get("type")
    if expected_type == "string":
        if not isinstance(value, str):
            return f"'{key}' must be a string"
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return f"'{key}' must be a boolean"
    elif expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"'{key}' must be an integer"
        if "minimum" in subschema and value < subschema["minimum"]:
            return f"'{key}' must be >= {subschema['minimum']}"
        if "maximum" in subschema and value > subschema["maximum"]:
            return f"'{key}' must be <= {subschema['maximum']}"
    elif expected_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"'{key}' must be a number"
    elif expected_type == "array":
        if not isinstance(value, list):
            return f"'{key}' must be an array"
        items_schema = subschema.get("items") or {}
        item_type = items_schema.get("type")
        if item_type == "string":
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    return f"'{key}[{index}]' must be a string"
    elif expected_type == "object":
        if not isinstance(value, dict):
            return f"'{key}' must be an object"
        nested_props = subschema.get("properties")
        if nested_props is not None and subschema.get("additionalProperties") is False:
            unknown = [k for k in value if k not in nested_props]
            if unknown:
                return f"'{key}' has unknown field(s): {', '.join(sorted(unknown))}"

    enum = subschema.get("enum")
    if enum is not None and value not in enum:
        return f"'{key}' must be one of {enum}"
    return None


def _validate_args(schema: Dict[str, Any], args: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    if not isinstance(args, dict):
        return {}, "arguments must be a JSON object"
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_ok = schema.get("additionalProperties", True)
    if not additional_ok:
        unknown = [k for k in args if k not in properties]
        if unknown:
            return {}, f"unknown argument(s): {', '.join(sorted(unknown))}"
    cleaned: Dict[str, Any] = {}
    for key, subschema in properties.items():
        if key in args:
            value = args[key]
        elif "default" in subschema:
            value = subschema["default"]
        elif key in required:
            return {}, f"missing required argument: '{key}'"
        else:
            continue
        error = _validate_value(key, value, subschema)
        if error:
            return {}, error
        cleaned[key] = value
    return cleaned, None


# --- locator handling ----------------------------------------------------

LOCATOR_IDENTIFYING_KEYS = ("selector", "text", "role", "name", "label", "placeholder")
LOCATOR_STRING_KEYS = LOCATOR_IDENTIFYING_KEYS


def _clean_locator(raw_locator: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Strip a raw locator argument down to the bounded set of fields the
    bridge understands, dropping anything else. Returns None if the result
    has no identifying field (a locator consisting only of 'exact' is not
    usable)."""
    cleaned: Dict[str, Any] = {}
    for field in LOCATOR_STRING_KEYS:
        value = raw_locator.get(field)
        if isinstance(value, str) and value:
            cleaned[field] = value
    if isinstance(raw_locator.get("exact"), bool):
        cleaned["exact"] = raw_locator["exact"]
    if not any(field in cleaned for field in LOCATOR_IDENTIFYING_KEYS):
        return None
    return cleaned


def _resolve_locator_arg(
    session_id: Optional[str], raw_locator: Any, field_name: str = "locator"
) -> Optional[Dict[str, Any]]:
    """Validate+clean an optional locator argument. Returns None when absent.
    Raises _ToolArgError when present but malformed/empty."""
    if raw_locator is None:
        return None
    if not isinstance(raw_locator, dict):
        raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"'{field_name}' must be an object.", False))
    cleaned = _clean_locator(raw_locator)
    if cleaned is None:
        raise _ToolArgError(_err(
            session_id, INVALID_ARGUMENT,
            f"'{field_name}' must include at least one of: "
            f"{', '.join(LOCATOR_IDENTIFYING_KEYS)}.", False,
        ))
    return cleaned


def _no_ref_method_message(action: str) -> str:
    """The specific reason 'ref' is rejected for an ACT_NO_REF_ACTIONS
    action — distinct per action because the underlying bridge limitation
    differs (no ref method exists at all for upload/drag; a ref method
    exists but is nondeterministic for check/uncheck)."""
    if action in ("check", "uncheck"):
        return (
            f"action '{action}' has no deterministic ref-based method — the bridge's only "
            f"ref-based path is locator.clickRef, which unconditionally toggles the current "
            f"state rather than setting it. Supply 'locator' instead of 'ref'."
        )
    if action == "drag":
        return (
            "action 'drag' has no ref-based bridge method (locator.dragToRef does not "
            "exist); supply 'locator' and 'targetLocator' instead of 'ref'."
        )
    return f"action '{action}' has no ref-based bridge method (locator.setInputFilesRef does not exist); supply 'locator' instead of 'ref'."


# =====================================================================
# LLM-facing JSON tool definitions
# =====================================================================

LOCATOR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "A semantic locator for one element, used when no ref applies. Supply at least "
        "one of 'selector' (CSS) or 'text'/'role'/'name'/'label'/'placeholder'. Use only "
        "values already seen (e.g. in browser_observe) or given by the user."
    ),
    "properties": {
        "selector": {"type": "string", "description": "A CSS selector for the element."},
        "text": {"type": "string", "description": "Visible text to match (e.g. a link/button label)."},
        "role": {"type": "string", "description": "ARIA role to match (e.g. 'button', 'textbox'); combine with 'name'."},
        "name": {"type": "string", "description": "Accessible name to match, typically with 'role'."},
        "label": {"type": "string", "description": "Associated <label> text, for form fields."},
        "placeholder": {"type": "string", "description": "Placeholder text, for text inputs."},
        "exact": {"type": "boolean", "default": False, "description": "Exact match instead of case-insensitive/substring. Default false."},
    },
    "additionalProperties": False,
}


def _locator_schema(extra_description: str) -> Dict[str, Any]:
    schema = dict(LOCATOR_SCHEMA)
    schema["description"] = LOCATOR_SCHEMA["description"] + " " + extra_description
    return schema


BROWSER_OBSERVE_DESCRIPTION = (
    "Purpose: Read the bound tab's current state without changing it, and mint short-lived "
    "'ref' tokens for a following browser_act call — the only tool that mints refs.\n\n"
    "When to use: Starting work on a page; discovering controls; right before a ref-based "
    "browser_act; after navigation or a big page change; after a stale/missing-ref error.\n\n"
    "When not to use: Modifying the page (browser_act); changing location (browser_navigate); "
    "waiting (browser_wait); proving an outcome (browser_assert); screenshots/snapshots "
    "(browser_capture_evidence).\n\n"
    "Important capabilities: 'compact' (default) returns a ref-tagged element list; 'aria' "
    "and 'text' describe the page but mint no refs.\n\n"
    "Required sequencing: Observe before any ref-based act; use only the latest "
    "observationId/refs for this session; observe again after navigation or whenever a "
    "result reports the observation invalidated.\n\n"
    "Important limitations: truncated=true on a large page means retry with a bigger "
    "maxNodes rather than guessing unseen content. 'aria'/'text' never produce refs. Only "
    "the bound tab is visible.\n\n"
    "Expected result: observationId, snapshotId (traceability only), url, title, a snapshot "
    "string with inline '[ref_N] role \"name\"' tokens, and truncated.\n\n"
    "Recommended next step: browser_act with a ref from this snapshot, or browser_assert if "
    "the needed fact is already visible here."
)

BROWSER_OBSERVE_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Read the page in an already-bound session. Does not modify the page or prove completion.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "mode": {
            "type": "string",
            "enum": ["compact", "aria", "text"],
            "default": "compact",
            "description": "compact for actionable refs; aria for accessibility structure; text for plain readable text.",
        },
        "maxNodes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2000,
            "default": 1000,
            "description": "Max accessibility nodes in compact mode; raise only if a result reports truncated.",
        },
    },
    "required": ["browserSessionId"],
    "additionalProperties": False,
}


BROWSER_ACT_DESCRIPTION = (
    "Purpose: Perform exactly one interaction — click, fill, press, hover, select, check, "
    "uncheck, upload, drag, or scroll — in the bound tab. One call is one interaction, "
    "never a batch.\n\n"
    "When to use: One concrete interaction is needed now.\n\n"
    "When not to use: Understanding an unknown page (browser_observe); going to a URL "
    "(browser_navigate); waiting for a resulting transition (browser_wait, afterward); "
    "confirming success (browser_assert — a successful act does not itself prove it); "
    "collecting evidence (browser_capture_evidence).\n\n"
    "Important capabilities: Prefers a 'ref' from the latest browser_observe; falls back "
    "to a bounded 'locator'. Returns 'whatChanged' (URL/focus/popup deltas) when the "
    "bridge reports one.\n\n"
    "Required sequencing: Observe first for ref-based actions; use only current refs. "
    "After acting, check 'observationInvalidated' — if true, observe again before the "
    "next ref-based action.\n\n"
    "Important limitations: Never runs arbitrary JavaScript or a raw method. 'upload' and "
    "'drag' have no ref-based method and need 'locator' (drag also needs 'targetLocator'). "
    "'check'/'uncheck' are locator-only — the bridge has no deterministic ref-based "
    "check/uncheck (only a toggling click). 'hover' without a ref needs a CSS 'selector' "
    "in locator.\n\n"
    "Expected result: action, whatChanged (may be null), observationInvalidated.\n\n"
    "Recommended next step: browser_wait for an async transition; browser_assert to "
    "confirm; browser_observe if invalidated; browser_diagnose only after an unexpected "
    "failure."
)

BROWSER_ACT_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Perform exactly one interaction in the bound session. Does not verify business success.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "action": {
            "type": "string",
            "enum": ["click", "fill", "press", "hover", "select", "check", "uncheck", "upload", "drag", "scroll"],
            "description": (
                "click/fill/press/hover/select act on one element via 'ref' (preferred) or "
                "'locator'. check/uncheck/upload/drag require 'locator' (drag also "
                "'targetLocator') — no ref path exists for these four. scroll uses "
                "'deltaX'/'deltaY', not a ref."
            ),
        },
        "observationId": {
            "type": "string",
            "description": "observationId that produced 'ref'; required with 'ref'. Must be the latest one for this session.",
        },
        "ref": {
            "type": "string",
            "description": "A ref token from the latest browser_observe. Preferred for click/fill/press/hover/select; not usable for check/uncheck/upload/drag.",
        },
        "locator": _locator_schema("Fallback/requirement for click/fill/press/hover/select/check/uncheck/upload; the drag source."),
        "targetLocator": _locator_schema("The drag destination. Required for 'drag'; unused otherwise."),
        "value": {
            "type": "string",
            "description": "Text to type. Required for 'fill' (may be empty to clear); ignored otherwise.",
        },
        "key": {
            "type": "string",
            "description": "Key or shortcut (e.g. 'Enter', 'Control+A'). Required for 'press'; ignored otherwise.",
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Option value(s)/label(s) to select, matching real options. Required (non-empty) for 'select'.",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Approved local file path(s) to upload. Required (non-empty) for 'upload'.",
        },
        "deltaX": {
            "type": "number",
            "default": 0,
            "description": "Horizontal scroll distance in px (positive = right). Used only by 'scroll'.",
        },
        "deltaY": {
            "type": "number",
            "default": 0,
            "description": "Vertical scroll distance in px (positive = down). Used only by 'scroll'.",
        },
        "timeoutMs": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120000,
            "default": 30000,
            "description": "Max wait for the target to become actionable, in ms (max 120000).",
        },
    },
    "required": ["browserSessionId", "action"],
    "additionalProperties": False,
}


BROWSER_NAVIGATE_DESCRIPTION = (
    "Purpose: Open a URL or change history (reload/back/forward) in the bound tab.\n\n"
    "When to use: Direct URL or history control is intended.\n\n"
    "When not to use: Clicking a link/button (browser_act); waiting for the destination to "
    "finish loading (browser_wait, afterward); confirming the destination "
    "(browser_assert); inspecting it (browser_observe).\n\n"
    "Important capabilities: Returns resulting tab state and 'whatChanged' when available.\n\n"
    "Required sequencing: Observe before interacting with the destination — every prior "
    "ref/observationId is invalid after this call.\n\n"
    "Important limitations: Does not verify application-level success (a loaded page is "
    "not a verified outcome); does not touch page elements.\n\n"
    "Expected result: resulting url/title, whatChanged, observationsInvalidated=true.\n\n"
    "Recommended next step: browser_wait if still loading, then browser_observe, then "
    "browser_assert."
)

BROWSER_NAVIGATE_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Open a URL or change history in the bound tab. Invalidates every prior ref/observationId.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "operation": {
            "type": "string",
            "enum": ["open", "reload", "back", "forward"],
            "description": "'open' loads 'url' (required). 'reload' reloads. 'back'/'forward' move through history.",
        },
        "url": {
            "type": "string",
            "description": "Absolute URL to open. Required when operation is 'open'; use a URL already known or observed.",
        },
        "timeoutMs": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120000,
            "default": 30000,
            "description": "Max wait for the navigation/history change, in ms (max 120000).",
        },
    },
    "required": ["browserSessionId", "operation"],
    "additionalProperties": False,
}


BROWSER_WAIT_DESCRIPTION = (
    "Purpose: Wait for one supported browser condition without asserting a business "
    "outcome.\n\n"
    "When to use: Loading/rendering in progress; an element appearing/disappearing; a "
    "URL/navigation change; network idle; a popup, dialog, request, or response.\n\n"
    "When not to use: An arbitrary sleep; proving completion (browser_assert, afterward); "
    "inspecting (browser_observe); acting (browser_act).\n\n"
    "Important capabilities: A fixed set of conditions with a bounded timeout.\n\n"
    "Required sequencing: Pick the most specific condition; assert or observe afterward "
    "as needed.\n\n"
    "Important limitations: Success means only that the condition occurred, not that the "
    "outcome is correct — it is not an assertion; never runs arbitrary JavaScript.\n\n"
    "Expected result: condition, matched=true, a bounded result payload, and "
    "observationsInvalidated for a confirmed navigation/url change.\n\n"
    "Recommended next step: browser_assert to verify; browser_observe if the page "
    "changed; browser_diagnose only after an unexpected timeout."
)

BROWSER_WAIT_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Wait for one browser condition in the bound tab. Does not verify a business outcome.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "condition": {
            "type": "string",
            "enum": [
                "element_state", "url", "navigation", "page_load", "network_idle",
                "text", "popup", "dialog", "request", "response",
            ],
            "description": (
                "'element_state' needs 'locator'+'state'. 'url'/'request'/'response'/'text' "
                "need 'expected' (URL substring, or literal text for 'text'). "
                "'navigation'/'page_load'/'network_idle'/'popup'/'dialog' need only timeoutMs."
            ),
        },
        "locator": _locator_schema("Required for 'element_state'; unused otherwise."),
        "state": {
            "type": "string",
            "enum": ["attached", "visible", "hidden", "detached"],
            "default": "visible",
            "description": "Element state to wait for; used only with 'element_state'.",
        },
        "expected": {
            "type": "string",
            "description": "URL substring or literal text to wait for; required by url/request/response/text.",
        },
        "timeoutMs": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120000,
            "default": 30000,
            "description": "Max wait for the condition, in ms (max 120000); fails with TIMEOUT on expiry.",
        },
    },
    "required": ["browserSessionId", "condition"],
    "additionalProperties": False,
}


BROWSER_ASSERT_DESCRIPTION = (
    "Purpose: Verify one expected page/element condition. The preferred way to confirm an "
    "action succeeded or an outcome was reached.\n\n"
    "When to use: Confirming an action's effect; an element's visibility/state/value/text/"
    "count/attribute; the page title; the accessibility outline; proving a final outcome "
    "before reporting completion.\n\n"
    "When not to use: Discovering unknown elements (browser_observe); mutating (browser_act); "
    "navigating (browser_navigate); diagnosing an unexpected failure (browser_diagnose); as "
    "a substitute for a still-needed browser_wait.\n\n"
    "Important capabilities: A retrying bridge check with a bounded timeout, returning "
    "expected/actual and, on mismatch, bounded bridge diagnostic detail.\n\n"
    "Required sequencing: Use a locator/expectation already known; a failed assertion "
    "(passed=false) is a valid, informative result, not a tool error.\n\n"
    "Important limitations: Cannot discover unknown structure. Only a genuine "
    "condition-not-met mismatch is reported as ok:true/passed:false — scope, permission, "
    "input, and infrastructure errors remain ok:false.\n\n"
    "Expected result: assertion, passed, the expected value as supplied, actual (when "
    "available), and diagnostic detail on mismatch.\n\n"
    "Recommended next step: Report the outcome if passed; capture evidence or diagnose if "
    "not, before deciding whether to retry the underlying action."
)

BROWSER_ASSERT_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Verify one expected condition in the bound tab; the preferred way to confirm an outcome.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "assertion": {
            "type": "string",
            "enum": [
                "visible", "hidden", "enabled", "disabled", "editable", "checked",
                "value", "text", "count", "attribute", "title", "aria_snapshot",
            ],
            "description": (
                "'visible'..'checked' plus 'value'/'text'/'count'/'attribute' are element "
                "assertions and need 'locator'. 'title' checks the page title against "
                "'expected'. 'aria_snapshot' matches the accessibility outline."
            ),
        },
        "locator": _locator_schema("Required for every element assertion (all values except 'title'/'aria_snapshot')."),
        "expected": {
            "type": "string",
            "description": "Expected text/value/title/snapshot. Not used by visible/hidden/enabled/disabled/editable/checked/count.",
        },
        "attribute": {
            "type": "string",
            "description": "Attribute name to read. Required when assertion is 'attribute'.",
        },
        "count": {
            "type": "integer",
            "minimum": 0,
            "description": "Expected number of matching elements. Required when assertion is 'count'.",
        },
        "contains": {
            "type": "boolean",
            "default": False,
            "description": "Substring match instead of exact. Applies to 'text' and 'title' only.",
        },
        "timeoutMs": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120000,
            "default": 30000,
            "description": "Max time to retry before reporting passed=false, in ms (max 120000).",
        },
    },
    "required": ["browserSessionId", "assertion"],
    "additionalProperties": False,
}


BROWSER_CAPTURE_EVIDENCE_DESCRIPTION = (
    "Purpose: Produce a bounded artifact — screenshot, DOM/accessibility snapshot, PDF, or "
    "trace — from the bound tab.\n\n"
    "When to use: An explicit evidence request; preserving an important state; a failed "
    "assertion; diagnosis needing an artifact.\n\n"
    "When not to use: Routine page understanding (browser_observe); by default after every "
    "action; acting or asserting; console/network diagnostics (browser_diagnose).\n\n"
    "Important capabilities: Element screenshots are locator-based (no ref path). Large/"
    "binary results are saved to disk via the native host by default.\n\n"
    "Required sequencing: Choose the smallest useful capture; chain trace_start -> actions "
    "-> trace_stop/trace_export using the returned traceId.\n\n"
    "Important limitations: 'element_screenshot' requires 'locator'; 'trace_stop'/"
    "'trace_export' require 'traceId'. saveToDisk=false never returns a large payload to "
    "the LLM.\n\n"
    "Expected result: evidenceId, capture type, url, capturedAt, and either "
    "saved=true+path/bytes/mimeType or saved=false+a bounded preview.\n\n"
    "Recommended next step: Hand the path/metadata to the user, or browser_diagnose if it "
    "reveals a technical failure."
)

BROWSER_CAPTURE_EVIDENCE_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Produce a bounded evidence artifact from the bound tab. Not for routine page understanding.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "capture": {
            "type": "string",
            "enum": [
                "page_screenshot", "element_screenshot", "dom_snapshot",
                "accessibility_snapshot", "pdf", "trace_start", "trace_stop", "trace_export",
            ],
            "description": (
                "'element_screenshot' requires 'locator'. 'trace_stop'/'trace_export' "
                "require 'traceId' from a prior 'trace_start'. Others need no extra args "
                "beyond filename/saveToDisk."
            ),
        },
        "locator": _locator_schema("Required for 'element_screenshot' (locator-based, not ref-based)."),
        "filename": {
            "type": "string",
            "description": "Optional safe filename (no path separators or '..'); generated if omitted. Ignored when saveToDisk is false.",
        },
        "traceId": {
            "type": "string",
            "description": "Trace id from a prior 'trace_start'. Required for 'trace_stop'/'trace_export'.",
        },
        "saveToDisk": {
            "type": "boolean",
            "default": True,
            "description": "Save artifacts to disk via the native host (default true). False returns only bounded metadata/preview, never a large payload.",
        },
    },
    "required": ["browserSessionId", "capture"],
    "additionalProperties": False,
}


BROWSER_DIAGNOSE_DESCRIPTION = (
    "Purpose: Investigate a technical failure, hang, or explicit diagnostic request in the "
    "bound tab.\n\n"
    "When to use: An unexpected action/assertion failure; a stuck page; suspected console/"
    "network errors; trace status; an explicit diagnostic request.\n\n"
    "When not to use: Normal observation/action/navigation/waiting; business-outcome "
    "verification (browser_assert); user-facing evidence (browser_capture_evidence).\n\n"
    "Important capabilities: Bounded, redacted console/network reads; a DOM snapshot "
    "summary; trace status; response_body is opt-in and off by default.\n\n"
    "Required sequencing: Pick the smallest diagnostic source likely to explain the "
    "specific problem observed.\n\n"
    "Important limitations: response_body can still leak app-specific secrets beyond the "
    "redaction list even when enabled. Never modifies the page; does not replace "
    "browser_observe/browser_wait/browser_assert.\n\n"
    "Expected result: diagnostic type, redactionApplied=true, bounded entries with a "
    "truncated flag.\n\n"
    "Recommended next step: Retry only on a clearly transient signal; otherwise report or "
    "escalate."
)

BROWSER_DIAGNOSE_PARAMS: Dict[str, Any] = {
    "type": "object",
    "description": "Investigate a technical failure in the bound tab. Not for interaction, observation, or verification.",
    "properties": {
        "browserSessionId": {
            "type": "string",
            "description": "Session id from bind_active_managed_tab(); not a Chrome tabId.",
        },
        "diagnostic": {
            "type": "string",
            "enum": ["console", "network", "response_body", "dom", "trace_status"],
            "description": (
                "'response_body' needs 'requestId' and is disabled unless the host enabled "
                "allow_response_body. 'console'/'network' return buffered events bounded "
                "by 'limit'. 'dom' returns a diagnostic snapshot summary. 'trace_status' "
                "optionally scoped by 'traceId'."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 100,
            "description": "Max buffered events to return. Used by 'console'/'network'.",
        },
        "requestId": {
            "type": "string",
            "description": "Request id from a prior 'network' event. Required for 'response_body'.",
        },
        "traceId": {
            "type": "string",
            "description": "Optional trace id to scope 'trace_status' to one trace.",
        },
    },
    "required": ["browserSessionId", "diagnostic"],
    "additionalProperties": False,
}


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {"type": "function", "function": {"name": "browser_observe", "description": BROWSER_OBSERVE_DESCRIPTION, "parameters": BROWSER_OBSERVE_PARAMS}},
    {"type": "function", "function": {"name": "browser_act", "description": BROWSER_ACT_DESCRIPTION, "parameters": BROWSER_ACT_PARAMS}},
    {"type": "function", "function": {"name": "browser_navigate", "description": BROWSER_NAVIGATE_DESCRIPTION, "parameters": BROWSER_NAVIGATE_PARAMS}},
    {"type": "function", "function": {"name": "browser_wait", "description": BROWSER_WAIT_DESCRIPTION, "parameters": BROWSER_WAIT_PARAMS}},
    {"type": "function", "function": {"name": "browser_assert", "description": BROWSER_ASSERT_DESCRIPTION, "parameters": BROWSER_ASSERT_PARAMS}},
    {"type": "function", "function": {"name": "browser_capture_evidence", "description": BROWSER_CAPTURE_EVIDENCE_DESCRIPTION, "parameters": BROWSER_CAPTURE_EVIDENCE_PARAMS}},
    {"type": "function", "function": {"name": "browser_diagnose", "description": BROWSER_DIAGNOSE_DESCRIPTION, "parameters": BROWSER_DIAGNOSE_PARAMS}},
]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return the seven agent-facing tool definitions in standard
    function-tool format, ready to hand to an LLM function-calling API.

    Returns a deep copy of the module-level TOOL_DEFINITIONS so a caller
    that mutates the result (e.g. an SDK that annotates tool dicts in
    place) cannot corrupt the schemas for every other caller/session."""
    return copy.deepcopy(TOOL_DEFINITIONS)


# =====================================================================
# Scope-provider interface
# =====================================================================
#
# Every public tool authorizes through a ScopeProvider before issuing an RPC.
# This is the seam a future external firewall plugs into: swap the provider
# passed to BrowserAgentTools() and none of the seven public tools change.

class ScopeDecision:
    """A scope authorization outcome. Mirrors the JSON shape from the design
    brief: {"allowed": bool, "reason": str|None, "policyId": str|None}."""

    __slots__ = ("allowed", "reason", "policy_id")

    def __init__(self, allowed: bool, reason: Optional[str] = None, policy_id: Optional[str] = None):
        self.allowed = allowed
        self.reason = reason
        self.policy_id = policy_id

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "policyId": self.policy_id}


class ScopeProvider:
    """Minimal interface a scope/firewall implementation must satisfy."""

    def authorize(self, context: Dict[str, Any]) -> ScopeDecision:
        raise NotImplementedError


class ManagedTabGroupScopeProvider(ScopeProvider):
    """Current default scope provider.

    Allows an operation once its browserSessionId has been resolved from a
    verified Agent-managed bridge session/tab (see
    :meth:`BrowserAgentTools.bind_active_managed_tab`). It performs no
    additional per-method policy — the Browser Agent Bridge itself is the
    final enforcement boundary for tab-group isolation (see
    ``extension/sw/tab-scope.js`` / ``sessions.js``); this provider only
    confirms Axis Agent has a legitimately bound session before it will even
    attempt the call.
    """

    POLICY_ID = "managed-tab-group"

    def authorize(self, context: Dict[str, Any]) -> ScopeDecision:
        if not context.get("browserSessionId") or not context.get("bridgeSessionId") or context.get("tabId") is None:
            return ScopeDecision(False, "No Agent-managed browser session is bound.", self.POLICY_ID)
        return ScopeDecision(True, None, self.POLICY_ID)


class ExternalFirewallScopeProvider(ScopeProvider):
    """Placeholder for a future external-firewall policy engine.

    Intentionally unimplemented — wiring an external firewall (network
    calls, policy storage, an administration UI) is out of scope for this
    tool layer. Swap this in for ManagedTabGroupScopeProvider once a
    firewall is available; no change to the seven public tools is required
    because every tool already routes its RPC through
    ``BrowserAgentTools.scope_provider.authorize()``.
    """

    def authorize(self, context: Dict[str, Any]) -> ScopeDecision:
        raise NotImplementedError("External firewall integration is not configured")


# =====================================================================
# Bridge-method mappings
# =====================================================================
# Every mapping below was verified line-by-line against
# browser-agent-bridge-main/extension/service-worker.js (the method
# registry) and the relevant extension/sw/*.js handler. See the README for
# notes on the two corrections made to the brief's suggested fallback list
# (no non-ref locator.hover exists; check/uncheck's ref path is
# locator.clickRef, not a dedicated ref method).

OBSERVE_METHOD_MAP = {
    "compact": "page.accessibilityTree",
    "aria": "page.ariaSnapshot",
    "text": "page.readText",
}

NAV_METHOD_MAP = {
    "open": "page.navigate",
    "reload": "page.reload",
    "back": "page.goBack",
    "forward": "page.goForward",
}

WAIT_METHOD_MAP = {
    "element_state": "locator.waitFor",
    "url": "page.waitForURL",
    "navigation": "page.waitForNavigation",
    "page_load": "page.waitForLoad",
    "network_idle": "page.waitForNetworkIdle",
    "text": "page.waitForText",
    "popup": "page.waitForPopup",
    "dialog": "page.waitForDialog",
    "request": "page.waitForRequest",
    "response": "page.waitForResponse",
}

ASSERT_METHOD_MAP = {
    "visible": "expect.locator.toBeVisible",
    "hidden": "expect.locator.toBeHidden",
    "enabled": "expect.locator.toBeEnabled",
    "disabled": "expect.locator.toBeDisabled",
    "editable": "expect.locator.toBeEditable",
    "checked": "expect.locator.toBeChecked",
    "value": "expect.locator.toHaveValue",
    "text": "expect.locator.toHaveText",
    "count": "expect.locator.toHaveCount",
    "attribute": "expect.locator.toHaveAttribute",
    "title": "expect.page.toHaveTitle",
    "aria_snapshot": "expect.page.toMatchAriaSnapshot",
}
LOCATOR_ASSERTIONS = frozenset({
    "visible", "hidden", "enabled", "disabled", "editable", "checked", "value", "text", "count", "attribute",
})

CAPTURE_METHOD_MAP = {
    "page_screenshot": "page.screenshot",
    "element_screenshot": "locator.screenshot",
    "dom_snapshot": "page.domSnapshot",
    "accessibility_snapshot": "page.accessibilityTree",
    "pdf": "page.pdf",
    "trace_start": "trace.start",
    "trace_stop": "trace.stop",
    "trace_export": "trace.exportHtml",
}

DIAGNOSTIC_METHOD_MAP = {
    "console": "console.read",
    "network": "network.read",
    "response_body": "network.getResponseBody",
    "dom": "page.domSnapshot",
    "trace_status": "trace.status",
}

# browser_act's mapping is action- and ref-availability-dependent, so it is
# built dynamically in BrowserAgentTools._build_act_call rather than as a
# static dict; ACT_REF_METHODS / ACT_LOCATOR_METHODS document it for tests
# and README purposes.
ACT_REF_METHODS = {
    "click": "locator.clickRef",
    "fill": "locator.fillRef",
    "press": "locator.pressRef",
    "hover": "locator.hoverRef",
    "select": "locator.selectOptionRef",
}
ACT_LOCATOR_METHODS = {
    "click": "locator.click",
    "fill": "locator.fill",
    "press": "locator.press",
    "hover": "dom.hover",  # no locator.hover exists in the bridge; see README
    "select": "locator.selectOption",
    "check": "locator.check",
    "uncheck": "locator.uncheck",
    "upload": "locator.setInputFiles",
    "drag": "locator.dragTo",
}
# Actions with no ref-based bridge method at all, so 'ref' is always rejected
# for them: upload/drag (locator.setInputFilesRef/locator.dragToRef don't
# exist), and check/uncheck (the bridge has no checkRef/uncheckRef — only
# locator.clickRef, which unconditionally toggles and would be
# nondeterministic against an already-observed-but-possibly-stale state or a
# live application change; see README).
ACT_NO_REF_ACTIONS = frozenset({"upload", "drag", "check", "uncheck"})
ACT_SCROLL_METHOD = "dom.scroll"


# =====================================================================
# Browser session and observation state helpers
# =====================================================================

_REF_LINE_PATTERN = re.compile(r"^\[f(\d+):([^\]]+)\](.*)$")


def _rewrite_compact_snapshot(raw_snapshot: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Rewrite a raw bridge compact-mode snapshot (refs shaped 'f{frameId}:
    {bridgeRef}') into Axis Agent's own sequential ref_N tokens, and build
    the ref -> {frameId, bridgeRef} map used by _resolve_ref. This is what
    keeps the bridge's internal ref format out of the LLM's hands."""
    refs: Dict[str, Dict[str, Any]] = {}
    out_lines: List[str] = []
    counter = 0
    for line in (raw_snapshot or "").split("\n"):
        match = _REF_LINE_PATTERN.match(line)
        if not match:
            out_lines.append(line)
            continue
        counter += 1
        agent_ref = f"ref_{counter}"
        frame_id = int(match.group(1))
        bridge_ref = match.group(2)
        remainder = match.group(3)
        refs[agent_ref] = {"frameId": frame_id, "bridgeRef": bridge_ref}
        out_lines.append(f"[{agent_ref}]{remainder}")
    return "\n".join(out_lines), refs


def _bound_text(text: str, limit: int = MAX_TEXT_CHARS) -> Tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]", True


# =====================================================================
# BrowserAgentTools implementation
# =====================================================================

class BrowserAgentTools:
    """Binds one Agent-managed Chrome tab and exposes the seven semantic
    browser tools against it. See the module docstring for the overall
    design and the README for a worked example.
    """

    def __init__(
        self,
        bridge_client: BrowserBridgeClient,
        scope_provider: Optional[ScopeProvider] = None,
        allow_response_body: bool = False,
    ):
        self.bridge_client = bridge_client
        self.scope_provider: ScopeProvider = scope_provider or ManagedTabGroupScopeProvider()
        self.allow_response_body = allow_response_body

        # Lightweight in-memory state (see class docstring / README —
        # deliberately not persisted, not a database).
        self.browser_sessions: Dict[str, Dict[str, Any]] = {}
        self.observations: Dict[str, Dict[str, Any]] = {}

    # -- internal: bridge call safety net --------------------------------

    def _assert_method_allowed(self, method: str) -> None:
        """Defense-in-depth check run immediately before every RPC, on top
        of the per-tool mapping tables (which never produce a forbidden
        method) and the scope provider (checked separately by each tool).

        This is a positive allowlist, not just a denylist: a method must be
        one of the ~40 methods this module's mapping tables actually use
        (``_ALLOWED_METHODS``) *and* not be in ``FORBIDDEN_METHODS``.
        Checking membership in the forbidden set alone would let an
        unrecognized/typo'd method through by default; requiring allowlist
        membership means an unmapped or invented method — including any
        ``computer.*`` coordinate method, which is absent from
        ``_ALLOWED_METHODS`` because no schema in this layer accepts
        coordinates — is rejected even if nobody remembered to also forbid
        it."""
        if method in FORBIDDEN_METHODS:
            raise BridgeMethodNotAllowedError(f"Bridge method '{method}' is forbidden for agent tools.")
        if method not in _ALLOWED_METHODS:
            raise BridgeMethodNotAllowedError(
                f"Bridge method '{method}' is not in the allowed method set for agent tools."
            )

    def _rpc(self, method: str, params: Dict[str, Any]) -> Any:
        self._assert_method_allowed(method)
        return self.bridge_client.rpc(method, params)

    def _call_bridge(
        self, session_id: Optional[str], method: str, params: Dict[str, Any]
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Run one RPC and translate any failure into the common error
        envelope. Returns (result, None) on success or (None, error_response)."""
        try:
            return self._rpc(method, params), None
        except BridgeMethodNotAllowedError as error:
            return None, _err(session_id, SENSITIVE_OPERATION_BLOCKED, str(error), False)
        except BrowserBridgeError as error:
            return None, _bridge_error_response(session_id, error)

    def _save_data_url(
        self, session_id: Optional[str], data_url: str, filename: Optional[str]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        # Explicit internal allowlist/security check: this is the only code
        # path in the module that reaches the native host, and it does so
        # through BrowserBridgeClient.save_data_url() rather than self._rpc(),
        # so it would otherwise bypass _assert_method_allowed() entirely.
        # Re-derive the same guarantee here rather than silently trusting
        # the caller: only native.saveDataUrl is ever invoked, and only with
        # a genuine data: URL (never an arbitrary path or remote URL).
        method = "native.saveDataUrl"
        if method not in _ALLOWED_NATIVE_METHODS or method in FORBIDDEN_METHODS:
            return None, _err(session_id, SENSITIVE_OPERATION_BLOCKED, f"'{method}' is not an allowed native operation.", False)
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None, _err(session_id, INVALID_ARGUMENT, "Refusing to save a value that is not a data: URL.", False)
        try:
            result = self.bridge_client.save_data_url(data_url, filename=filename)
            return result, None
        except BrowserBridgeError as error:
            return None, _bridge_error_response(session_id, error)

    def _save_text(
        self, session_id: Optional[str], text: str, filename: str, mime_type: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        # The native host's data-URL parser (native/host.py's DATA_URL_RE) is
        # deliberately simple: ^data:([^;,]+)?(;base64)?,(.*)$ — the mime
        # group cannot contain a ';', and at most one literal ';base64'
        # segment is allowed before the comma. A 'data:...;charset=utf-8;
        # base64,...' URL does not match that pattern at all and is
        # rejected as "Invalid data URL", so charset must not be included
        # here (the text was already encoded as UTF-8 bytes below, so no
        # information is lost by omitting it).
        encoded = base64.b64encode((text or "").encode("utf-8")).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        saved, error_response = self._save_data_url(session_id, data_url, filename)
        if error_response:
            return None, error_response
        return {
            "path": saved.get("path") if saved else None,
            "bytes": saved.get("bytes") if saved else None,
            "mimeType": saved.get("mimeType") if saved else None,
        }, None

    # -- scope authorization ----------------------------------------------

    def _authorize(
        self,
        semantic_tool: str,
        bridge_method: str,
        session: Dict[str, Any],
        action: Optional[str] = None,
        request_summary: Optional[Dict[str, Any]] = None,
    ) -> ScopeDecision:
        context = {
            "semanticTool": semantic_tool,
            "bridgeMethod": bridge_method,
            "browserSessionId": session.get("browserSessionId"),
            "bridgeSessionId": session.get("bridgeSessionId"),
            "tabId": session.get("tabId"),
            "url": session.get("url"),
            "action": action,
            "requestSummary": request_summary or {},
        }
        return self.scope_provider.authorize(context)

    # -- session binding ----------------------------------------------------

    @staticmethod
    def _session_sort_key(summary: Dict[str, Any]) -> Tuple[str, str, str]:
        # Deterministic, reproducible ordering across repeated calls and
        # regardless of the order session.list happens to return: most
        # recently updated session first, ties broken by createdAt, final
        # tiebreak by id so the ordering never depends on dict/list iteration
        # order from the bridge.
        return (
            str(summary.get("updatedAt") or ""),
            str(summary.get("createdAt") or ""),
            str(summary.get("id") or ""),
        )

    def bind_active_managed_tab(self) -> Dict[str, Any]:
        """Find Chrome's actually-focused tab (the active tab of the
        last-focused window) and, only if it belongs to an Agent-managed
        bridge session, mint an Axis Agent browserSessionId for it. Must be
        called (and succeed) before any of the seven tools can be used for a
        given session. Never falls back to an unmanaged tab, a session's
        mainTabId, or "the most recently updated session" as a proxy for
        what the user is actually looking at.

        The bridge's tab-isolation gate (assertRpcTabIsolation in
        extension/service-worker.js) requires tabs.list's query to already
        carry a known Agent-managed groupId — there is no unscoped "list
        every tab" escape hatch, by design. So this cannot first ask Chrome
        "what tab is focused?" and then check whether it happens to be
        managed; instead, for each managed session it already knows about
        (from session.list/session.get), it asks the bridge the narrower,
        in-bounds question "is *this* group's active tab also the active
        tab of Chrome's last-focused window?" via:

            tabs.list({"query": {"groupId": <managed groupId>,
                                  "active": True, "lastFocusedWindow": True}})

        Chrome has exactly one last-focused window and exactly one active
        tab per window, so at most one managed group can ever satisfy that
        query at a time — the first (and only) match is Chrome's actual
        focused tab, confirmed to be Agent-managed, with no ambiguity. A managed
        session with no numeric groupId (tab-based only) cannot be checked
        this way and is skipped, since there is no bridge-compliant way to
        confirm its focus state.

        Only browserSessionId and the public url are returned in ``data``;
        the internal tabId/windowId/groupId/bridgeSessionId stay in
        ``self.browser_sessions`` and are never handed back to the caller.
        """
        result, error_response = self._call_bridge(None, "session.list", {})
        if error_response:
            return error_response

        sessions = (result or {}).get("sessions") or []
        candidates = [s for s in sessions if isinstance(s, dict) and s.get("id")]
        candidates.sort(key=self._session_sort_key, reverse=True)  # deterministic iteration order

        for summary in candidates:
            bridge_session_id = summary["id"]
            detail, detail_error = self._call_bridge(None, "session.get", {"sessionId": bridge_session_id})
            if detail_error:
                continue
            session_info = (detail or {}).get("session") or summary
            group_id = session_info.get("groupId")
            if not isinstance(group_id, int):
                continue

            focused, focus_error = self._call_bridge(
                None, "tabs.list",
                {"query": {"groupId": group_id, "active": True, "lastFocusedWindow": True}},
            )
            if focus_error:
                continue
            focused_tabs = (focused or {}).get("tabs") or []
            active_tab = next(
                (t for t in focused_tabs if isinstance(t, dict) and isinstance(t.get("id"), int)), None,
            )
            if active_tab is None:
                continue  # this managed group is not where Chrome's focus currently is

            browser_session_id = f"bs-{uuid.uuid4().hex[:12]}"
            record = {
                "browserSessionId": browser_session_id,
                "bridgeSessionId": bridge_session_id,
                "tabId": active_tab["id"],
                "windowId": active_tab.get("windowId"),
                "groupId": active_tab.get("groupId", group_id),
                "url": active_tab.get("url"),
            }
            self.browser_sessions[browser_session_id] = record
            return _ok(browser_session_id, {"browserSessionId": browser_session_id, "url": record["url"]})

        return _err(
            None, NO_MANAGED_TAB,
            "The currently focused Chrome tab is not part of an Agent-managed browser session.", True,
        )

    def _require_session(self, session_id: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if not isinstance(session_id, str) or session_id not in self.browser_sessions:
            return None, _err(
                session_id if isinstance(session_id, str) else None,
                SESSION_NOT_FOUND,
                f"No bound browser session found for browserSessionId {session_id!r}. "
                f"Call bind_active_managed_tab() before using any browser tool.",
                False,
            )
        return self.browser_sessions[session_id], None

    # -- observation state --------------------------------------------------

    def _store_observation(
        self, session_id: str, tab_id: int, snapshot_id: Optional[str], url: Optional[str],
        refs: Dict[str, Dict[str, Any]],
    ) -> str:
        observation_id = f"obs-{uuid.uuid4().hex[:12]}"
        self.observations[observation_id] = {
            "browserSessionId": session_id,
            "tabId": tab_id,
            "snapshotId": snapshot_id,
            "url": url,
            "createdAt": time.time(),
            "stale": False,
            "refs": refs,
        }
        return observation_id

    def _get_observation(self, observation_id: str) -> Optional[Dict[str, Any]]:
        return self.observations.get(observation_id)

    def _invalidate_session_observations(self, session_id: str) -> None:
        for obs in self.observations.values():
            if obs.get("browserSessionId") == session_id:
                obs["stale"] = True

    def _resolve_ref(
        self, session_id: str, observation_id: Any, ref: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if not isinstance(observation_id, str) or not observation_id:
            return None, _err(
                session_id, STALE_OBSERVATION,
                "'observationId' is required to use a ref; call browser_observe first.", True,
            )
        obs = self._get_observation(observation_id)
        if obs is None:
            return None, _err(
                session_id, STALE_OBSERVATION,
                f"Observation {observation_id!r} was not found. Call browser_observe again.", True,
            )
        if obs["browserSessionId"] != session_id:
            return None, _err(
                session_id, STALE_OBSERVATION,
                "That observation belongs to a different browser session. Call browser_observe again.", True,
            )
        if obs["stale"]:
            return None, _err(
                session_id, STALE_OBSERVATION,
                "The page changed since this observation. Call browser_observe again.", True,
            )
        ref_info = obs["refs"].get(ref)
        if ref_info is None:
            return None, _err(
                session_id, REF_NOT_FOUND,
                f"Ref {ref!r} was not found in observation {observation_id!r}.", False,
            )
        return {
            "frameId": ref_info["frameId"],
            "bridgeRef": ref_info["bridgeRef"],
            "snapshotId": obs["snapshotId"],
            "tabId": obs["tabId"],
        }, None

    def _ref_params(
        self, tab_id: int, resolved_ref: Dict[str, Any], timeout_ms: int, extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Deliberately no "a11yDiff": True here. wrapWithActionObserver
        # (extension/sw/action-observer.js) captures a pre-action
        # accessibility tree when a11yDiff is requested, and building that
        # tree calls page.accessibilityTree, which mints and installs a
        # *new* snapshotId server-side before the ref action itself runs.
        # The ref action then arrives carrying the (now superseded)
        # snapshotId this observation stored, and the content script
        # (extension/content/accessibility-tree.js) rejects it as a stale
        # accessibility snapshot. Omitting a11yDiff keeps the observer's
        # cheap default probe (URL/focus/popup via whatChanged) without
        # ever re-minting a snapshotId out from under the ref we're using.
        params = {
            "tabId": tab_id,
            "ref": resolved_ref["bridgeRef"],
            "frameId": resolved_ref["frameId"],
            "snapshotId": resolved_ref["snapshotId"],
            "timeoutMs": timeout_ms,
        }
        if extra:
            params.update(extra)
        return params

    # ------------------------------------------------------------------
    # Tool 1: browser_observe
    # ------------------------------------------------------------------

    def browser_observe(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_OBSERVE_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        mode = cleaned.get("mode", "compact")
        max_nodes = cleaned.get("maxNodes", DEFAULT_MAX_NODES)
        method = OBSERVE_METHOD_MAP[mode]
        params: Dict[str, Any] = {"tabId": session["tabId"]}
        if mode == "compact":
            params.update({"format": "compact", "maxNodes": max_nodes})

        decision = self._authorize("browser_observe", method, session, action=mode)
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Observation denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response
        result = result if isinstance(result, dict) else {}

        # A fresh observation supersedes every earlier one for this session:
        # once this call returns, refs minted before it must never be usable
        # again (the page may have moved on since they were captured), so any
        # stale observation for this browserSessionId is retired first.
        self._invalidate_session_observations(session_id)

        if mode == "compact":
            rewritten, refs = _rewrite_compact_snapshot(result.get("snapshot") or "")
            bounded_snapshot, extra_truncated = _bound_text(rewritten)
            observation_id = self._store_observation(session_id, session["tabId"], result.get("snapshotId"), result.get("url"), refs)
            if result.get("url"):
                session["url"] = result["url"]
            data = {
                "observationId": observation_id,
                "snapshotId": result.get("snapshotId"),
                "url": result.get("url"),
                "title": result.get("title"),
                "snapshot": bounded_snapshot,
                "truncated": bool(result.get("truncated")) or extra_truncated,
            }
        elif mode == "aria":
            bounded_snapshot, extra_truncated = _bound_text(result.get("snapshot") or "")
            observation_id = self._store_observation(session_id, session["tabId"], None, session.get("url"), {})
            data = {
                "observationId": observation_id,
                "snapshotId": None,
                "url": session.get("url"),
                "title": None,
                "snapshot": bounded_snapshot,
                "truncated": extra_truncated,
            }
        else:  # text
            bounded_snapshot, extra_truncated = _bound_text(result.get("text") or "")
            observation_id = self._store_observation(session_id, session["tabId"], None, result.get("url"), {})
            if result.get("url"):
                session["url"] = result["url"]
            data = {
                "observationId": observation_id,
                "snapshotId": None,
                "url": result.get("url"),
                "title": result.get("title"),
                "snapshot": bounded_snapshot,
                "truncated": extra_truncated,
            }

        return _ok(session_id, data)

    # ------------------------------------------------------------------
    # Tool 2: browser_act
    # ------------------------------------------------------------------

    def _build_act_call(
        self, action: str, args: Dict[str, Any], session: Dict[str, Any], resolved_ref: Optional[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, Any]]:
        tab_id = session["tabId"]
        session_id = session["browserSessionId"]
        timeout_ms = args.get("timeoutMs", DEFAULT_TIMEOUT_MS)

        locator = _resolve_locator_arg(session_id, args.get("locator"), "locator")

        def require_locator() -> Dict[str, Any]:
            if locator is None:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"action '{action}' requires a 'locator'.", False))
            return locator

        def require_ref_or_locator() -> None:
            if resolved_ref is None and locator is None:
                raise _ToolArgError(_err(
                    session_id, INVALID_ARGUMENT,
                    f"action '{action}' requires either 'ref' (with 'observationId') from the "
                    f"latest browser_observe, or a 'locator'.", False,
                ))

        if action in ACT_NO_REF_ACTIONS and resolved_ref is not None:
            raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, _no_ref_method_message(action), False))

        if action == "click":
            require_ref_or_locator()
            if resolved_ref is not None:
                return "locator.clickRef", self._ref_params(tab_id, resolved_ref, timeout_ms)
            return "locator.click", {"tabId": tab_id, "timeoutMs": timeout_ms, "locator": require_locator()}

        if action == "fill":
            value = args.get("value")
            if not isinstance(value, str):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'fill' requires a string 'value'.", False))
            require_ref_or_locator()
            if resolved_ref is not None:
                return "locator.fillRef", self._ref_params(tab_id, resolved_ref, timeout_ms, {"text": value})
            return "locator.fill", {"tabId": tab_id, "timeoutMs": timeout_ms, "text": value, "locator": require_locator()}

        if action == "press":
            key = args.get("key")
            if not isinstance(key, str) or not key:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'press' requires a non-empty string 'key'.", False))
            require_ref_or_locator()
            if resolved_ref is not None:
                return "locator.pressRef", self._ref_params(tab_id, resolved_ref, timeout_ms, {"key": key})
            return "locator.press", {"tabId": tab_id, "timeoutMs": timeout_ms, "key": key, "locator": require_locator()}

        if action == "hover":
            if resolved_ref is not None:
                return "locator.hoverRef", self._ref_params(tab_id, resolved_ref, timeout_ms)
            loc = require_locator()
            if not loc.get("selector"):
                raise _ToolArgError(_err(
                    session_id, INVALID_ARGUMENT,
                    "action 'hover' without a ref requires a CSS 'selector' in 'locator'; "
                    "the bridge has no non-ref hover-by-role/text method.", False,
                ))
            return "dom.hover", {"tabId": tab_id, "selector": loc["selector"]}

        if action == "select":
            options = args.get("options")
            if not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'select' requires a non-empty array of string 'options'.", False))
            require_ref_or_locator()
            if resolved_ref is not None:
                return "locator.selectOptionRef", self._ref_params(tab_id, resolved_ref, timeout_ms, {"options": options})
            return "locator.selectOption", {"tabId": tab_id, "timeoutMs": timeout_ms, "options": options, "locator": require_locator()}

        if action in ("check", "uncheck"):
            # No ref path at all: the bridge has no checkRef/uncheckRef, only
            # locator.clickRef, which unconditionally toggles whatever is
            # currently there. That is nondeterministic against a live page
            # (the checkbox may have changed since the ref was observed), so
            # a semantic locator — which locator.check/uncheck read and set
            # explicitly rather than toggle — is required instead. 'ref' is
            # already rejected earlier (see ACT_NO_REF_ACTIONS) before this
            # branch is ever reached.
            method = "locator.check" if action == "check" else "locator.uncheck"
            return method, {"tabId": tab_id, "timeoutMs": timeout_ms, "locator": require_locator()}

        if action == "upload":
            files = args.get("files")
            if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'upload' requires a non-empty array of string 'files'.", False))
            return "locator.setInputFiles", {"tabId": tab_id, "timeoutMs": timeout_ms, "files": files, "locator": require_locator()}

        if action == "drag":
            # No ref-based bridge method exists (locator.dragToRef does not
            # exist), so drag always requires both locators; 'targetRef' is
            # not part of the schema at all (additionalProperties: false
            # rejects it before this is reached).
            target_locator = _resolve_locator_arg(session_id, args.get("targetLocator"), "targetLocator")
            if locator is None or target_locator is None:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'drag' requires both 'locator' (source) and 'targetLocator' (destination).", False))
            return "locator.dragTo", {"tabId": tab_id, "timeoutMs": timeout_ms, "locator": locator, "targetLocator": target_locator}

        if action == "scroll":
            if resolved_ref is not None:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "action 'scroll' does not use 'ref'; use 'deltaX'/'deltaY' (and optionally a CSS 'selector' in 'locator').", False))
            delta_x = args.get("deltaX", 0)
            delta_y = args.get("deltaY", 0)
            params: Dict[str, Any] = {"tabId": tab_id, "x": delta_x, "y": delta_y, "mode": "scrollBy"}
            if locator and locator.get("selector"):
                params["selector"] = locator["selector"]
            return ACT_SCROLL_METHOD, params

        raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"Unknown action {action!r}.", False))

    def browser_act(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_ACT_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        action = cleaned["action"]
        ref = cleaned.get("ref")
        resolved_ref = None
        if ref is not None:
            if action in ACT_NO_REF_ACTIONS:
                return _err(session_id, INVALID_ARGUMENT, _no_ref_method_message(action), False)
            resolved_ref, error_response = self._resolve_ref(session_id, cleaned.get("observationId"), ref)
            if error_response:
                return error_response

        try:
            method, params = self._build_act_call(action, cleaned, session, resolved_ref)
        except _ToolArgError as tool_error:
            return tool_error.response

        decision = self._authorize(
            "browser_act", method, session, action=action,
            request_summary={"hasRef": ref is not None, "hasLocator": cleaned.get("locator") is not None},
        )
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Action denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response
        result = result if isinstance(result, dict) else {}

        what_changed = result.get("whatChanged")
        invalidated = False
        # wrapWithActionObserver (extension/sw/action-observer.js) reports a
        # navigation as whatChanged.urlChanged (bool) + whatChanged.toUrl /
        # .fromUrl — there is no whatChanged.url field. Checking for a
        # nonexistent key meant an action-triggered navigation (e.g. clicking
        # a link) was silently missed and stale refs stayed "usable".
        if isinstance(what_changed, dict) and what_changed.get("urlChanged") is True:
            to_url = what_changed.get("toUrl")
            if isinstance(to_url, str) and to_url:
                session["url"] = to_url
            self._invalidate_session_observations(session_id)
            invalidated = True

        return _ok(session_id, {
            "action": action,
            "whatChanged": what_changed,
            "observationInvalidated": invalidated,
        })

    # ------------------------------------------------------------------
    # Tool 3: browser_navigate
    # ------------------------------------------------------------------

    def browser_navigate(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_NAVIGATE_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        operation = cleaned["operation"]
        timeout_ms = cleaned.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        method = NAV_METHOD_MAP[operation]
        params: Dict[str, Any] = {"tabId": session["tabId"], "timeoutMs": timeout_ms}
        if operation == "open":
            url = cleaned.get("url")
            if not isinstance(url, str) or not url:
                return _err(session_id, INVALID_ARGUMENT, "operation 'open' requires a non-empty 'url'.", False)
            params["url"] = url

        decision = self._authorize("browser_navigate", method, session, action=operation, request_summary={"url": cleaned.get("url")})
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Navigation denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response
        result = result if isinstance(result, dict) else {}

        self._invalidate_session_observations(session_id)
        tab_info = result.get("tab") if isinstance(result.get("tab"), dict) else {}
        new_url = tab_info.get("url")
        if new_url:
            session["url"] = new_url

        return _ok(session_id, {
            "operation": operation,
            "url": new_url,
            "title": tab_info.get("title"),
            "whatChanged": result.get("whatChanged"),
            "observationsInvalidated": True,
        })

    # ------------------------------------------------------------------
    # Tool 4: browser_wait
    # ------------------------------------------------------------------

    def _build_wait_call(self, condition: str, args: Dict[str, Any], session: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        tab_id = session["tabId"]
        session_id = session["browserSessionId"]
        timeout_ms = args.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        method = WAIT_METHOD_MAP[condition]
        params: Dict[str, Any] = {"tabId": tab_id, "timeoutMs": timeout_ms}
        expected = args.get("expected")

        if condition == "element_state":
            locator = _resolve_locator_arg(session_id, args.get("locator"), "locator")
            if locator is None:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "condition 'element_state' requires a 'locator'.", False))
            params["locator"] = locator
            params["state"] = args.get("state") or "visible"
            return method, params

        if condition in ("url", "request", "response"):
            if not isinstance(expected, str) or not expected:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"condition '{condition}' requires a string 'expected' URL (or substring).", False))
            params["urlContains"] = expected
            return method, params

        if condition == "text":
            if not isinstance(expected, str) or not expected:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "condition 'text' requires a string 'expected' text.", False))
            params["text"] = expected
            return method, params

        if condition in ("navigation", "page_load", "network_idle", "popup", "dialog"):
            return method, params

        raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"Unknown condition {condition!r}.", False))

    def browser_wait(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_WAIT_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        condition = cleaned["condition"]
        try:
            method, params = self._build_wait_call(condition, cleaned, session)
        except _ToolArgError as tool_error:
            return tool_error.response

        decision = self._authorize("browser_wait", method, session, action=condition)
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Wait denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response

        # A confirmed navigation/URL change means every ref minted before
        # this wait may now point at a replaced page, exactly like an
        # explicit browser_navigate call. page.waitForURL/waitForNavigation
        # return the new url directly on success (unlike browser_act's
        # whatChanged.toUrl — a different bridge code path).
        observations_invalidated = False
        if condition in ("url", "navigation") and isinstance(result, dict):
            new_url = result.get("url")
            if isinstance(new_url, str) and new_url:
                session["url"] = new_url
            self._invalidate_session_observations(session_id)
            observations_invalidated = True

        return _ok(session_id, {
            "condition": condition,
            "matched": True,
            "result": result,
            "observationsInvalidated": observations_invalidated,
        })

    # ------------------------------------------------------------------
    # Tool 5: browser_assert
    # ------------------------------------------------------------------

    def _build_assert_call(self, assertion: str, args: Dict[str, Any], session: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        tab_id = session["tabId"]
        session_id = session["browserSessionId"]
        timeout_ms = args.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        method = ASSERT_METHOD_MAP[assertion]
        params: Dict[str, Any] = {"tabId": tab_id, "timeoutMs": timeout_ms}
        expected = args.get("expected")
        contains = args.get("contains") is True

        if assertion in LOCATOR_ASSERTIONS:
            locator = _resolve_locator_arg(session_id, args.get("locator"), "locator")
            if locator is None:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"assertion '{assertion}' requires a 'locator'.", False))
            params["locator"] = locator

        if assertion in ("visible", "hidden", "enabled", "disabled", "editable", "checked"):
            return method, params

        if assertion == "value":
            if not isinstance(expected, str):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'value' requires a string 'expected'.", False))
            params["expected"] = expected
            return method, params

        if assertion == "text":
            if not isinstance(expected, str):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'text' requires a string 'expected'.", False))
            params["expected"] = expected
            if contains:
                params["contains"] = True
            return method, params

        if assertion == "count":
            count = args.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'count' requires a non-negative integer 'count'.", False))
            params["expected"] = count
            return method, params

        if assertion == "attribute":
            attribute = args.get("attribute")
            if not isinstance(attribute, str) or not attribute:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'attribute' requires 'attribute' (the attribute name).", False))
            if not isinstance(expected, str):
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'attribute' requires a string 'expected' value.", False))
            params["attribute"] = attribute
            params["expected"] = expected
            return method, params

        if assertion == "title":
            if not isinstance(expected, str) or not expected:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'title' requires a string 'expected' title.", False))
            if contains:
                params["titleContains"] = expected
            else:
                params["title"] = expected
            return method, params

        if assertion == "aria_snapshot":
            if not isinstance(expected, str) or not expected:
                raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, "assertion 'aria_snapshot' requires a string 'expected' snapshot.", False))
            params["expected"] = expected
            return method, params

        raise _ToolArgError(_err(session_id, INVALID_ARGUMENT, f"Unknown assertion {assertion!r}.", False))

    @staticmethod
    def _reported_expected(assertion: str, cleaned: Dict[str, Any]) -> Any:
        """The expectation to echo back to the caller, taken from the
        public tool arguments rather than the internal bridge params dict:
        params never has an 'expected' key for 'title' (it uses 'title' or
        'titleContains') or 'count' (it uses the integer under 'expected'
        internally, but the argument the caller supplied is 'count')."""
        if assertion == "count":
            return cleaned.get("count")
        return cleaned.get("expected")

    @staticmethod
    def _assertion_mismatch_diagnostic(error: BrowserBridgeError) -> Dict[str, Any]:
        """Bounded, structured detail from a genuine assertion-mismatch
        error, preserved instead of discarded. _ok() redacts/bounds this
        like any other payload, so nothing unbounded or sensitive reaches
        the LLM even though the bridge's diagnostic object (actual/expected
        values, current title, missing ARIA lines, elapsedMs, ...) is
        passed through."""
        diagnostic: Dict[str, Any] = {"message": str(error)}
        data = error.data if isinstance(error.data, dict) else None
        if data:
            code = data.get("code")
            if code:
                diagnostic["code"] = code
            nested = data.get("diagnostic")
            if isinstance(nested, dict):
                diagnostic.update(nested)
        return diagnostic

    def browser_assert(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_ASSERT_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        assertion = cleaned["assertion"]
        try:
            method, params = self._build_assert_call(assertion, cleaned, session)
        except _ToolArgError as tool_error:
            return tool_error.response

        decision = self._authorize("browser_assert", method, session, action=assertion)
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Assertion denied by scope provider.", False, {"policyId": decision.policy_id})

        reported_expected = self._reported_expected(assertion, cleaned)

        try:
            self._assert_method_allowed(method)
            result = self.bridge_client.rpc(method, params)
        except BridgeMethodNotAllowedError as tool_error:
            return _err(session_id, SENSITIVE_OPERATION_BLOCKED, str(tool_error), False)
        except BrowserBridgeError as bridge_error:
            # The bridge signals both "the condition was evaluated and is
            # false" (expect.* timeout) AND unrelated failures (tab-scope
            # rejection, invalid parameters, a debugger/actionability error,
            # a transport problem) the same way: by throwing. Only the
            # former is a real assertion outcome; everything else must stay
            # ok:false so it isn't misreported as a passed=false result.
            # See _is_assertion_mismatch_error for the classification.
            if not _is_assertion_mismatch_error(bridge_error):
                return _bridge_error_response(session_id, bridge_error)
            return _ok(session_id, {
                "assertion": assertion,
                "passed": False,
                "expected": reported_expected,
                "actual": None,
                "diagnostic": self._assertion_mismatch_diagnostic(bridge_error),
            })

        return _ok(session_id, {
            "assertion": assertion,
            "passed": True,
            "expected": reported_expected,
            "actual": result if isinstance(result, dict) else None,
        })

    # ------------------------------------------------------------------
    # Tool 6: browser_capture_evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_filename(name: Any) -> Optional[str]:
        if not isinstance(name, str) or not name:
            return None
        if "/" in name or "\\" in name or ".." in name:
            return None
        return name

    def browser_capture_evidence(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_CAPTURE_EVIDENCE_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        capture = cleaned["capture"]
        save_to_disk = cleaned.get("saveToDisk", True)
        raw_filename = cleaned.get("filename")
        filename = None
        if raw_filename is not None:
            filename = self._safe_filename(raw_filename)
            if filename is None:
                return _err(session_id, INVALID_ARGUMENT, "'filename' must not contain path separators or '..'.", False)

        tab_id = session["tabId"]
        method = CAPTURE_METHOD_MAP[capture]
        params: Dict[str, Any] = {"tabId": tab_id}

        if capture == "element_screenshot":
            try:
                locator = _resolve_locator_arg(session_id, cleaned.get("locator"), "locator")
            except _ToolArgError as tool_error:
                return tool_error.response
            if locator is None:
                return _err(session_id, INVALID_ARGUMENT, "capture 'element_screenshot' requires a 'locator' (the bridge's element screenshot is locator-based).", False)
            params["locator"] = locator

        if capture in ("trace_stop", "trace_export"):
            trace_id = cleaned.get("traceId")
            if not trace_id:
                return _err(session_id, INVALID_ARGUMENT, f"capture '{capture}' requires 'traceId' (from a prior trace_start).", False)
            params["traceId"] = trace_id

        if capture == "accessibility_snapshot":
            params.update({"format": "compact", "maxNodes": DEFAULT_MAX_NODES})

        decision = self._authorize("browser_capture_evidence", method, session, action=capture)
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Evidence capture denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response
        result = result if isinstance(result, dict) else {}

        evidence_id = f"ev-{uuid.uuid4().hex[:12]}"
        data: Dict[str, Any] = {
            "evidenceId": evidence_id,
            "capture": capture,
            "url": session.get("url"),
            "capturedAt": time.time(),
        }

        data_url = result.get("dataUrl")
        if isinstance(data_url, str) and data_url:
            if save_to_disk:
                saved, error_response = self._save_data_url(session_id, data_url, filename)
                if error_response:
                    return error_response
                data["saved"] = True
                data["path"] = saved.get("path") if saved else None
                data["bytes"] = saved.get("bytes") if saved else None
                data["mimeType"] = saved.get("mimeType") if saved else None
            else:
                data["saved"] = False
                data["note"] = (
                    "saveToDisk was false; the artifact was produced but its content is not "
                    "returned to the LLM. Re-run with saveToDisk=true to persist it."
                )
        elif capture == "trace_export":
            html = result.get("html") or ""
            if save_to_disk:
                saved, error_response = self._save_text(session_id, html, filename or f"{evidence_id}.html", "text/html")
                if error_response:
                    return error_response
                data["saved"] = True
                data.update(saved)
            else:
                data["saved"] = False
                data["htmlPreview"] = html[:1000]
                data["htmlBytes"] = len(html.encode("utf-8"))
            data["traceId"] = cleaned.get("traceId")
        elif capture in ("dom_snapshot", "accessibility_snapshot"):
            if capture == "dom_snapshot":
                raw_text = json.dumps(result, ensure_ascii=False)
                mime_type, default_ext = "application/json", "json"
            else:
                raw_text = result.get("snapshot") or ""
                mime_type, default_ext = "text/plain", "txt"
            if save_to_disk:
                saved, error_response = self._save_text(session_id, raw_text, filename or f"{evidence_id}.{default_ext}", mime_type)
                if error_response:
                    return error_response
                data["saved"] = True
                data.update(saved)
            else:
                data["saved"] = False
                data["preview"] = raw_text[:2000]
                data["truncated"] = len(raw_text) > 2000
        elif capture in ("trace_start", "trace_stop"):
            data["trace"] = result.get("trace")
            data["traceId"] = (result.get("trace") or {}).get("id") if isinstance(result.get("trace"), dict) else cleaned.get("traceId")
            data["saved"] = False
        else:
            data["saved"] = False

        return _ok(session_id, data)

    # ------------------------------------------------------------------
    # Tool 7: browser_diagnose
    # ------------------------------------------------------------------

    @staticmethod
    def _bound_diagnostic_result(diagnostic: str, result: Any) -> Dict[str, Any]:
        result = result if isinstance(result, dict) else {}
        if diagnostic in ("console", "network"):
            events = result.get("events") or []
            truncated = len(events) > MAX_DIAGNOSTIC_LIMIT
            return {"events": events[:MAX_DIAGNOSTIC_LIMIT], "count": len(events), "truncated": truncated}
        if diagnostic == "response_body":
            body = result.get("body") or ""
            truncated = len(body) > MAX_DIAGNOSTIC_BODY_CHARS
            return {
                "requestId": result.get("requestId"),
                "base64Encoded": bool(result.get("base64Encoded")),
                "body": body[:MAX_DIAGNOSTIC_BODY_CHARS],
                "truncated": truncated,
            }
        if diagnostic == "dom":
            text = json.dumps(result, ensure_ascii=False)
            truncated = len(text) > MAX_TEXT_CHARS
            return {"summary": text[:MAX_TEXT_CHARS], "truncated": truncated}
        if diagnostic == "trace_status":
            return {"status": result}
        return {"result": result}

    def browser_diagnose(self, args: Any) -> Dict[str, Any]:
        cleaned, error = _validate_args(BROWSER_DIAGNOSE_PARAMS, args)
        if error:
            return _err(_safe_session_id(args), INVALID_ARGUMENT, error, False)
        session_id = cleaned["browserSessionId"]
        session, error_response = self._require_session(session_id)
        if error_response:
            return error_response

        diagnostic = cleaned["diagnostic"]
        limit = cleaned.get("limit", DEFAULT_DIAGNOSTIC_LIMIT)
        method = DIAGNOSTIC_METHOD_MAP[diagnostic]
        params: Dict[str, Any] = {"tabId": session["tabId"]}

        if diagnostic == "response_body":
            if not self.allow_response_body:
                return _err(
                    session_id, SENSITIVE_OPERATION_BLOCKED,
                    "response_body diagnostics are disabled. Construct BrowserAgentTools "
                    "with allow_response_body=True to enable them.", False,
                )
            request_id = cleaned.get("requestId")
            if not request_id:
                return _err(session_id, INVALID_ARGUMENT, "diagnostic 'response_body' requires 'requestId' (from a prior network diagnostic).", False)
            params["requestId"] = request_id
        if diagnostic in ("console", "network"):
            params["limit"] = limit
        if diagnostic == "trace_status" and cleaned.get("traceId"):
            params["traceId"] = cleaned["traceId"]

        decision = self._authorize("browser_diagnose", method, session, action=diagnostic)
        if not decision.allowed:
            return _err(session_id, SCOPE_DENIED, decision.reason or "Diagnostics denied by scope provider.", False, {"policyId": decision.policy_id})

        result, error_response = self._call_bridge(session_id, method, params)
        if error_response:
            return error_response

        bounded = self._bound_diagnostic_result(diagnostic, result)
        return _ok(session_id, {"diagnostic": diagnostic, "redactionApplied": True, **bounded})

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def dispatch(self, tool_name: str, arguments: Any) -> Dict[str, Any]:
        handlers = get_tool_handlers(self)
        handler = handlers.get(tool_name)
        if handler is None:
            return _err(
                _safe_session_id(arguments), UNKNOWN_TOOL,
                f"Unknown tool {tool_name!r}. Available tools: {', '.join(sorted(handlers))}.", False,
            )
        return handler(arguments)


# =====================================================================
# Tool registry and dispatcher
# =====================================================================

def get_tool_handlers(browser_tools: "BrowserAgentTools") -> Dict[str, Callable[[Any], Dict[str, Any]]]:
    return {
        "browser_observe": browser_tools.browser_observe,
        "browser_act": browser_tools.browser_act,
        "browser_navigate": browser_tools.browser_navigate,
        "browser_wait": browser_tools.browser_wait,
        "browser_assert": browser_tools.browser_assert,
        "browser_capture_evidence": browser_tools.browser_capture_evidence,
        "browser_diagnose": browser_tools.browser_diagnose,
    }


# =====================================================================
# Agent instructions
# =====================================================================

BROWSER_AGENT_INSTRUCTIONS = """You control one approved Chrome tab through seven semantic browser tools:
browser_observe, browser_act, browser_navigate, browser_wait, browser_assert,
browser_capture_evidence, and browser_diagnose. There is no eighth tool and no way to call a
raw bridge method, run arbitrary JavaScript, or select a different tab.

The browser is restricted to the bound Agent-managed tab group. Do not assume that arbitrary
existing Chrome tabs are accessible — only the tab bound to your browserSessionId is reachable.

Tool selection:
  Understand the current page        -> browser_observe
  Perform one interaction            -> browser_act
  Open, reload, or change history    -> browser_navigate
  Wait for an asynchronous condition -> browser_wait
  Verify an expected condition       -> browser_assert
  Create a screenshot/snapshot/PDF/trace -> browser_capture_evidence
  Investigate console/network/DOM/trace problems -> browser_diagnose

browser_observe reads the page but does not modify or verify it.
browser_act performs exactly one interaction but does not prove business success.
browser_navigate changes location/history and invalidates every previous ref.
browser_wait synchronizes with browser activity but does not prove completion — it is not an assertion.
browser_assert verifies expected conditions and is the preferred confirmation tool.
browser_capture_evidence creates artifacts and is not ordinary observation.
browser_diagnose investigates failures and is not for normal interaction.

Use browser_observe to understand the current page. It is normally the first browser tool
used on a page, and the only one that mints refs.

Always observe before an element-targeted action that needs a ref.

Use only refs from the latest observation. Never invent refs, browserSessionId values, URLs,
selectors, or file paths.

Use browser_act for exactly one semantic interaction per call — never batch multiple
interactions into one call.

Use browser_navigate only for opening URLs, reloading, or changing browser history — not for
clicking a link a user could click. Navigation invalidates observations and all previous refs.

After navigation, call browser_observe before interacting with the destination page.

Use browser_wait for meaningful asynchronous conditions. Do not use it as an arbitrary delay
when no supported condition applies.

A successful browser_wait means the wait condition occurred. It does not prove the final
requested business outcome.

Use browser_assert to confirm requested outcomes. A successful action does not by itself prove
completion — a failed assertion (passed=false) is a valid, informative result, not a tool error.

Use browser_capture_evidence only when the user requests evidence, an important state must be
preserved, an assertion failed, or diagnosis requires an artifact.

Use browser_diagnose only for failures, hangs, unexpected behavior, or an explicit diagnostic
request — never as a substitute for browser_observe.

If an observation is stale (STALE_OBSERVATION) or a ref is missing (REF_NOT_FOUND), observe
again rather than retrying the same ref.

Do not claim completion until the requested result is visible in an observation or confirmed
by browser_assert.

Never request arbitrary JavaScript execution, raw bridge methods, cookie access, extension
reload, policy changes, CSP/header/user-agent changes, or unrestricted network interception —
none of these are reachable through any of the seven tools, no matter how you phrase a request.
"""
