"""Load and validate ``axis.yaml`` — the one configuration file for
browser/limits/CLI settings (Phase 1.2).

Deliberately one layer, not a configuration framework: one loader function,
one typed result (`AxisConfig`), one error type. Environment variables
remain for credentials/secrets only (see `provider_config.py`); nothing
here reads or accepts a credential.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import yaml  # noqa: E402

from axis.models import AxisRunLimits, AxisRunLimitsError  # noqa: E402
from browser_agent_tools import FirewallConfig  # noqa: E402

DEFAULT_CONFIG_PATH = AGENT_DIR / "axis.yaml"


class AxisConfigError(RuntimeError):
    """Raised when axis.yaml is missing a required section, has an invalid
    type, a negative/zero-when-not-allowed limit, or an unknown top-level/
    important key — a controlled startup failure, never a raw traceback
    from a YAML parser or a KeyError deep in application code."""


@dataclass(frozen=True)
class BrowserConfig:
    whole_browser_access: bool = True
    initial_tab: str = "focused"
    max_open_tabs: int = 30
    allow_response_body: bool = False
    allow_coordinate_fallback: bool = False  # recorded only; no schema accepts coordinates yet
    firewall: FirewallConfig = field(default_factory=FirewallConfig)


@dataclass(frozen=True)
class CliConfig:
    trace: str = "verbose"
    show_model_text: bool = True
    show_tool_arguments: bool = True
    show_tool_results: bool = True
    show_usage: bool = True
    redact_sensitive_values: bool = True


@dataclass(frozen=True)
class Phase2PlanningConfig:
    enabled: bool = True
    required_for_mutations: bool = True
    max_items: int = 12


@dataclass(frozen=True)
class Phase2EffectsConfig:
    max_effects_per_run: int = 20
    max_mutations_per_effect: int = 10


@dataclass(frozen=True)
class Phase2ApprovalsConfig:
    read: str = "allow"
    reversible_local: str = "allow"
    external_effect: str = "require"
    destructive_high_impact: str = "require"

    def as_dict(self) -> Dict[str, str]:
        return {
            "read": self.read, "reversible_local": self.reversible_local,
            "external_effect": self.external_effect, "destructive_high_impact": self.destructive_high_impact,
        }


@dataclass(frozen=True)
class Phase2RemindersConfig:
    enabled: bool = True
    interval_requests: int = 5
    max_fires: int = 4


@dataclass(frozen=True)
class Phase2Config:
    planning: Phase2PlanningConfig = field(default_factory=Phase2PlanningConfig)
    effects: Phase2EffectsConfig = field(default_factory=Phase2EffectsConfig)
    approvals: Phase2ApprovalsConfig = field(default_factory=Phase2ApprovalsConfig)
    reminders: Phase2RemindersConfig = field(default_factory=Phase2RemindersConfig)


@dataclass(frozen=True)
class Phase3TemporalConfig:
    enabled: bool = False
    target: str = ""
    namespace: str = "default"
    task_queue: str = "axis-agent"
    workflow_execution_timeout_seconds: float = 86400
    model_activity_timeout_seconds: float = 180
    browser_read_activity_timeout_seconds: float = 60
    browser_mutation_activity_timeout_seconds: float = 120


@dataclass(frozen=True)
class Phase3RetriesConfig:
    model_max_attempts: int = 3
    browser_read_max_attempts: int = 3
    browser_mutation_max_attempts: int = 1


@dataclass(frozen=True)
class Phase3LeasesConfig:
    enabled: bool = True
    conflict_policy: str = "wait"
    acquire_timeout_seconds: float = 30
    duration_seconds: float = 30


@dataclass(frozen=True)
class Phase3RecoveryConfig:
    reconcile_unknown_effects: bool = True


@dataclass(frozen=True)
class Phase3HistoryConfig:
    # 100 let workflow history grow past 8MB and >800KB per-turn payloads in
    # observed runs, which pushed workflow task processing past Temporal's
    # task-timeout window (TMPRL1103 payload warnings, then "task not found"/
    # "query not found" errors as tasks expired before the worker replied).
    # 20 checkpoints (continue_as_new) well before that, at the cost of more
    # frequent history resets. Retune against real per-turn payload sizes for
    # the target site if this is still too high (or needlessly low).
    max_run_segments: int = 20


@dataclass(frozen=True)
class Phase3Config:
    temporal: Phase3TemporalConfig = field(default_factory=Phase3TemporalConfig)
    retries: Phase3RetriesConfig = field(default_factory=Phase3RetriesConfig)
    leases: Phase3LeasesConfig = field(default_factory=Phase3LeasesConfig)
    recovery: Phase3RecoveryConfig = field(default_factory=Phase3RecoveryConfig)
    history: Phase3HistoryConfig = field(default_factory=Phase3HistoryConfig)


_VALID_DESKTOP_THEMES = {"system", "light", "dark"}


@dataclass(frozen=True)
class Phase4DesktopConfig:
    """Phase 4: the PySide6/QML desktop control surface's own presentation
    settings only — never a second source of Temporal endpoint, namespace,
    task queue, bridge, provider, firewall, or approval configuration (all
    of that stays exclusively under `phase3`/`phase2`/`browser`)."""

    enabled: bool = True
    poll_interval_ms: int = 750
    reconnect_initial_delay_ms: int = 500
    reconnect_max_delay_ms: int = 10_000
    max_timeline_items: int = 300
    max_session_jobs: int = 20
    theme: str = "system"
    animations_enabled: bool = True


@dataclass(frozen=True)
class Phase4Config:
    desktop: Phase4DesktopConfig = field(default_factory=Phase4DesktopConfig)


@dataclass(frozen=True)
class AxisConfig:
    browser: BrowserConfig
    limits: AxisRunLimits
    cli: CliConfig
    max_retries: int = 3
    phase2: Phase2Config = field(default_factory=Phase2Config)
    phase3: Phase3Config = field(default_factory=Phase3Config)
    phase4: Phase4Config = field(default_factory=Phase4Config)


_TOP_LEVEL_KEYS = {"browser", "limits", "cli", "phase2", "phase3", "phase4"}
_PHASE4_KEYS = {"desktop"}
_PHASE4_DESKTOP_KEYS = {
    "enabled", "poll_interval_ms", "reconnect_initial_delay_ms", "reconnect_max_delay_ms",
    "max_timeline_items", "max_session_jobs", "theme", "animations_enabled",
}
_PHASE3_KEYS = {"temporal", "retries", "leases", "recovery", "history"}
_PHASE3_TEMPORAL_KEYS = {
    "enabled", "target", "namespace", "task_queue", "workflow_execution_timeout_seconds",
    "model_activity_timeout_seconds", "browser_read_activity_timeout_seconds",
    "browser_mutation_activity_timeout_seconds",
}
_PHASE3_RETRIES_KEYS = {
    "model_max_attempts", "browser_read_max_attempts", "browser_mutation_max_attempts",
}
_PHASE3_LEASES_KEYS = {"enabled", "conflict_policy", "acquire_timeout_seconds", "duration_seconds"}
_PHASE3_RECOVERY_KEYS = {"reconcile_unknown_effects"}
_PHASE3_HISTORY_KEYS = {"max_run_segments"}
_VALID_CONFLICT_POLICIES = {"wait", "fail"}
# A browser lease must not be able to expire mid-mutation: the durable
# workflow renews it to `duration_seconds` immediately before every browser
# RPC (see axis.durability.runtime._lease_is_current), so as long as the
# lease outlives one mutation activity's own start-to-close timeout by this
# margin, renewal always wins the race against expiry.
LEASE_SAFETY_MARGIN_RATIO = 1.25
_PHASE2_KEYS = {"planning", "effects", "approvals", "reminders"}
_PHASE2_PLANNING_KEYS = {"enabled", "required_for_mutations", "max_items"}
_PHASE2_EFFECTS_KEYS = {"max_effects_per_run", "max_mutations_per_effect"}
_PHASE2_APPROVALS_KEYS = {"read", "reversible_local", "external_effect", "destructive_high_impact"}
_PHASE2_REMINDERS_KEYS = {"enabled", "interval_requests", "max_fires"}
_VALID_APPROVAL_VALUES = {"allow", "require"}
_BROWSER_KEYS = {
    "whole_browser_access", "initial_tab", "max_open_tabs", "allow_response_body",
    "allow_coordinate_fallback", "firewall",
}
_FIREWALL_KEYS = {"default", "allow_urls", "deny_urls", "deny_schemes", "allow_methods", "deny_methods"}
_LIMITS_KEYS = {"max_requests", "max_tool_calls", "max_total_tokens", "max_wall_clock_seconds", "max_retries"}
_CLI_KEYS = {"trace", "show_model_text", "show_tool_arguments", "show_tool_results", "show_usage", "redact_sensitive_values"}


def _require_dict(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AxisConfigError(f"'{label}' must be a mapping (YAML object).")
    return value


def _require_type(value: Any, expected: type, label: str) -> Any:
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise AxisConfigError(f"'{label}' must be a {expected.__name__}.")
    return value


def _optional_positive_number(value: Any, label: str, kind: type) -> Optional[Any]:
    if value is None:
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise AxisConfigError(f"'{label}' must be null or a {kind.__name__}.")
    if value <= 0:
        raise AxisConfigError(f"'{label}' must be null or > 0.")
    return value


def _reject_unknown_keys(data: Dict[str, Any], allowed: set, label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise AxisConfigError(f"Unknown key(s) in '{label}': {', '.join(sorted(unknown))}.")


def _parse_firewall(raw: Dict[str, Any]) -> FirewallConfig:
    _reject_unknown_keys(raw, _FIREWALL_KEYS, "browser.firewall")
    default = raw.get("default", "allow")
    if default not in ("allow", "deny"):
        raise AxisConfigError("'browser.firewall.default' must be 'allow' or 'deny'.")

    def _string_list(key: str) -> Tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise AxisConfigError(f"'browser.firewall.{key}' must be a list of strings.")
        return tuple(value)

    return FirewallConfig(
        default=default,
        allow_urls=_string_list("allow_urls") or ("*",),
        deny_urls=_string_list("deny_urls"),
        deny_schemes=_string_list("deny_schemes"),
        allow_methods=_string_list("allow_methods") or ("*",),
        deny_methods=_string_list("deny_methods"),
    )


def _parse_browser(raw: Dict[str, Any]) -> BrowserConfig:
    _reject_unknown_keys(raw, _BROWSER_KEYS, "browser")
    firewall_raw = raw.get("firewall", {})
    return BrowserConfig(
        whole_browser_access=bool(_require_type(raw.get("whole_browser_access", True), bool, "browser.whole_browser_access")),
        initial_tab=str(_require_type(raw.get("initial_tab", "focused"), str, "browser.initial_tab")),
        max_open_tabs=_require_positive_int(raw.get("max_open_tabs", 30), "browser.max_open_tabs"),
        allow_response_body=bool(_require_type(raw.get("allow_response_body", False), bool, "browser.allow_response_body")),
        allow_coordinate_fallback=bool(_require_type(raw.get("allow_coordinate_fallback", False), bool, "browser.allow_coordinate_fallback")),
        firewall=_parse_firewall(_require_dict(firewall_raw, "browser.firewall")) if firewall_raw else FirewallConfig(),
    )


def _require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AxisConfigError(f"'{label}' must be a positive integer.")
    return value


def _parse_limits(raw: Dict[str, Any]) -> Tuple[AxisRunLimits, int]:
    _reject_unknown_keys(raw, _LIMITS_KEYS, "limits")
    try:
        limits = AxisRunLimits(
            max_requests=_optional_positive_number(raw.get("max_requests", 20), "limits.max_requests", int),
            max_tool_calls=_optional_positive_number(raw.get("max_tool_calls", 30), "limits.max_tool_calls", int),
            max_total_tokens=_optional_positive_number(raw.get("max_total_tokens", 100_000), "limits.max_total_tokens", int),
            max_wall_clock_seconds=_optional_positive_number(raw.get("max_wall_clock_seconds", 180), "limits.max_wall_clock_seconds", (int, float)),
        )
    except AxisRunLimitsError as exc:
        raise AxisConfigError(str(exc)) from exc
    max_retries = _require_positive_int(raw.get("max_retries", 3), "limits.max_retries")
    return limits, max_retries


def _parse_cli(raw: Dict[str, Any]) -> CliConfig:
    _reject_unknown_keys(raw, _CLI_KEYS, "cli")
    trace = raw.get("trace", "verbose")
    if trace not in ("verbose", "quiet"):
        raise AxisConfigError("'cli.trace' must be 'verbose' or 'quiet'.")
    return CliConfig(
        trace=trace,
        show_model_text=bool(_require_type(raw.get("show_model_text", True), bool, "cli.show_model_text")),
        show_tool_arguments=bool(_require_type(raw.get("show_tool_arguments", True), bool, "cli.show_tool_arguments")),
        show_tool_results=bool(_require_type(raw.get("show_tool_results", True), bool, "cli.show_tool_results")),
        show_usage=bool(_require_type(raw.get("show_usage", True), bool, "cli.show_usage")),
        redact_sensitive_values=bool(_require_type(raw.get("redact_sensitive_values", True), bool, "cli.redact_sensitive_values")),
    )


def _parse_phase2_planning(raw: Dict[str, Any]) -> Phase2PlanningConfig:
    _reject_unknown_keys(raw, _PHASE2_PLANNING_KEYS, "phase2.planning")
    return Phase2PlanningConfig(
        enabled=bool(_require_type(raw.get("enabled", True), bool, "phase2.planning.enabled")),
        required_for_mutations=bool(_require_type(raw.get("required_for_mutations", True), bool, "phase2.planning.required_for_mutations")),
        max_items=_require_positive_int(raw.get("max_items", 12), "phase2.planning.max_items"),
    )


def _parse_phase2_effects(raw: Dict[str, Any]) -> Phase2EffectsConfig:
    _reject_unknown_keys(raw, _PHASE2_EFFECTS_KEYS, "phase2.effects")
    return Phase2EffectsConfig(
        max_effects_per_run=_require_positive_int(raw.get("max_effects_per_run", 20), "phase2.effects.max_effects_per_run"),
        max_mutations_per_effect=_require_positive_int(raw.get("max_mutations_per_effect", 10), "phase2.effects.max_mutations_per_effect"),
    )


def _parse_phase2_approvals(raw: Dict[str, Any]) -> Phase2ApprovalsConfig:
    _reject_unknown_keys(raw, _PHASE2_APPROVALS_KEYS, "phase2.approvals")
    values: Dict[str, str] = {}
    for key in _PHASE2_APPROVALS_KEYS:
        value = raw.get(key, "allow" if key in ("read", "reversible_local") else "require")
        if value not in _VALID_APPROVAL_VALUES:
            raise AxisConfigError(f"'phase2.approvals.{key}' must be 'allow' or 'require'.")
        values[key] = value
    return Phase2ApprovalsConfig(**values)


def _parse_phase2_reminders(raw: Dict[str, Any]) -> Phase2RemindersConfig:
    _reject_unknown_keys(raw, _PHASE2_REMINDERS_KEYS, "phase2.reminders")
    return Phase2RemindersConfig(
        enabled=bool(_require_type(raw.get("enabled", True), bool, "phase2.reminders.enabled")),
        interval_requests=_require_positive_int(raw.get("interval_requests", 5), "phase2.reminders.interval_requests"),
        max_fires=_require_positive_int(raw.get("max_fires", 4), "phase2.reminders.max_fires"),
    )


def _parse_phase2(raw: Dict[str, Any]) -> Phase2Config:
    _reject_unknown_keys(raw, _PHASE2_KEYS, "phase2")
    planning_raw = raw.get("planning", {})
    effects_raw = raw.get("effects", {})
    approvals_raw = raw.get("approvals", {})
    reminders_raw = raw.get("reminders", {})
    return Phase2Config(
        planning=_parse_phase2_planning(_require_dict(planning_raw, "phase2.planning")) if planning_raw else Phase2PlanningConfig(),
        effects=_parse_phase2_effects(_require_dict(effects_raw, "phase2.effects")) if effects_raw else Phase2EffectsConfig(),
        approvals=_parse_phase2_approvals(_require_dict(approvals_raw, "phase2.approvals")) if approvals_raw else Phase2ApprovalsConfig(),
        reminders=_parse_phase2_reminders(_require_dict(reminders_raw, "phase2.reminders")) if reminders_raw else Phase2RemindersConfig(),
    )


def _positive_seconds(raw: Dict[str, Any], key: str, default: float, label: str) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise AxisConfigError(f"'{label}' must be a positive number.")
    return float(value)


def _nonempty_string(raw: Dict[str, Any], key: str, default: str, label: str) -> str:
    value = str(_require_type(raw.get(key, default), str, label)).strip()
    if not value:
        raise AxisConfigError(f"'{label}' must not be empty.")
    return value


def _parse_phase3_temporal(raw: Dict[str, Any]) -> Phase3TemporalConfig:
    _reject_unknown_keys(raw, _PHASE3_TEMPORAL_KEYS, "phase3.temporal")
    defaults = Phase3TemporalConfig()
    return Phase3TemporalConfig(
        enabled=bool(_require_type(raw.get("enabled", defaults.enabled), bool, "phase3.temporal.enabled")),
        target=_nonempty_string(raw, "target", defaults.target, "phase3.temporal.target"),
        namespace=_nonempty_string(raw, "namespace", defaults.namespace, "phase3.temporal.namespace"),
        task_queue=_nonempty_string(raw, "task_queue", defaults.task_queue, "phase3.temporal.task_queue"),
        workflow_execution_timeout_seconds=_positive_seconds(raw, "workflow_execution_timeout_seconds", defaults.workflow_execution_timeout_seconds, "phase3.temporal.workflow_execution_timeout_seconds"),
        model_activity_timeout_seconds=_positive_seconds(raw, "model_activity_timeout_seconds", defaults.model_activity_timeout_seconds, "phase3.temporal.model_activity_timeout_seconds"),
        browser_read_activity_timeout_seconds=_positive_seconds(raw, "browser_read_activity_timeout_seconds", defaults.browser_read_activity_timeout_seconds, "phase3.temporal.browser_read_activity_timeout_seconds"),
        browser_mutation_activity_timeout_seconds=_positive_seconds(raw, "browser_mutation_activity_timeout_seconds", defaults.browser_mutation_activity_timeout_seconds, "phase3.temporal.browser_mutation_activity_timeout_seconds"),
    )


def _parse_phase3_retries(raw: Dict[str, Any]) -> Phase3RetriesConfig:
    _reject_unknown_keys(raw, _PHASE3_RETRIES_KEYS, "phase3.retries")
    mutation_max = _require_positive_int(raw.get("browser_mutation_max_attempts", 1), "phase3.retries.browser_mutation_max_attempts")
    if mutation_max != 1:
        raise AxisConfigError(
            "'phase3.retries.browser_mutation_max_attempts' must be 1: browser mutations are not proven "
            "idempotent, so more than one attempt could blindly repeat an ambiguous mutation."
        )
    return Phase3RetriesConfig(
        model_max_attempts=_require_positive_int(raw.get("model_max_attempts", 3), "phase3.retries.model_max_attempts"),
        browser_read_max_attempts=_require_positive_int(raw.get("browser_read_max_attempts", 3), "phase3.retries.browser_read_max_attempts"),
        browser_mutation_max_attempts=mutation_max,
    )


def _parse_phase3_leases(raw: Dict[str, Any]) -> Phase3LeasesConfig:
    _reject_unknown_keys(raw, _PHASE3_LEASES_KEYS, "phase3.leases")
    policy = raw.get("conflict_policy", "wait")
    if policy not in _VALID_CONFLICT_POLICIES:
        raise AxisConfigError("'phase3.leases.conflict_policy' must be 'wait' or 'fail'.")
    return Phase3LeasesConfig(
        enabled=bool(_require_type(raw.get("enabled", True), bool, "phase3.leases.enabled")),
        conflict_policy=policy,
        acquire_timeout_seconds=_positive_seconds(raw, "acquire_timeout_seconds", 30, "phase3.leases.acquire_timeout_seconds"),
        duration_seconds=_positive_seconds(raw, "duration_seconds", 30, "phase3.leases.duration_seconds"),
    )


def _parse_phase3_recovery(raw: Dict[str, Any]) -> Phase3RecoveryConfig:
    _reject_unknown_keys(raw, _PHASE3_RECOVERY_KEYS, "phase3.recovery")
    return Phase3RecoveryConfig(
        reconcile_unknown_effects=bool(_require_type(raw.get("reconcile_unknown_effects", True), bool, "phase3.recovery.reconcile_unknown_effects")),
    )


def _parse_phase3_history(raw: Dict[str, Any]) -> Phase3HistoryConfig:
    _reject_unknown_keys(raw, _PHASE3_HISTORY_KEYS, "phase3.history")
    return Phase3HistoryConfig(
        max_run_segments=_require_positive_int(raw.get("max_run_segments", 100), "phase3.history.max_run_segments"),
    )


def _parse_phase3(raw: Dict[str, Any]) -> Phase3Config:
    _reject_unknown_keys(raw, _PHASE3_KEYS, "phase3")
    temporal_raw = raw.get("temporal", {})
    retries_raw = raw.get("retries", {})
    leases_raw = raw.get("leases", {})
    recovery_raw = raw.get("recovery", {})
    history_raw = raw.get("history", {})
    temporal = _parse_phase3_temporal(_require_dict(temporal_raw, "phase3.temporal")) if temporal_raw else Phase3TemporalConfig()
    leases = _parse_phase3_leases(_require_dict(leases_raw, "phase3.leases")) if leases_raw else Phase3LeasesConfig()
    if leases.enabled:
        required_minimum = temporal.browser_mutation_activity_timeout_seconds * LEASE_SAFETY_MARGIN_RATIO
        if leases.duration_seconds < required_minimum:
            raise AxisConfigError(
                "'phase3.leases.duration_seconds' "
                f"({leases.duration_seconds}) must be at least "
                f"{LEASE_SAFETY_MARGIN_RATIO}x 'phase3.temporal.browser_mutation_activity_timeout_seconds' "
                f"({temporal.browser_mutation_activity_timeout_seconds}), i.e. >= {required_minimum}: "
                "a lease must never be able to expire while a browser mutation is still running."
            )
    return Phase3Config(
        temporal=temporal,
        retries=_parse_phase3_retries(_require_dict(retries_raw, "phase3.retries")) if retries_raw else Phase3RetriesConfig(),
        leases=leases,
        recovery=_parse_phase3_recovery(_require_dict(recovery_raw, "phase3.recovery")) if recovery_raw else Phase3RecoveryConfig(),
        history=_parse_phase3_history(_require_dict(history_raw, "phase3.history")) if history_raw else Phase3HistoryConfig(),
    )


def _require_int_range(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AxisConfigError(f"'{label}' must be an integer.")
    if value < minimum or value > maximum:
        raise AxisConfigError(f"'{label}' must be between {minimum} and {maximum} (got {value}).")
    return value


def _parse_phase4_desktop(raw: Dict[str, Any]) -> Phase4DesktopConfig:
    _reject_unknown_keys(raw, _PHASE4_DESKTOP_KEYS, "phase4.desktop")
    defaults = Phase4DesktopConfig()
    poll_interval_ms = _require_int_range(
        raw.get("poll_interval_ms", defaults.poll_interval_ms), "phase4.desktop.poll_interval_ms",
        minimum=250, maximum=10_000,
    )
    reconnect_initial_delay_ms = _require_int_range(
        raw.get("reconnect_initial_delay_ms", defaults.reconnect_initial_delay_ms),
        "phase4.desktop.reconnect_initial_delay_ms", minimum=1, maximum=60_000,
    )
    reconnect_max_delay_ms = _require_int_range(
        raw.get("reconnect_max_delay_ms", defaults.reconnect_max_delay_ms),
        "phase4.desktop.reconnect_max_delay_ms", minimum=1, maximum=60_000,
    )
    if reconnect_max_delay_ms < reconnect_initial_delay_ms:
        raise AxisConfigError(
            "'phase4.desktop.reconnect_max_delay_ms' must be >= 'phase4.desktop.reconnect_initial_delay_ms'."
        )
    max_timeline_items = _require_int_range(
        raw.get("max_timeline_items", defaults.max_timeline_items), "phase4.desktop.max_timeline_items",
        minimum=1, maximum=2000,
    )
    max_session_jobs = _require_int_range(
        raw.get("max_session_jobs", defaults.max_session_jobs), "phase4.desktop.max_session_jobs",
        minimum=1, maximum=100,
    )
    theme = raw.get("theme", defaults.theme)
    if theme not in _VALID_DESKTOP_THEMES:
        raise AxisConfigError(f"'phase4.desktop.theme' must be one of {sorted(_VALID_DESKTOP_THEMES)}.")
    return Phase4DesktopConfig(
        enabled=bool(_require_type(raw.get("enabled", defaults.enabled), bool, "phase4.desktop.enabled")),
        poll_interval_ms=poll_interval_ms,
        reconnect_initial_delay_ms=reconnect_initial_delay_ms,
        reconnect_max_delay_ms=reconnect_max_delay_ms,
        max_timeline_items=max_timeline_items,
        max_session_jobs=max_session_jobs,
        theme=theme,
        animations_enabled=bool(_require_type(
            raw.get("animations_enabled", defaults.animations_enabled), bool, "phase4.desktop.animations_enabled",
        )),
    )


def _parse_phase4(raw: Dict[str, Any]) -> Phase4Config:
    _reject_unknown_keys(raw, _PHASE4_KEYS, "phase4")
    desktop_raw = raw.get("desktop", {})
    return Phase4Config(
        desktop=_parse_phase4_desktop(_require_dict(desktop_raw, "phase4.desktop")) if desktop_raw else Phase4DesktopConfig(),
    )


def parse_axis_config(raw: Dict[str, Any]) -> AxisConfig:
    """Validate an already-parsed YAML mapping into a typed `AxisConfig`.
    Raises `AxisConfigError` (never a raw KeyError/TypeError/yaml.YAMLError)
    on any structural problem."""
    if not isinstance(raw, dict):
        raise AxisConfigError("axis.yaml must contain a top-level mapping.")
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, "axis.yaml")
    for required in ("browser", "limits"):
        if required not in raw:
            raise AxisConfigError(f"axis.yaml is missing the required '{required}' section.")

    browser = _parse_browser(_require_dict(raw["browser"], "browser"))
    limits, max_retries = _parse_limits(_require_dict(raw["limits"], "limits"))
    cli_raw = raw.get("cli", {})
    cli = _parse_cli(_require_dict(cli_raw, "cli")) if cli_raw else CliConfig()
    phase2_raw = raw.get("phase2", {})
    phase2 = _parse_phase2(_require_dict(phase2_raw, "phase2")) if phase2_raw else Phase2Config()
    phase3_raw = raw.get("phase3", {})
    phase3 = _parse_phase3(_require_dict(phase3_raw, "phase3")) if phase3_raw else Phase3Config()
    phase4_raw = raw.get("phase4", {})
    phase4 = _parse_phase4(_require_dict(phase4_raw, "phase4")) if phase4_raw else Phase4Config()

    return AxisConfig(
        browser=browser, limits=limits, cli=cli, max_retries=max_retries,
        phase2=phase2, phase3=phase3, phase4=phase4,
    )


def load_axis_config(path: Optional[Path] = None) -> AxisConfig:
    """Load and validate axis.yaml. Looks at, in order: the explicit `path`
    argument, then `AXIS_CONFIG_PATH`, then the project-root default
    (`axis.yaml` next to `browser_agent_tools.py`). Raises `AxisConfigError`
    — never an unhandled YAML/IO exception — on any problem, including a
    missing file."""
    if path is None:
        env_path = os.environ.get("AXIS_CONFIG_PATH")
        path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise AxisConfigError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AxisConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise AxisConfigError(f"{path} is empty.")
    return parse_axis_config(raw)
