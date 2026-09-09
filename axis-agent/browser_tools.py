"""Minimal, typed Pydantic AI tools for Browser Agent Bridge.

The model sees safe tab/ref/artifact aliases. Browser and bridge identifiers stay
inside :class:`BrowserRuntime`, which is the only object allowed to issue RPCs.
Import and register only the individual tool functions an agent needs.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import BinaryContent, RunContext

from browser_bridge_client import BrowserBridgeClient, BrowserBridgeError


Timeout = Annotated[int, Field(ge=1, le=120_000)]
Limit = Annotated[int, Field(ge=1, le=500)]
ScrollDelta = Annotated[float, Field(ge=-5_000, le=5_000)]
NonEmpty = Annotated[str, Field(min_length=1)]

MAX_TEXT = 20_000
MAX_BODY = 4_000
MAX_ITEMS = 200
MAX_DEPTH = 8
DEFAULT_ACTION_TIMEOUT_MS = 8_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 15_000
DEFAULT_WAIT_TIMEOUT_MS = 15_000

_SENSITIVE = re.compile(
    r"authorization|cookie|password|passwd|secret|api[-_]?key|apikey|"
    r"(?:access|refresh|bearer|auth|session|csrf|xsrf)[-_]?token|"
    r"client[-_]?secret|private[-_]?key",
    re.IGNORECASE,
)
_INTERNAL_KEYS = {
    "tabId", "windowId", "groupId", "sessionId", "snapshotId", "screenshotId", "frameId", "parentFrameId", "processId",
    "targetId", "loaderId", "executionContextId", "traceId", "requestId",
}
_REF_LINE = re.compile(r"^\[f(\d+):([^\]]+)\](.*)$")
_ASSERTION_MISMATCH = {
    "LOCATOR_EXPECT_TIMEOUT", "PAGE_EXPECT_TITLE_TIMEOUT",
    "PAGE_EXPECT_ARIA_SNAPSHOT_TIMEOUT",
}
_RAW_ID_IN_MESSAGE = re.compile(
    r"\b(tab|window|group|frame|snapshot|request|trace)(?:\s*id|\s+with\s+id)\s*[:=#]?\s*[-\w]+",
    re.IGNORECASE,
)

_ALLOWED_METHODS = frozenset({
    "extension.info", "native.status", "native.sitePatterns",
    "tabs.list", "tabs.create", "tabs.activate", "tabs.close",
    "page.accessibilityTree", "page.ariaSnapshot", "page.readText", "page.frames",
    "locator.clickRef", "locator.fillRef", "locator.pressRef",
    "locator.hoverRef", "locator.selectOptionRef",
    "locator.click", "locator.fill", "locator.press", "locator.selectOption",
    "locator.check", "locator.uncheck", "locator.setInputFiles",
    "locator.dragTo", "locator.pressSequentially", "locator.focus",
    "locator.count", "locator.textContent", "locator.allTextContents",
    "locator.allInnerTexts", "locator.getAttribute", "locator.boundingBox",
    "keyboard.press", "dom.hover", "dom.scroll",
    "page.navigate", "page.reload", "page.goBack", "page.goForward",
    "locator.waitFor", "page.waitForURL", "page.waitForNavigation",
    "page.waitForLoad", "page.waitForNetworkIdle", "page.waitForText",
    "page.waitForPopup", "page.waitForDialog", "page.waitForRequest",
    "page.waitForResponse", "page.acceptDialog", "page.dismissDialog",
    "expect.locator.toBeVisible", "expect.locator.toBeHidden",
    "expect.locator.toBeEnabled", "expect.locator.toBeDisabled",
    "expect.locator.toBeEditable", "expect.locator.toBeChecked",
    "expect.locator.toHaveValue", "expect.locator.toHaveText",
    "expect.locator.toHaveCount", "expect.locator.toHaveAttribute",
    "expect.page.toHaveTitle", "expect.page.toMatchAriaSnapshot",
    "page.screenshot", "locator.screenshot", "page.domSnapshot", "page.pdf",
    "trace.start", "trace.stop", "trace.exportHtml", "trace.clear",
    "console.read", "network.read", "network.getResponseBody", "trace.status",
    "downloads.list", "downloads.waitFor",
    "recording.start", "recording.stop", "recording.status", "recording.export", "recording.clear",
    "page.visualState", "computer.click", "computer.drag", "computer.hover", "computer.scroll", "computer.type",
})
_ALLOWED_NATIVE_METHODS = frozenset({"native.saveDataUrl"})


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Locator(Command):
    """One semantic way to locate an element."""

    selector: str | None = None
    text: str | None = None
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    exact: bool = False
    visible: bool | None = None
    has_text: str | None = Field(None, serialization_alias="hasText")
    has_not_text: str | None = Field(None, serialization_alias="hasNotText")
    within: Locator | None = None
    frame_selector: str | None = Field(None, serialization_alias="frameSelector")

    @model_validator(mode="after")
    def has_strategy(self) -> Locator:
        if not any((self.selector, self.text, self.role, self.name, self.label, self.placeholder)):
            raise ValueError("A locator needs selector, text, role/name, label, or placeholder.")
        return self

    def bridge_value(self) -> dict[str, Any]:
        value = self.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
        if not any(value.get(k) for k in ("selector", "text", "role", "name", "label", "placeholder")):
            raise ToolFault("INVALID_ARGUMENT", "A locator needs selector, text, role/name, label, or placeholder.")
        return value


def _locator_params(locator: Locator) -> dict[str, Any]:
    """Keep frame routing top-level while retaining one nested locator contract."""
    value = locator.bridge_value()
    params: dict[str, Any] = {"locator": value}
    frame_selector = value.pop("frameSelector", None)
    if frame_selector:
        params["frameSelector"] = frame_selector
    return params


def _action_locator_params(locator: Locator) -> dict[str, Any]:
    """Actions should never select an earlier hidden duplicate by accident."""
    params = _locator_params(locator)
    params["locator"].setdefault("visible", True)
    return params


class RefTarget(Command):
    kind: Literal["ref"]
    ref: NonEmpty


class LocatorTarget(Command):
    kind: Literal["locator"]
    locator: Locator


Target = Annotated[RefTarget | LocatorTarget, Field(discriminator="kind")]


# browser_tabs commands
class ListTabs(Command):
    operation: Literal["list"]


class CreateTab(Command):
    operation: Literal["create"]
    url: NonEmpty = "about:blank"


class ExistingTab(Command):
    operation: Literal["activate", "close"]
    tab: NonEmpty


TabCommand = Annotated[ListTabs | CreateTab | ExistingTab, Field(discriminator="operation")]


# browser_act commands
class Click(Command):
    action: Literal["click"]
    target: Target
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Fill(Command):
    action: Literal["fill"]
    target: Target
    value: str
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Press(Command):
    action: Literal["press"]
    key: NonEmpty
    target: Target | None = None
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class TypeSequentially(Command):
    action: Literal["type_sequentially"]
    locator: Locator
    value: str
    delay_ms: Annotated[int, Field(ge=0, le=2_000)] = 25
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Hover(Command):
    action: Literal["hover"]
    target: Target
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Select(Command):
    action: Literal["select"]
    target: Target
    options: Annotated[list[NonEmpty], Field(min_length=1)]
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Check(Command):
    action: Literal["check", "uncheck"]
    locator: Locator
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Upload(Command):
    action: Literal["upload"]
    locator: Locator
    files: Annotated[list[NonEmpty], Field(min_length=1)]
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Drag(Command):
    action: Literal["drag"]
    source: Locator
    destination: Locator
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class Scroll(Command):
    action: Literal["scroll"]
    delta_x: ScrollDelta = 0
    delta_y: ScrollDelta = 0
    selector: str | None = None


class Focus(Command):
    action: Literal["focus"]
    locator: Locator
    index: Annotated[int, Field(ge=0)] = 0


class DialogAction(Command):
    action: Literal["accept_dialog", "dismiss_dialog"]
    prompt_text: str | None = None


ActCommand = Annotated[
    Click | Fill | Press | TypeSequentially | Hover | Select | Check | Upload | Drag | Scroll | Focus | DialogAction,
    Field(discriminator="action"),
]


# browser_navigate commands
class Open(Command):
    operation: Literal["open"]
    url: NonEmpty
    timeout_ms: Timeout = DEFAULT_NAVIGATION_TIMEOUT_MS


class History(Command):
    operation: Literal["reload", "back", "forward"]
    timeout_ms: Timeout = DEFAULT_NAVIGATION_TIMEOUT_MS


NavigateCommand = Annotated[Open | History, Field(discriminator="operation")]


# browser_wait commands
class ElementWait(Command):
    condition: Literal["element_state"]
    locator: Locator
    state: Literal["attached", "visible", "hidden", "detached"] = "visible"
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS


class ValueWait(Command):
    condition: Literal["url", "text"]
    expected: NonEmpty
    match: Literal["contains", "exact", "regex"] = "contains"
    selector: str | None = None
    frame_selector: str | None = None
    case_sensitive: bool = False
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS

    @model_validator(mode="after")
    def valid_match(self) -> ValueWait:
        if self.condition == "text" and self.match == "regex":
            raise ValueError("Text waits support contains or exact; regex matching is URL-only.")
        return self


class RequestWait(Command):
    condition: Literal["request"]
    url: str | None = None
    url_contains: str | None = None
    url_regex: str | None = None
    http_method: str | None = None
    resource_type: str | None = None
    header_contains: dict[str, str] | None = None
    header_regex: dict[str, str] | None = None
    post_data_contains: str | None = None
    post_data_regex: str | None = None
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS

    @model_validator(mode="after")
    def has_filter(self) -> RequestWait:
        if not any((self.url, self.url_contains, self.url_regex, self.http_method, self.resource_type,
                    self.header_contains, self.header_regex, self.post_data_contains, self.post_data_regex)):
            raise ValueError("A request wait needs at least one request filter.")
        return self


class ResponseWait(Command):
    condition: Literal["response"]
    url: str | None = None
    url_contains: str | None = None
    url_regex: str | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    http_method: str | None = None
    resource_type: str | None = None
    mime_type: str | None = None
    header_contains: dict[str, str] | None = None
    header_regex: dict[str, str] | None = None
    min_size: Annotated[int, Field(ge=0)] | None = None
    max_size: Annotated[int, Field(ge=0)] | None = None
    body_contains: str | None = None
    body_regex: str | None = None
    json_path: str | None = None
    json_contains: str | None = None
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS

    @model_validator(mode="after")
    def has_filter(self) -> ResponseWait:
        fields = (
            self.url, self.url_contains, self.url_regex, self.status, self.http_method, self.resource_type,
            self.mime_type, self.header_contains, self.header_regex, self.min_size, self.max_size,
            self.body_contains, self.body_regex, self.json_path, self.json_contains,
        )
        if not any(value is not None and value != {} for value in fields):
            raise ValueError("A response wait needs at least one response filter.")
        return self


class EventWait(Command):
    condition: Literal["navigation", "page_load", "network_idle", "popup", "dialog"]
    url: str | None = None
    url_contains: str | None = None
    url_regex: str | None = None
    dialog_type: Literal["alert", "confirm", "prompt", "beforeunload"] | None = None
    message_contains: str | None = None
    idle_ms: Annotated[int, Field(ge=1, le=30_000)] = 500
    max_inflight: Annotated[int, Field(ge=0, le=100)] = 0
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS


WaitCommand = Annotated[ElementWait | ValueWait | RequestWait | ResponseWait | EventWait, Field(discriminator="condition")]


# browser_assert commands
class StateAssertion(Command):
    assertion: Literal["visible", "hidden", "enabled", "disabled", "editable", "checked"]
    locator: Locator
    expected: bool = True
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class ValueAssertion(Command):
    assertion: Literal["value"]
    locator: Locator
    expected: str
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class TextAssertion(Command):
    assertion: Literal["text"]
    locator: Locator
    expected: str
    contains: bool = False
    regex: bool = False
    case_sensitive: bool = False
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class CountAssertion(Command):
    assertion: Literal["count"]
    locator: Locator
    count: Annotated[int, Field(ge=0)]
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class AttributeAssertion(Command):
    assertion: Literal["attribute"]
    locator: Locator
    attribute: NonEmpty
    expected: str
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class TitleAssertion(Command):
    assertion: Literal["title"]
    expected: NonEmpty
    contains: bool = False
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class AriaAssertion(Command):
    assertion: Literal["aria_snapshot"]
    expected: NonEmpty
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


class URLAssertion(Command):
    assertion: Literal["url"]
    expected: NonEmpty
    timeout_ms: Timeout = DEFAULT_ACTION_TIMEOUT_MS


AssertCommand = Annotated[
    StateAssertion | ValueAssertion | TextAssertion | CountAssertion |
    AttributeAssertion | TitleAssertion | AriaAssertion | URLAssertion,
    Field(discriminator="assertion"),
]


# browser_capture_evidence commands
class PageCapture(Command):
    capture: Literal["page_screenshot", "dom_snapshot", "accessibility_snapshot", "pdf"]
    filename: str | None = None
    save: bool = True


class ElementCapture(Command):
    capture: Literal["element_screenshot"]
    locator: Locator
    filename: str | None = None
    save: bool = True


class TraceStart(Command):
    capture: Literal["trace_start"]


class TraceStop(Command):
    capture: Literal["trace_stop"]
    trace: NonEmpty


class TraceExport(Command):
    capture: Literal["trace_export"]
    trace: NonEmpty
    filename: str | None = None
    save: bool = True


CaptureCommand = Annotated[
    PageCapture | ElementCapture | TraceStart | TraceStop | TraceExport,
    Field(discriminator="capture"),
]


# browser_diagnose commands
class BufferedDiagnostic(Command):
    diagnostic: Literal["console", "network"]
    limit: Limit = 100


class ResponseBodyDiagnostic(Command):
    diagnostic: Literal["response_body"]
    request: NonEmpty


class SimpleDiagnostic(Command):
    diagnostic: Literal["dom"]


class TraceDiagnostic(Command):
    diagnostic: Literal["trace_status"]
    trace: str | None = None


DiagnoseCommand = Annotated[
    BufferedDiagnostic | ResponseBodyDiagnostic | SimpleDiagnostic | TraceDiagnostic,
    Field(discriminator="diagnostic"),
]


# browser_downloads commands (optional tool; hidden unless the task needs files)
class ListDownloads(Command):
    operation: Literal["list"]
    query: str | None = None
    filename_regex: str | None = None
    url_regex: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class WaitForDownload(Command):
    operation: Literal["wait"]
    state: Literal["any", "in_progress", "complete", "interrupted"] = "complete"
    query: str | None = None
    filename: str | None = None
    filename_contains: str | None = None
    filename_regex: str | None = None
    url: str | None = None
    url_contains: str | None = None
    url_regex: str | None = None
    include_existing: bool = False
    timeout_ms: Timeout = DEFAULT_WAIT_TIMEOUT_MS


DownloadCommand = Annotated[ListDownloads | WaitForDownload, Field(discriminator="operation")]


# browser_visual is disclosed only when DOM targeting needs visual recovery.
ImageCoordinate = Annotated[float, Field(ge=0, le=1_600, allow_inf_nan=False)]


class VisualCapture(Command):
    operation: Literal["capture"]


class VisualPoint(Command):
    operation: Literal["click", "hover"]
    screenshot: NonEmpty
    x: ImageCoordinate
    y: ImageCoordinate


class VisualDrag(Command):
    operation: Literal["drag"]
    screenshot: NonEmpty
    x: ImageCoordinate
    y: ImageCoordinate
    end_x: ImageCoordinate
    end_y: ImageCoordinate


class VisualScroll(Command):
    operation: Literal["scroll"]
    screenshot: NonEmpty
    x: ImageCoordinate
    y: ImageCoordinate
    delta_x: ScrollDelta = 0
    delta_y: ScrollDelta = 0


class VisualType(Command):
    operation: Literal["type"]
    screenshot: NonEmpty
    value: str


VisualCommand = Annotated[
    VisualCapture | VisualPoint | VisualDrag | VisualScroll | VisualType,
    Field(discriminator="operation"),
]


@dataclass
class TabState:
    raw_id: int
    url: str | None = None
    title: str | None = None
    active: bool = False
    closed: bool = False


@dataclass
class Observation:
    snapshot: str | None
    refs: dict[str, tuple[int, str]] = field(default_factory=dict)


@dataclass(repr=False)
class _VisualScreenshot:
    alias: str
    tab: str
    bridge_id: str
    state: dict[str, Any]
    image: dict[str, int]
    content: BinaryContent


class ToolFault(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail


def _public(value: Any, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return "<max depth exceeded>"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            if key in _INTERNAL_KEYS:
                continue
            clean[key] = "<redacted>" if _SENSITIVE.search(str(key)) else _public(child, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        items = [_public(v, depth + 1) for v in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"... [{len(value) - MAX_ITEMS} more items truncated]")
        return items
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"... [truncated {len(value) - MAX_TEXT} characters]"
    return value


def _ok(tab: str | None, data: Any) -> dict[str, Any]:
    return {"ok": True, "tab": tab, "data": _public(data), "error": None}


def _error(tab: str | None, fault: ToolFault) -> dict[str, Any]:
    return {
        "ok": False,
        "tab": tab,
        "data": None,
        "error": _public({
            "code": fault.code,
            "message": str(fault),
            "retryable": fault.retryable,
            "detail": _without_bridge_element_refs(fault.detail),
        }),
    }


def _without_trace_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_trace_ids(child) for key, child in value.items() if key not in {"id", "traceId"}}
    if isinstance(value, list):
        return [_without_trace_ids(child) for child in value]
    return value


def _without_bridge_element_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_bridge_element_refs(child)
            for key, child in value.items()
            if key not in _INTERNAL_KEYS and key not in {"id", "ref"}
        }
    if isinstance(value, list):
        return [_without_bridge_element_refs(child) for child in value]
    return value


def _safe_bridge_message(message: str) -> str:
    """Remove bridge identity values while retaining a useful error reason."""
    return _RAW_ID_IN_MESSAGE.sub(lambda match: f"{match.group(1).lower()} identifier", message)


def _public_action_change(rt: BrowserRuntime, changed: Any) -> tuple[Any, list[str]]:
    if not isinstance(changed, dict):
        return changed, []
    public = _without_bridge_element_refs(changed)
    aliases: list[str] = []
    raw_popups = changed.get("newPopups", [])
    if isinstance(raw_popups, list):
        popups: list[dict[str, Any]] = []
        for raw in raw_popups:
            if not isinstance(raw, dict):
                continue
            try:
                alias = rt._register({**raw, "id": raw.get("tabId", raw.get("id"))})
            except ToolFault:
                continue
            aliases.append(alias)
            popups.append(rt.public_tab(alias))
        if popups:
            public["newPopups"] = popups
    return public, aliases


def _snapshot_without_bridge_refs(snapshot: str) -> str:
    lines: list[str] = []
    for line in snapshot.splitlines():
        match = _REF_LINE.match(line)
        lines.append(f"[element]{match.group(3)}" if match else line)
    return "\n".join(lines)


class BrowserRuntime:
    """Trusted state and policy boundary around a BrowserBridgeClient."""

    def __init__(
        self,
        client: BrowserBridgeClient,
        *,
        max_tabs: int = 30,
        allow_response_body: bool = False,
        upload_roots: tuple[str | Path, ...] = (),
        artifact_directory: str | None = None,
        firewall_default: Literal["allow", "deny"] = "allow",
        allow_urls: tuple[str, ...] = ("*", "chrome://newtab/*", "chrome://new-tab-page/*"),
        deny_urls: tuple[str, ...] = (),
        deny_schemes: tuple[str, ...] = ("chrome", "chrome-extension", "devtools", "javascript"),
        allow_methods: tuple[str, ...] = ("*",),
        deny_methods: tuple[str, ...] = (),
        allow_coordinate_fallback: bool = False,
    ):
        self.client = client
        self.max_tabs = max_tabs
        self.allow_response_body = allow_response_body
        self.upload_roots = tuple(Path(p).resolve() for p in upload_roots)
        self.artifact_directory = artifact_directory
        self.firewall_default = firewall_default
        self.allow_urls = allow_urls
        self.deny_urls = deny_urls
        self.deny_schemes = tuple(s.lower() for s in deny_schemes)
        self.allow_methods = allow_methods
        self.deny_methods = deny_methods
        self.allow_coordinate_fallback = allow_coordinate_fallback
        self.visual_enabled = False
        self.task_started_wallclock = time.time()
        self.tabs: dict[str, TabState] = {}
        self.observations: dict[str, Observation] = {}
        self.issued_refs: dict[str, str] = {}
        self.ref_descriptions: dict[str, str] = {}
        self.traces: dict[str, str] = {}
        self.requests: dict[str, str] = {}
        self.recordings: dict[str, str] = {}
        self.advertised_methods: frozenset[str] | None = None
        self.capability_warning: str | None = None
        self.site_patterns: dict[str, dict[str, Any]] = {}
        self._capabilities_checked = False
        self._ui_failures: dict[str, int] = {}
        self._tab_number = self._ref_number = self._trace_number = self._request_number = 0
        self._recording_number = 0
        self._evidence_number = 0
        self.debug_recording: str | None = None
        self.debug_trace: str | None = None
        self._visual_screenshot: _VisualScreenshot | None = None
        self._visual_number = 0
        self._observation_text: dict[str, str] = {}

    def invalidate_visual(self, tab: str | None = None) -> None:
        """Discard private pixels and the one-use coordinate lease."""
        if self._visual_screenshot and (tab is None or self._visual_screenshot.tab == tab):
            self._visual_screenshot = None

    def visual_content(self, screenshot: str) -> BinaryContent:
        """Return pixels only to trusted model-message assembly, never tool JSON."""
        current = self._visual_screenshot
        if not self.visual_enabled or current is None or current.alias != screenshot:
            raise ToolFault("STALE_SCREENSHOT", "Capture a fresh screenshot before using visual input.", True)
        return current.content

    @staticmethod
    def _normalized_observation(snapshot: str) -> str:
        return " ".join(re.sub(r"\[(?:ref_\d+|f\d+:[^\]]+)\]", "[element]", snapshot).split())

    @staticmethod
    def _matches(value: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(value.lower(), pattern.lower()) for pattern in patterns if pattern)

    def _method_allowed(self, method: str) -> bool:
        return (
            method in _ALLOWED_METHODS
            and not self._matches(method, self.deny_methods)
            and (self._matches(method, self.allow_methods) or self.firewall_default == "allow")
        )

    def _url_allowed(self, url: str | None) -> bool:
        if not url:
            return True
        if self._matches(url, self.deny_urls):
            return False
        scheme_match = re.match(r"^([a-z][a-z0-9+.-]*):", url, re.IGNORECASE)
        scheme = scheme_match.group(1).lower() if scheme_match else ""
        explicit_scheme_allow = any(
            pattern != "*" and pattern.lower().startswith(f"{scheme}:")
            and self._matches(url, (pattern,))
            for pattern in self.allow_urls
        )
        if scheme in self.deny_schemes and not explicit_scheme_allow:
            return False
        if self._matches(url, self.allow_urls):
            return True
        return self.firewall_default == "allow"

    def _rpc(self, method: str, params: dict[str, Any], *, tab: str | None = None, url: str | None = None) -> Any:
        if not self._method_allowed(method):
            raise ToolFault("FIREWALL_DENIED", "This browser operation is not allowed.")
        current_url = self.tabs[tab].url if tab in self.tabs else None
        if not self._url_allowed(url or current_url):
            raise ToolFault("FIREWALL_DENIED", "This URL is blocked by browser policy.")
        if not self._capabilities_checked and method != "extension.info":
            self._negotiate_capabilities()
        if self.advertised_methods is not None and method not in self.advertised_methods and not method.startswith("native."):
            raise ToolFault(
                "CAPABILITY_UNAVAILABLE",
                f"The connected bridge does not advertise {method}; reload or upgrade the extension.",
            )
        try:
            return self.client.rpc(method, params)
        except BrowserBridgeError as exc:
            data = exc.data if isinstance(exc.data, dict) else {}
            bridge_code = data.get("code")
            raw_text = str(exc)
            if tab and re.search(r"no tab with id|tab (?:was )?not found|invalid tab id", raw_text, re.IGNORECASE):
                state = self.tabs.get(tab)
                if state is not None:
                    state.closed = True
                self.invalidate(tab)
                raise ToolFault(
                    "TAB_NOT_FOUND",
                    f"{tab} is no longer open; refresh the tab list and choose a live tab.",
                    True,
                ) from exc
            text = _safe_bridge_message(raw_text)
            # These are the bridge's explicit pre-dispatch policy messages.
            # Preserve denials as denials: classifying them as an unknown input
            # outcome could incorrectly activate coordinate recovery.
            if re.match(r"^Permission denied by user(?: for this session)?:", raw_text):
                raise ToolFault("APPROVAL_DENIED", text, False, data) from exc
            if (raw_text.startswith("Access denied:") or raw_text.startswith("Method blocked by policy:")
                    or raw_text.startswith(f"{method} blocked by policy for ")):
                raise ToolFault("FIREWALL_DENIED", text, False, data) from exc
            if bridge_code in _ASSERTION_MISMATCH or re.search(
                r"timed out waiting for locator .* to be (attached|visible|hidden|detached)", text, re.IGNORECASE,
            ):
                raise ToolFault("ASSERTION_FAILED", text, detail=data) from exc
            if bridge_code in {"STALE_ACCESSIBILITY_SNAPSHOT", "REF_NOT_FOUND", "LOCATOR_REF_NOT_FOUND"} or re.search(
                r"stale.*(snapshot|ref)|ref.*not found", text, re.IGNORECASE,
            ):
                if tab:
                    self.invalidate(tab)
                raise ToolFault("STALE_REFERENCE", "The page changed; observe it again.", True) from exc
            if isinstance(bridge_code, str) and bridge_code:
                retryable = bridge_code.endswith("_TIMEOUT") or bridge_code in {
                    "LOCATOR_ACTIONABILITY_TIMEOUT", "PAGE_WAIT_FOR_POPUP_TIMEOUT",
                    "PAGE_WAIT_FOR_DIALOG_TIMEOUT", "PAGE_WAIT_FOR_REQUEST_TIMEOUT",
                    "PAGE_WAIT_FOR_RESPONSE_TIMEOUT",
                }
                raise ToolFault(bridge_code, text, retryable, data) from exc
            if re.search(r"timed?\s*out|timeout", text, re.IGNORECASE):
                raise ToolFault("TIMEOUT", text, True, data) from exc
            if re.search(r"connection|refused|unreachable|temporarily unavailable|HTTP 5\d\d", text, re.IGNORECASE):
                raise ToolFault("BRIDGE_UNAVAILABLE", text, True, data) from exc
            raise ToolFault("BRIDGE_ERROR", text, False, data) from exc

    def _negotiate_capabilities(self) -> None:
        """Best-effort one-time handshake; older/fake bridges remain compatible."""
        self._capabilities_checked = True
        try:
            info = self.client.rpc("extension.info", {}) or {}
            methods = info.get("methods", info.get("tools")) if isinstance(info, dict) else None
            if isinstance(methods, list) and methods:
                self.advertised_methods = frozenset(item for item in methods if isinstance(item, str))
            patterns = self.client.rpc("native.sitePatterns", {}) or {}
            for item in patterns.get("patterns", []) if isinstance(patterns, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("domain"), str):
                    self.site_patterns[item["domain"].lower()] = item
        except BrowserBridgeError as exc:
            self.capability_warning = str(exc)

    def site_pattern_for(self, url: str | None) -> dict[str, Any] | None:
        host = (urlsplit(url).hostname or "").lower() if url else ""
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        while host:
            if host in self.site_patterns:
                return self.site_patterns[host]
            host = host.partition(".")[2]
        return None

    def tab(self, alias: str) -> TabState:
        state = self.tabs.get(alias)
        if state is None:
            raise ToolFault("TAB_NOT_FOUND", f"Unknown tab alias {alias!r}; list tabs again.")
        if state.closed:
            raise ToolFault("TAB_CLOSED", f"Tab {alias!r} is closed.")
        return state

    def _register(self, raw: dict[str, Any]) -> str:
        raw_id = raw.get("id")
        if not isinstance(raw_id, int):
            raise ToolFault("BRIDGE_ERROR", "The bridge returned an invalid tab.")
        alias = next((a for a, t in self.tabs.items() if t.raw_id == raw_id and not t.closed), None)
        if alias is None:
            if sum(not t.closed for t in self.tabs.values()) >= self.max_tabs:
                raise ToolFault("TAB_LIMIT", "The browser tab limit has been reached.")
            self._tab_number += 1
            alias = f"tab_{self._tab_number}"
        self.tabs[alias] = TabState(raw_id, raw.get("url"), raw.get("title"), bool(raw.get("active")))
        return alias

    def public_tab(self, alias: str) -> dict[str, Any]:
        state = self.tabs[alias]
        return {"tab": alias, "title": state.title, "url": self.safe_url(state.url), "active": state.active}

    @staticmethod
    def safe_url(url: str | None) -> str | None:
        if not url or "?" not in url:
            return url
        base, query = url.split("?", 1)
        kept = [part for part in query.split("&") if not _SENSITIVE.search(part.split("=", 1)[0])]
        return base if not kept else f"{base}?{'&'.join(kept)}"

    def invalidate(self, tab: str) -> None:
        self.observations.pop(tab, None)
        self.invalidate_visual(tab)

    def observe_refs(self, tab: str, snapshot: str, snapshot_id: str | None) -> tuple[str, bool]:
        refs: dict[str, tuple[int, str]] = {}
        lines: list[str] = []
        for line in snapshot.splitlines():
            match = _REF_LINE.match(line)
            if not match:
                lines.append(line)
                continue
            self._ref_number += 1
            alias = f"ref_{self._ref_number}"
            refs[alias] = (int(match.group(1)), match.group(2))
            self.issued_refs[alias] = tab
            self.ref_descriptions[alias] = match.group(3).strip()[:500]
            lines.append(f"[{alias}]{match.group(3)}")
        if refs and not snapshot_id:
            raise ToolFault("BRIDGE_ERROR", "The bridge returned refs without a usable snapshot.")
        public_snapshot = "\n".join(lines)
        truncated = len(public_snapshot) > MAX_TEXT
        self.observations[tab] = Observation(snapshot_id, refs)
        return public_snapshot[:MAX_TEXT], truncated

    def observe(
        self,
        tab: str,
        mode: Literal["compact", "aria", "text"] = "compact",
        max_nodes: int = 1_000,
    ) -> dict[str, Any]:
        """The single observation implementation.

        ``browser_observe`` is the model-facing wrapper around this; an
        orchestrator that needs a fresh page view *between* model turns calls
        it directly instead of asking the model to observe. Raises
        :class:`ToolFault`; the tool wrapper is what turns that into an
        error envelope.
        """
        state = self.tab(tab)
        methods = {"compact": "page.accessibilityTree", "aria": "page.ariaSnapshot", "text": "page.readText"}
        params: dict[str, Any] = {"tabId": state.raw_id}
        if mode == "compact":
            params.update(format="compact", maxNodes=max_nodes)
        result = self._rpc(methods[mode], params, tab=tab) or {}
        self.observations.pop(tab, None)
        if mode == "compact":
            snapshot, truncated = self.observe_refs(tab, result.get("snapshot", ""), result.get("snapshotId"))
        else:
            raw = result.get("snapshot", "") if mode == "aria" else result.get("text", "")
            snapshot, truncated = raw[:MAX_TEXT], len(raw) > MAX_TEXT
        state.url = result.get("url", state.url)
        state.title = result.get("title", state.title)
        data: dict[str, Any] = {
            "url": self.safe_url(state.url), "title": state.title,
            "snapshot": snapshot, "truncated": bool(result.get("truncated")) or truncated,
        }
        scroll = result.get("scroll") if isinstance(result.get("scroll"), dict) else result
        x, y = scroll.get("x", scroll.get("scrollX")), scroll.get("y", scroll.get("scrollY"))
        if isinstance(x, (int, float)) or isinstance(y, (int, float)):
            data["scroll"] = {"x": x, "y": y}
        normalized = self._normalized_observation(snapshot)
        visual = self._visual_screenshot
        if visual is not None and visual.tab == tab:
            previous = self._observation_text.get(tab)
            if (previous is None or previous != normalized or visual.state["url"] != state.url
                    or ("scroll" in data and data["scroll"] != visual.state["scroll"])):
                self.invalidate_visual(tab)
        self._observation_text[tab] = normalized
        pattern = self.site_pattern_for(state.url)
        if pattern:
            content = pattern.get("content", "")
            data["sitePattern"] = {
                "domain": pattern.get("domain"),
                "summary": pattern.get("summary"),
                "notes": content[:4_000] if isinstance(content, str) else None,
            }
        if self.capability_warning:
            data["runtimeWarning"] = "Bridge capability negotiation was unavailable; compatibility mode is active."
        return data

    def resolve_ref(self, tab: str, ref: str) -> tuple[int, str, str]:
        observation = self.observations.get(tab)
        if observation and ref in observation.refs and observation.snapshot:
            frame, bridge_ref = observation.refs[ref]
            return frame, bridge_ref, observation.snapshot
        if ref in self.issued_refs:
            raise ToolFault("STALE_REFERENCE", "That ref is stale; observe the tab again.", True)
        raise ToolFault("REF_NOT_FOUND", f"Unknown ref {ref!r}; use a ref from browser_observe.")

    def describe_ref(self, ref: str) -> str | None:
        return self.ref_descriptions.get(ref)

    def reconcile_tabs(self) -> list[str]:
        """Refresh cached aliases from the bridge and return live aliases."""
        result = browser_tabs(type("Context", (), {"deps": self})(), ListTabs(operation="list"))
        if not result["ok"]:
            error = result.get("error") or {}
            raise ToolFault(
                error.get("code") or "BRIDGE_ERROR",
                error.get("message") or "The bridge failed to list tabs.",
                bool(error.get("retryable")),
            )
        return [item["tab"] for item in result["data"]["tabs"]]

    def ref_params(self, tab: str, ref: str, timeout: int) -> dict[str, Any]:
        state = self.tab(tab)
        frame, bridge_ref, snapshot = self.resolve_ref(tab, ref)
        return {"tabId": state.raw_id, "frameId": frame, "ref": bridge_ref, "snapshotId": snapshot, "timeoutMs": timeout}

    def validated_files(self, files: list[str]) -> list[str]:
        if not self.upload_roots:
            raise ToolFault("UPLOAD_DENIED", "No upload directory is configured.")
        approved: list[str] = []
        for item in files:
            path = Path(item).resolve()
            if not path.is_file() or not any(path == root or root in path.parents for root in self.upload_roots):
                raise ToolFault("UPLOAD_DENIED", "An upload path is missing or outside the configured roots.")
            approved.append(str(path))
        return approved

    @staticmethod
    def safe_filename(filename: str | None) -> str | None:
        if filename is None:
            return None
        if not filename or Path(filename).name != filename or ".." in filename:
            raise ToolFault("INVALID_ARGUMENT", "filename must be a plain safe filename.")
        return filename

    def save(self, data_url: str, filename: str | None) -> dict[str, Any]:
        if "native.saveDataUrl" not in _ALLOWED_NATIVE_METHODS:
            raise ToolFault("FIREWALL_DENIED", "Artifact saving is not allowed.")
        try:
            return self.client.save_data_url(data_url, self.safe_filename(filename), self.artifact_directory)
        except BrowserBridgeError as exc:
            raise ToolFault("ARTIFACT_ERROR", str(exc), False, exc.data) from exc

    def new_trace(self, bridge_trace: str) -> str:
        self._trace_number += 1
        alias = f"trace_{self._trace_number}"
        self.traces[alias] = bridge_trace
        return alias

    def trace(self, alias: str) -> str:
        try:
            return self.traces[alias]
        except KeyError as exc:
            raise ToolFault("TRACE_NOT_FOUND", f"Unknown trace alias {alias!r}.") from exc

    def new_recording(self, bridge_recording: str) -> str:
        self._recording_number += 1
        alias = f"recording_{self._recording_number}"
        self.recordings[alias] = bridge_recording
        return alias

    def recording(self, alias: str) -> str:
        try:
            return self.recordings[alias]
        except KeyError as exc:
            raise ToolFault("RECORDING_NOT_FOUND", f"Unknown recording alias {alias!r}.") from exc

    def note_ui_success(self, tab: str) -> None:
        self._ui_failures.pop(tab, None)

    def start_debug_telemetry(self, tab: str) -> None:
        """Start bounded, text-redacted bridge telemetry once for this run."""
        state = self.tab(tab)
        if self.debug_recording is None:
            try:
                result = self._rpc("recording.start", {
                    "tabId": state.raw_id, "name": "AXIS debug", "captureScreenshots": False,
                    "includeText": False, "retentionMs": 86_400_000, "maxActions": 250,
                }, tab=tab) or {}
                raw = result.get("recording", {}).get("id")
                if isinstance(raw, str):
                    self.debug_recording = self.new_recording(raw)
            except ToolFault as fault:
                self.capability_warning = f"Debug recording unavailable: {fault.code}"
        if self.debug_trace is None:
            try:
                result = self._rpc("trace.start", {
                    "name": "AXIS debug", "includeText": False, "includeParams": True,
                    "includeResults": True, "includeContext": True,
                    "retentionMs": 86_400_000, "maxEvents": 500,
                }, tab=tab) or {}
                raw = result.get("trace", {}).get("id")
                if isinstance(raw, str):
                    self.debug_trace = self.new_trace(raw)
            except ToolFault as fault:
                self.capability_warning = f"Debug trace unavailable: {fault.code}"

    def stop_debug_telemetry(self) -> dict[str, str]:
        stopped: dict[str, str] = {}
        if self.debug_recording:
            alias = self.debug_recording
            try:
                self._rpc("recording.stop", {"recordingId": self.recording(alias)})
                stopped["recording"] = alias
            except ToolFault:
                pass
            self.debug_recording = None
        if self.debug_trace:
            alias = self.debug_trace
            try:
                self._rpc("trace.stop", {"traceId": self.trace(alias)})
                stopped["trace"] = alias
            except ToolFault:
                pass
            self.debug_trace = None
        return stopped

    def enrich_ui_failure(self, tab: str, fault: ToolFault, locator: Locator | None) -> ToolFault:
        """After repeated UI failures, attach cheap targeted bridge diagnostics."""
        count = self._ui_failures.get(tab, 0) + 1
        self._ui_failures[tab] = count
        if count < 2:
            return fault
        detail = dict(fault.detail) if isinstance(fault.detail, dict) else {"bridgeDetail": fault.detail}
        detail["consecutiveUiFailures"] = count
        state = self.tabs.get(tab)
        if state is None:
            fault.detail = detail
            return fault
        try:
            frames = self._rpc("page.frames", {"tabId": state.raw_id}, tab=tab) or {}
            detail["frames"] = frames.get("frames", [])[:20] if isinstance(frames, dict) else frames
        except ToolFault as diagnostic_fault:
            detail["framesDiagnostic"] = diagnostic_fault.code
        if locator is not None:
            try:
                count_result = self._rpc(
                    "locator.count", {"tabId": state.raw_id, **_locator_params(locator)}, tab=tab,
                ) or {}
                detail["locatorDiagnostic"] = count_result
            except ToolFault as diagnostic_fault:
                detail["locatorDiagnostic"] = {"error": diagnostic_fault.code}
        fault.detail = detail
        return fault

    def public_requests(self, events: list[Any]) -> list[Any]:
        public: list[Any] = []
        grouped: dict[str, dict[str, Any]] = {}
        for raw in events:
            if not isinstance(raw, dict):
                public.append(raw)
                continue
            event = dict(raw)
            # CDP emits several deeply nested events per request. Keep the
            # useful request outcome together, without verbose headers/stacks.
            nested = isinstance(raw.get("params"), dict) and str(raw.get("method", "")).startswith("Network.")
            if nested:
                params = raw["params"]
                event = {key: params[key] for key in
                         ("requestId", "errorText", "canceled", "blockedReason", "encodedDataLength") if key in params}
                for part in (params.get("request", {}), params.get("response", {})):
                    if isinstance(part, dict):
                        event.update({key: part[key] for key in
                                      ("url", "method", "status", "statusText", "mimeType", "fromDiskCache") if key in part})
                if "statusCode" in params:
                    event["status"] = params["statusCode"]
                if isinstance(event.get("url"), str):
                    event["url"] = self.safe_url(event["url"])
            bridge_id = event.pop("requestId", None)
            if isinstance(bridge_id, str):
                alias = next((a for a, value in self.requests.items() if value == bridge_id), None)
                if alias is None:
                    self._request_number += 1
                    alias = f"request_{self._request_number}"
                    self.requests[alias] = bridge_id
                    if len(self.requests) > MAX_ITEMS:
                        del self.requests[next(iter(self.requests))]
                event["request"] = alias
                if nested:
                    if bridge_id in grouped:
                        grouped[bridge_id].update(event)
                        continue
                    grouped[bridge_id] = event
            if nested and not bridge_id:
                continue
            public.append(event)
        return public

    def request(self, alias: str) -> str:
        try:
            return self.requests[alias]
        except KeyError as exc:
            raise ToolFault("REQUEST_NOT_FOUND", f"Unknown request alias {alias!r}.") from exc

    def evidence(self) -> str:
        self._evidence_number += 1
        return f"evidence_{self._evidence_number}"


def browser_tabs(ctx: RunContext[BrowserRuntime], command: TabCommand) -> dict[str, Any]:
    """List browser tabs or create, activate, or close one using safe tab aliases."""
    rt = ctx.deps
    try:
        if isinstance(command, ListTabs):
            result = rt._rpc("tabs.list", {"query": {}}) or {}
            raw_tabs = [t for t in result.get("tabs", []) if isinstance(t, dict) and isinstance(t.get("id"), int)]
            live_ids = {t["id"] for t in raw_tabs}
            for alias, state in rt.tabs.items():
                if state.raw_id not in live_ids:
                    state.closed = True
                    rt.invalidate(alias)
            aliases: list[str] = []
            for raw in raw_tabs:
                try:
                    aliases.append(rt._register(raw))
                except ToolFault as fault:
                    if fault.code != "TAB_LIMIT":
                        raise
            return _ok(None, {"tabs": [rt.public_tab(alias) for alias in aliases]})
        if isinstance(command, CreateTab):
            result = rt._rpc("tabs.create", {"url": command.url, "active": True}, url=command.url) or {}
            raw = result.get("tab") if isinstance(result.get("tab"), dict) else result
            alias = rt._register(raw)
            return _ok(alias, rt.public_tab(alias))
        state = rt.tab(command.tab)
        if command.operation == "activate":
            rt._rpc("tabs.activate", {"tabId": state.raw_id}, tab=command.tab)
            for alias, other in rt.tabs.items():
                other.active = alias == command.tab
            return _ok(command.tab, {**rt.public_tab(command.tab), "activated": True})
        rt._rpc("tabs.close", {"tabId": state.raw_id}, tab=command.tab)
        state.closed = True
        rt.invalidate(command.tab)
        return _ok(command.tab, {"tab": command.tab, "closed": True})
    except ToolFault as fault:
        return _error(getattr(command, "tab", None), fault)


def browser_observe(
    ctx: RunContext[BrowserRuntime],
    tab: NonEmpty,
    mode: Literal["compact", "aria", "text", "frames"] = "compact",
    max_nodes: Annotated[int, Field(ge=1, le=2_000)] = 1_000,
    locator: Locator | None = None,
    extract: Literal["count", "text", "all_text", "all_inner_text", "attribute", "bounding_box"] | None = None,
    index: Annotated[int, Field(ge=0)] = 0,
    attribute: str | None = None,
) -> dict[str, Any]:
    """Inspect a page, its frames, or one locator; compact page mode returns short-lived refs."""
    try:
        rt = ctx.deps
        state = rt.tab(tab)
        if mode == "frames":
            if locator is not None or extract is not None:
                raise ToolFault("INVALID_ARGUMENT", "Frame inspection cannot be combined with a locator extraction.")
            result = rt._rpc("page.frames", {"tabId": state.raw_id}, tab=tab) or {}
            return _ok(tab, {"mode": "frames", "frames": result.get("frames", [])})
        if locator is None:
            if extract is not None:
                raise ToolFault("INVALID_ARGUMENT", "A locator extraction requires locator.")
            return _ok(tab, rt.observe(tab, mode, max_nodes))
        if extract is None:
            # A locator with no requested scalar naturally means "read this
            # element".  Defaulting avoids an otherwise Pydantic-valid but
            # semantically incomplete call and matches the common agent intent.
            extract = "all_inner_text"
        methods = {
            "count": "locator.count", "text": "locator.textContent",
            "all_text": "locator.allTextContents", "all_inner_text": "locator.allInnerTexts",
            "attribute": "locator.getAttribute", "bounding_box": "locator.boundingBox",
        }
        params = {"tabId": state.raw_id, **_locator_params(locator)}
        if extract in {"text", "attribute", "bounding_box"}:
            params["index"] = index
        if extract == "attribute":
            if not attribute:
                raise ToolFault("INVALID_ARGUMENT", "Attribute extraction requires attribute.")
            params["name"] = attribute
        result = rt._rpc(methods[extract], params, tab=tab)
        return _ok(tab, {"mode": "locator", "extract": extract, "result": result})
    except ToolFault as fault:
        return _error(tab, fault)


def _act_target(rt: BrowserRuntime, tab: str, target: Target, command: Any) -> tuple[str, dict[str, Any]]:
    action = command.action
    timeout = command.timeout_ms
    ref_methods = {
        "click": "locator.clickRef", "fill": "locator.fillRef", "press": "locator.pressRef",
        "hover": "locator.hoverRef", "select": "locator.selectOptionRef",
    }
    locator_methods = {
        "click": "locator.click", "fill": "locator.fill", "press": "locator.press",
        "select": "locator.selectOption",
    }
    state = rt.tab(tab)
    if isinstance(target, RefTarget):
        params = rt.ref_params(tab, target.ref, timeout)
        params["stable"] = True
        return ref_methods[action], params
    locator_params = _action_locator_params(target.locator)
    if action == "hover":
        selector = locator_params["locator"].get("selector")
        if not selector:
            raise ToolFault("INVALID_ARGUMENT", "Locator hover requires a CSS selector; use a ref otherwise.")
        params = {"tabId": state.raw_id, "selector": selector, "index": 0}
        if "frameSelector" in locator_params:
            params["frameSelector"] = locator_params["frameSelector"]
        return "dom.hover", params
    params = {"tabId": state.raw_id, **locator_params, "timeoutMs": timeout}
    params.update(index=0, strict=True, stable=True)
    return locator_methods[action], params


def browser_act(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: ActCommand) -> dict[str, Any]:
    """Perform exactly one typed interaction in a tab."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        if isinstance(command, (Click, Fill, Press, Hover, Select)):
            if isinstance(command, Press) and command.target is None:
                method, params = "keyboard.press", {"tabId": state.raw_id, "key": command.key}
            else:
                method, params = _act_target(rt, tab, command.target, command)
            if isinstance(command, Fill):
                params["text"] = command.value
            elif isinstance(command, Press):
                params["key"] = command.key
            elif isinstance(command, Select):
                params["options"] = command.options
        elif isinstance(command, Check):
            method = f"locator.{command.action}"
            params = {
                "tabId": state.raw_id, **_action_locator_params(command.locator), "timeoutMs": command.timeout_ms,
                "index": 0, "strict": True, "stable": True,
            }
        elif isinstance(command, TypeSequentially):
            method = "locator.pressSequentially"
            params = {
                "tabId": state.raw_id, **_action_locator_params(command.locator), "text": command.value,
                "delayMs": command.delay_ms, "index": 0, "strict": True,
                "timeoutMs": command.timeout_ms,
            }
        elif isinstance(command, Upload):
            method = "locator.setInputFiles"
            params = {
                "tabId": state.raw_id, **_action_locator_params(command.locator),
                "files": rt.validated_files(command.files), "timeoutMs": command.timeout_ms,
                "index": 0, "strict": True,
            }
        elif isinstance(command, Drag):
            method = "locator.dragTo"
            params = {
                "tabId": state.raw_id, **_action_locator_params(command.source),
                "targetLocator": _action_locator_params(command.destination)["locator"], "timeoutMs": command.timeout_ms,
                "index": 0, "targetIndex": 0, "strict": True,
            }
        elif isinstance(command, Scroll):
            method = "dom.scroll"
            params = {"tabId": state.raw_id, "x": command.delta_x, "y": command.delta_y, "mode": "scrollBy"}
            if command.selector:
                params["selector"] = command.selector
        elif isinstance(command, Focus):
            method = "locator.focus"
            params = {"tabId": state.raw_id, **_action_locator_params(command.locator), "index": command.index}
        else:
            method = "page.acceptDialog" if command.action == "accept_dialog" else "page.dismissDialog"
            params = {"tabId": state.raw_id}
            if command.action == "accept_dialog" and command.prompt_text is not None:
                params["promptText"] = command.prompt_text
        rt.invalidate_visual(tab)
        result = rt._rpc(method, params, tab=tab) or {}
        changed, popup_aliases = _public_action_change(rt, result.get("whatChanged"))
        invalidated = isinstance(changed, dict) and changed.get("urlChanged") is True
        if invalidated:
            state.url = changed.get("toUrl", state.url)
            rt.invalidate(tab)
        metadata = {
            key: _without_bridge_element_refs(value)
            for key, value in result.items()
            if key not in {"ok", "whatChanged"}
        }
        rt.note_ui_success(tab)
        return _ok(tab, {
            "action": command.action, "result": metadata, "whatChanged": changed,
            "openedTabs": popup_aliases, "refsInvalidated": invalidated,
        })
    except ToolFault as fault:
        locator = None
        target = getattr(command, "target", None)
        if isinstance(target, LocatorTarget):
            locator = target.locator
        elif isinstance(getattr(command, "locator", None), Locator):
            locator = command.locator
        return _error(tab, rt.enrich_ui_failure(tab, fault, locator))


def browser_navigate(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: NavigateCommand) -> dict[str, Any]:
    """Open a URL, reload, or move backward or forward in one tab."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        methods = {"open": "page.navigate", "reload": "page.reload", "back": "page.goBack", "forward": "page.goForward"}
        params: dict[str, Any] = {"tabId": state.raw_id, "timeoutMs": command.timeout_ms}
        target_url = command.url if isinstance(command, Open) else None
        if target_url:
            params["url"] = target_url
        rt.invalidate_visual(tab)
        result = rt._rpc(methods[command.operation], params, tab=tab, url=target_url) or {}
        raw_tab = result.get("tab") if isinstance(result.get("tab"), dict) else {}
        state.url = raw_tab.get("url", target_url or state.url)
        state.title = raw_tab.get("title", state.title)
        rt.invalidate(tab)
        return _ok(tab, {
            "operation": command.operation, "url": rt.safe_url(state.url), "title": state.title,
            "whatChanged": result.get("whatChanged"), "refsInvalidated": True,
        })
    except ToolFault as fault:
        return _error(tab, fault)


def _visual_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Validate the bridge's visual lease before retaining any pixels."""
    viewport, scroll, dimensions = (result.get(key) for key in ("viewport", "scroll", "image"))
    if not all(isinstance(value, dict) for value in (viewport, scroll, dimensions)):
        raise ToolFault("VISUAL_METADATA_MISSING", "Reload the bridge: screenshots need viewport and image metadata.")
    for key in ("width", "height"):
        if (not isinstance(viewport.get(key), (int, float)) or isinstance(viewport.get(key), bool)
                or not math.isfinite(viewport[key]) or viewport[key] <= 0):
            raise ToolFault("VISUAL_METADATA_INVALID", "The screenshot viewport is invalid.")
        if (not isinstance(dimensions.get(key), int) or isinstance(dimensions.get(key), bool)
                or not 0 < dimensions[key] <= 1_600):
            raise ToolFault("VISUAL_METADATA_INVALID", "Model screenshots must be bounded to 1,600 pixels.")
    if any(not isinstance(scroll.get(key), (int, float)) or isinstance(scroll.get(key), bool)
           or not math.isfinite(scroll[key]) for key in ("x", "y")):
        raise ToolFault("VISUAL_METADATA_INVALID", "The screenshot scroll position is invalid.")
    if not isinstance(result.get("url"), str) or not isinstance(result.get("screenshotId"), str) or not result["screenshotId"]:
        raise ToolFault("VISUAL_METADATA_MISSING", "The bridge did not issue a usable screenshot lease.")
    return {"url": result["url"], "viewport": viewport, "scroll": scroll}


def _image_point(screenshot: _VisualScreenshot, x: float, y: float) -> tuple[float, float]:
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x < screenshot.image["width"]
            and 0 <= y < screenshot.image["height"]):
        raise ToolFault("INVALID_ARGUMENT", "Coordinates must be inside the latest screenshot image.")
    viewport = screenshot.state["viewport"]
    return x * viewport["width"] / screenshot.image["width"], y * viewport["height"] / screenshot.image["height"]


def browser_visual(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: VisualCommand) -> dict[str, Any]:
    """Recover from insufficient DOM targeting using a fresh image and one bounded interaction.

    Coordinates are pixels in the returned image. Each interaction consumes its
    screenshot; capture again before another visual interaction. Type enters
    text into the currently focused control; use browser_act for keyboard keys.
    """
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        if not rt.visual_enabled:
            raise ToolFault("VISUAL_DISABLED", "Visual recovery has not been enabled for this run.")
        if isinstance(command, VisualCapture):
            rt.invalidate_visual()
            result = rt._rpc("page.screenshot", {"tabId": state.raw_id, "modelFacing": True}, tab=tab) or {}
            metadata = _visual_metadata(result)
            if result.get("tabId") != state.raw_id:
                raise ToolFault("VISUAL_METADATA_INVALID", "The screenshot belongs to a different tab.")
            if not rt._url_allowed(metadata["url"]):
                raise ToolFault("FIREWALL_DENIED", "The screenshot URL is blocked by browser policy.")
            data_url = result.get("dataUrl", "")
            if not isinstance(data_url, str) or not re.match(r"^data:image/(png|jpeg|webp);base64,", data_url):
                raise ToolFault("VISUAL_IMAGE_INVALID", "The bridge did not return a supported image.")
            header, encoded = data_url.split(",", 1)
            try:
                pixels = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ToolFault("VISUAL_IMAGE_INVALID", "The bridge returned malformed image content.") from exc
            if not pixels or len(pixels) > 16_000_000:
                raise ToolFault("VISUAL_IMAGE_INVALID", "The screenshot image is empty or too large.")
            rt._visual_number += 1
            alias = f"screenshot_{rt._visual_number}"
            rt._visual_screenshot = _VisualScreenshot(
                alias, tab, result["screenshotId"], {"tabId": state.raw_id, **metadata}, result["image"],
                BinaryContent(data=pixels, media_type=header[5:].split(";", 1)[0]),
            )
            state.url = metadata["url"]
            return _ok(tab, {
                "operation": "capture", "screenshot": alias, "url": rt.safe_url(state.url),
                "viewport": metadata["viewport"], "scroll": metadata["scroll"], "image": result["image"],
            })
        if not rt.allow_coordinate_fallback:
            raise ToolFault("COORDINATE_FALLBACK_DISABLED", "Coordinate interaction is disabled by configuration.")
        screenshot = rt._visual_screenshot
        if screenshot is None or screenshot.alias != command.screenshot or screenshot.tab != tab:
            raise ToolFault("STALE_SCREENSHOT", "Capture this tab again before a coordinate interaction.", True)
        method = f"computer.{command.operation}"
        params: dict[str, Any] = {
            "tabId": state.raw_id, "screenshotId": screenshot.bridge_id, "expectedVisualState": screenshot.state,
        }
        if isinstance(command, VisualType):
            params["text"] = command.value
        else:
            x, y = _image_point(screenshot, command.x, command.y)
            if isinstance(command, VisualDrag):
                end_x, end_y = _image_point(screenshot, command.end_x, command.end_y)
                params.update(fromX=x, fromY=y, toX=end_x, toY=end_y)
            else:
                params.update(x=x, y=y)
                if isinstance(command, VisualScroll):
                    params.update(deltaX=command.delta_x, deltaY=command.delta_y)
        # Consume locally before dispatch, including when input has an unknown
        # outcome. The bridge independently checks live URL/viewport/scroll and
        # consumes its own lease immediately before sending the actual input.
        rt.invalidate(tab)
        result = rt._rpc(method, params, tab=tab) or {}
        changed, opened_tabs = _public_action_change(rt, result.get("whatChanged"))
        if isinstance(changed, dict) and changed.get("urlChanged"):
            state.url = changed.get("toUrl", state.url)
        rt.note_ui_success(tab)
        return _ok(tab, {
            "operation": command.operation, "screenshot": command.screenshot, "screenshotConsumed": True,
            "whatChanged": changed, "openedTabs": opened_tabs, "refsInvalidated": True,
        })
    except ToolFault as fault:
        return _error(tab, fault)


def browser_wait(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: WaitCommand) -> dict[str, Any]:
    """Wait for one supported browser condition; this does not assert success."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        methods = {
            "element_state": "locator.waitFor", "url": "page.waitForURL",
            "navigation": "page.waitForNavigation", "page_load": "page.waitForLoad",
            "network_idle": "page.waitForNetworkIdle", "text": "page.waitForText",
            "popup": "page.waitForPopup", "dialog": "page.waitForDialog",
            "request": "page.waitForRequest", "response": "page.waitForResponse",
        }
        params: dict[str, Any] = {"tabId": state.raw_id, "timeoutMs": command.timeout_ms}
        if isinstance(command, ElementWait):
            params.update(**_locator_params(command.locator), state=command.state)
        elif isinstance(command, ValueWait):
            if command.condition == "text":
                params.update(
                    text=command.expected, exact=command.match == "exact",
                    caseSensitive=command.case_sensitive,
                )
                if command.selector:
                    params["selector"] = command.selector
                if command.frame_selector:
                    params["frameSelector"] = command.frame_selector
            else:
                url_key = {"contains": "urlContains", "exact": "url", "regex": "urlRegex"}[command.match]
                params[url_key] = command.expected
        elif isinstance(command, (RequestWait, ResponseWait)):
            mapping = {
                "url_contains": "urlContains", "url_regex": "urlRegex",
                "http_method": "method",
                "resource_type": "resourceType", "mime_type": "mimeType",
                "header_contains": "headerContains", "header_regex": "headerRegex",
                "post_data_contains": "postDataContains", "post_data_regex": "postDataRegex",
                "min_size": "minSize", "max_size": "maxSize", "body_contains": "bodyContains",
                "body_regex": "bodyRegex", "json_path": "jsonPath", "json_contains": "jsonContains",
            }
            for key, value in command.model_dump(exclude={"condition", "timeout_ms"}, exclude_none=True).items():
                params[mapping.get(key, key)] = value
        elif command.condition == "network_idle":
            params.update(idleMs=command.idle_ms, maxInflight=command.max_inflight)
        elif isinstance(command, EventWait):
            for key, bridge_key in (
                ("url", "url"), ("url_contains", "urlContains"), ("url_regex", "urlRegex"),
                ("dialog_type", "type"), ("message_contains", "messageContains"),
            ):
                value = getattr(command, key)
                if value is not None:
                    params[bridge_key] = value
        result = rt._rpc(methods[command.condition], params, tab=tab) or {}
        invalidated = command.condition in {"url", "navigation"}
        if isinstance(command, (RequestWait, ResponseWait)) and isinstance(result, dict):
            event_key = command.condition
            if isinstance(result.get(event_key), dict):
                result = {**result, event_key: rt.public_requests([result[event_key]])[0]}
        popup_tab = None
        if command.condition == "popup" and isinstance(result, dict) and isinstance(result.get("tab"), dict):
            popup_tab = rt._register(result["tab"])
            result = {**result, "tab": rt.public_tab(popup_tab)}
        if invalidated:
            state.url = result.get("url", state.url) if isinstance(result, dict) else state.url
            rt.invalidate(tab)
        return _ok(tab, {
            "condition": command.condition, "matched": True, "result": _without_bridge_element_refs(result),
            "openedTab": popup_tab, "refsInvalidated": invalidated,
        })
    except ToolFault as fault:
        return _error(tab, fault)


def browser_assert(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: AssertCommand) -> dict[str, Any]:
    """Verify one expected page or element condition."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        methods = {
            "visible": "expect.locator.toBeVisible", "hidden": "expect.locator.toBeHidden",
            "enabled": "expect.locator.toBeEnabled", "disabled": "expect.locator.toBeDisabled",
            "editable": "expect.locator.toBeEditable", "checked": "expect.locator.toBeChecked",
            "value": "expect.locator.toHaveValue", "text": "expect.locator.toHaveText",
            "count": "expect.locator.toHaveCount", "attribute": "expect.locator.toHaveAttribute",
            "title": "expect.page.toHaveTitle", "aria_snapshot": "expect.page.toMatchAriaSnapshot",
            "url": "page.waitForURL",
        }
        params: dict[str, Any] = {"tabId": state.raw_id, "timeoutMs": command.timeout_ms}
        expected: Any = None
        if not isinstance(command, (TitleAssertion, AriaAssertion, URLAssertion)):
            params.update(_locator_params(command.locator))
        if isinstance(command, StateAssertion):
            expected = command.expected
            if command.assertion == "checked":
                params["checked"] = command.expected
            elif not command.expected:
                raise ToolFault("INVALID_ARGUMENT", "Use the opposite state assertion (hidden/visible or disabled/enabled) for a negative check.")
        elif isinstance(command, (ValueAssertion, TextAssertion)):
            expected = command.expected
            params["expected"] = expected
            if isinstance(command, TextAssertion) and command.contains:
                params["contains"] = True
            if isinstance(command, TextAssertion):
                params.update(regex=command.regex, caseSensitive=command.case_sensitive)
        elif isinstance(command, CountAssertion):
            expected = command.count
            params["expected"] = expected
        elif isinstance(command, AttributeAssertion):
            expected = command.expected
            params.update(attribute=command.attribute, expected=expected)
        elif isinstance(command, (TitleAssertion, AriaAssertion, URLAssertion)):
            expected = command.expected
            if isinstance(command, TitleAssertion):
                params["titleContains" if command.contains else "title"] = expected
            elif isinstance(command, URLAssertion):
                params["url"] = expected
            else:
                params["expected"] = expected
        try:
            actual = rt._rpc(methods[command.assertion], params, tab=tab)
        except ToolFault as fault:
            if fault.code not in {"ASSERTION_FAILED", "PAGE_WAIT_FOR_URL_TIMEOUT"}:
                raise
            return _ok(tab, {"assertion": command.assertion, "passed": False, "expected": expected, "detail": fault.detail})
        return _ok(tab, {"assertion": command.assertion, "passed": True, "expected": expected, "actual": actual,
                         "match": "regex" if getattr(command, "regex", False) else "contains" if getattr(command, "contains", False) else "exact"})
    except ToolFault as fault:
        return _error(tab, fault)


def _text_data_url(text: str, mime: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def browser_capture_evidence(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: CaptureCommand) -> dict[str, Any]:
    """Capture a screenshot, snapshot, PDF, or trace artifact."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        filename = rt.safe_filename(getattr(command, "filename", None))
        methods = {
            "page_screenshot": "page.screenshot", "element_screenshot": "locator.screenshot",
            "dom_snapshot": "page.domSnapshot", "accessibility_snapshot": "page.accessibilityTree",
            "pdf": "page.pdf", "trace_start": "trace.start", "trace_stop": "trace.stop",
            "trace_export": "trace.exportHtml",
        }
        params: dict[str, Any] = {"tabId": state.raw_id}
        if isinstance(command, ElementCapture):
            params.update(_locator_params(command.locator))
        elif isinstance(command, (TraceStop, TraceExport)):
            params["traceId"] = rt.trace(command.trace)
        elif command.capture == "accessibility_snapshot":
            params.update(format="compact", maxNodes=1_000)
        result = rt._rpc(methods[command.capture], params, tab=tab) or {}
        if isinstance(command, TraceStart):
            raw_trace = result.get("trace", {})
            bridge_trace = raw_trace.get("id") if isinstance(raw_trace, dict) else None
            if not isinstance(bridge_trace, str):
                raise ToolFault("BRIDGE_ERROR", "The bridge did not return a trace.")
            return _ok(tab, {"capture": command.capture, "trace": rt.new_trace(bridge_trace)})
        evidence = rt.evidence()
        data: dict[str, Any] = {"capture": command.capture, "evidence": evidence, "url": rt.safe_url(state.url)}
        save = getattr(command, "save", False)
        data_url = result.get("dataUrl")
        if isinstance(data_url, str) and data_url:
            if save:
                data.update(saved=True, **rt.save(data_url, filename))
            else:
                data.update(saved=False, note="Artifact content omitted; set save=true to persist it.")
        elif command.capture in {"dom_snapshot", "accessibility_snapshot", "trace_export"}:
            if command.capture == "dom_snapshot":
                text, mime, extension = json.dumps(_public(result), ensure_ascii=False), "application/json", "json"
            elif command.capture == "trace_export":
                text, mime, extension = result.get("html", ""), "text/html", "html"
            else:
                text = _snapshot_without_bridge_refs(result.get("snapshot", ""))
                mime, extension = "text/plain", "txt"
            if save:
                data.update(saved=True, **rt.save(_text_data_url(text, mime), filename or f"{evidence}.{extension}"))
            else:
                data.update(saved=False, preview=text[:2_000], truncated=len(text) > 2_000)
        else:
            data["saved"] = False
        if isinstance(command, (TraceStop, TraceExport)):
            data["trace"] = command.trace
        return _ok(tab, data)
    except ToolFault as fault:
        return _error(tab, fault)


def browser_diagnose(ctx: RunContext[BrowserRuntime], tab: NonEmpty, command: DiagnoseCommand) -> dict[str, Any]:
    """Read bounded console, network, DOM, response-body, or trace diagnostics."""
    rt = ctx.deps
    try:
        state = rt.tab(tab)
        methods = {
            "console": "console.read", "network": "network.read",
            "response_body": "network.getResponseBody", "dom": "page.domSnapshot",
            "trace_status": "trace.status",
        }
        params: dict[str, Any] = {"tabId": state.raw_id}
        if isinstance(command, BufferedDiagnostic):
            params["limit"] = command.limit
        elif isinstance(command, ResponseBodyDiagnostic):
            if not rt.allow_response_body:
                raise ToolFault("SENSITIVE_OPERATION_BLOCKED", "Response-body diagnostics are disabled.")
            params["requestId"] = rt.request(command.request)
        elif isinstance(command, TraceDiagnostic) and command.trace:
            params["traceId"] = rt.trace(command.trace)
        result = rt._rpc(methods[command.diagnostic], params, tab=tab) or {}
        if command.diagnostic in {"console", "network"}:
            events = result.get("events", []) if isinstance(result, dict) else []
            if command.diagnostic == "network":
                events = rt.public_requests(events)
            returned_limit = min(command.limit, MAX_ITEMS)
            payload = {
                "events": events[:returned_limit],
                "count": len(events),
                "truncated": len(events) > returned_limit,
            }
        elif command.diagnostic == "response_body":
            body = result.get("body", "")
            payload = {"request": command.request, "body": body[:MAX_BODY], "truncated": len(body) > MAX_BODY}
        elif command.diagnostic == "dom":
            import json
            text = json.dumps(_public(result), ensure_ascii=False)
            payload = {"summary": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}
        else:
            payload = {"status": _without_trace_ids(result), "trace": getattr(command, "trace", None)}
        return _ok(tab, {"diagnostic": command.diagnostic, **payload})
    except ToolFault as fault:
        return _error(tab, fault)


def _public_download(rt: BrowserRuntime, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    result = {
        key: rt.safe_url(value) if key in {"url", "finalUrl"} and isinstance(value, str) else value
        for key, value in item.items()
        if key != "id"
    }
    started = _download_started(item)
    result["taskEligible"] = started is not None and started >= rt.task_started_wallclock
    return result


def _download_started(item: Any) -> float | None:
    if not isinstance(item, dict) or not isinstance(item.get("startTime"), str):
        return None
    try:
        stamp = datetime.fromisoformat(item["startTime"].replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _download_matches(item: dict[str, Any], command: WaitForDownload) -> bool:
    if command.state != "any" and item.get("state") != command.state:
        return False
    if command.state == "complete" and item.get("exists") is False:
        return False
    urls = [str(item.get(key) or "") for key in ("url", "finalUrl")]
    filename = str(item.get("filename") or "")
    if command.url and command.url not in urls:
        return False
    if command.url_contains and not any(command.url_contains in url for url in urls):
        return False
    if command.filename and filename != command.filename:
        return False
    if command.filename_contains and command.filename_contains not in filename:
        return False
    try:
        if command.filename_regex and not re.search(command.filename_regex, filename):
            return False
        if command.url_regex and not any(re.search(command.url_regex, url) for url in urls):
            return False
    except re.error as exc:
        raise ToolFault("INVALID_ARGUMENT", "Download regular expression is invalid.") from exc
    if command.query and command.query.lower() not in " ".join([filename, *urls]).lower():
        return False
    return True


def browser_downloads(ctx: RunContext[BrowserRuntime], command: DownloadCommand) -> dict[str, Any]:
    """List downloads or wait for a matching download without exposing browser download IDs."""
    rt = ctx.deps
    try:
        mapping = {
            "filename_regex": "filenameRegex", "url_regex": "urlRegex",
            "filename_contains": "filenameContains", "url_contains": "urlContains",
            "include_existing": "includeExisting", "timeout_ms": "timeoutMs",
        }
        params = {
            mapping.get(key, key): value
            for key, value in command.model_dump(exclude={"operation"}, exclude_none=True).items()
        }
        params["startedAfter"] = int(rt.task_started_wallclock * 1_000)
        method = "downloads.list" if isinstance(command, ListDownloads) else "downloads.waitFor"
        result = rt._rpc(method, params) or {}
        if isinstance(command, ListDownloads):
            items = [
                _public_download(rt, item) for item in result.get("items", [])[:command.limit]
                if isinstance(item, dict) and (
                    _download_started(item) is None or _download_started(item) >= rt.task_started_wallclock
                )
            ]
            data = {"operation": "list", "items": items, "count": len(items)}
        else:
            item = result.get("item")
            started = _download_started(item)
            if (not isinstance(item, dict) or started is None or started < rt.task_started_wallclock
                    or not _download_matches(item, command)):
                raise ToolFault("DOWNLOAD_NOT_MATCHED", "No matching download from the current task was returned.", True)
            data = {
                "operation": "wait", "matched": True,
                "download": _public_download(rt, item),
                "elapsedMs": result.get("elapsedMs"),
            }
        return _ok(None, data)
    except ToolFault as fault:
        return _error(None, fault)


__all__ = [
    "BrowserRuntime", "Locator",
    "browser_tabs", "browser_observe", "browser_act", "browser_navigate",
    "browser_wait", "browser_assert", "browser_capture_evidence", "browser_diagnose",
    "browser_downloads",
    "browser_visual",
]
