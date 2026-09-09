"""Shared construction for CLI and the local UI service, without console setup."""
from typing import Any
from browser_bridge_client import BrowserBridgeClient
from browser_tools import BrowserRuntime
from axis.agents import build_model, build_navigator, build_planner
from axis.models import AxisConfig
from axis.orchestrator import AxisOrchestrator


def build_runtime(config: AxisConfig, client: Any | None = None) -> BrowserRuntime:
    """One :class:`BrowserRuntime` configured from ``axis.yaml``."""
    browser = config.browser
    firewall = browser.firewall
    return BrowserRuntime(
        client or BrowserBridgeClient(),
        max_tabs=browser.max_open_tabs,
        allow_response_body=browser.allow_response_body,
        upload_roots=tuple(browser.upload_roots),
        artifact_directory=browser.artifact_directory,
        firewall_default=firewall.default,
        allow_urls=tuple(firewall.allow_urls),
        deny_urls=tuple(firewall.deny_urls),
        deny_schemes=tuple(firewall.deny_schemes),
        allow_methods=tuple(firewall.allow_methods),
        deny_methods=tuple(firewall.deny_methods),
        allow_coordinate_fallback=browser.allow_coordinate_fallback,
    )


def build_orchestrator(
    config: AxisConfig,
    *,
    bridge: Any | None = None,
    openai_client: Any | None = None,
    model_name: str | None = None,
    on_event: Any | None = None,
    approval: Any | None = None,
) -> AxisOrchestrator:
    """Wire config -> one shared model -> planner + navigator -> orchestrator."""
    model = build_model(config, client=openai_client, model_name=model_name)

    def navigator_factory(*, capture: bool, diagnose: bool, downloads: bool, visual: bool = False):
        """Rebuild the navigator when an opt-in tool becomes necessary. Cheap:
        the model instance is shared, only the tool list changes."""
        variant = config.model_copy(deep=True)
        variant.tools.capture_evidence = capture
        variant.tools.diagnose = diagnose
        variant.tools.downloads = downloads
        variant.tools.visual = visual
        return build_navigator(variant, model)

    return AxisOrchestrator(
        browser=build_runtime(config, bridge),
        planner=build_planner(config, model),
        navigator=build_navigator(config, model),
        config=config,
        on_event=on_event,
        approval=approval,
        navigator_factory=navigator_factory,
    )


