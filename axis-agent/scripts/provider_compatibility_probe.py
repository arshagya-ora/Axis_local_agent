#!/usr/bin/env python3
"""OCI/Grok Responses-API compatibility probe (Phase 0).

Standalone script: probes a fixed set of Responses-API features
independently, classifies each as ``supported`` / ``unsupported`` /
``inconclusive``, and writes one sanitized, bounded JSON report. Safe to
import and run without live OCI credentials or the SDK ever having exercised
these code paths before — missing configuration or an unexpected provider
response degrades individual features (or the whole run) to
``inconclusive`` rather than raising.

This talks to the raw ``openai.AsyncOpenAI`` Responses API client directly
(via ``provider_config.build_async_openai_client``), not through Pydantic
AI's ``OpenAIResponsesModel``: Pydantic AI's abstraction hides exactly the
request/response detail (``previous_response_id``, background/cancel,
usage, streaming deltas, strict schemas) this probe exists to verify.

Usage:
    python scripts/provider_compatibility_probe.py [--environment LABEL] [--out PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pydantic import BaseModel, Field  # noqa: E402

from browser_agent_tools import _redact_value  # noqa: E402 - reuse the existing redactor, don't reinvent it
from browser_contract import BROWSER_CONTRACT_VERSION, build_contract, compute_contract_hash  # noqa: E402
from provider_config import (  # noqa: E402
    ProviderConfig,
    ProviderConfigError,
    build_async_openai_client,
    load_provider_config,
)

PROBE_SCHEMA_VERSION = "1.0.0"
MANIFEST_SCHEMA_VERSION = "1.0.0"
MAX_DIAGNOSTIC_CHARS = 400
DEFAULT_REPORT_PATH = AGENT_DIR / "reports" / "provider_compatibility_report.json"

_OCID_PATTERN = re.compile(r"ocid1\.[a-z0-9_.\-]+", re.IGNORECASE)

RELEVANT_PACKAGES = (
    "pydantic-ai-slim",
    "pydantic-ai",
    "pydantic-ai-harness",
    "openai",
    "oci",
    "oci-openai",
    "httpx",
    "pydantic",
)


# =====================================================================
# Result / report / manifest models
# =====================================================================

class ProbeStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    INCONCLUSIVE = "inconclusive"


class FeatureResult(BaseModel):
    feature: str
    status: ProbeStatus
    diagnostic: str = ""
    latencyMs: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProbeReport(BaseModel):
    schemaVersion: str = PROBE_SCHEMA_VERSION
    executedAtUtc: str
    environment: str
    region: Optional[str] = None
    endpointHost: Optional[str] = None
    modelAlias: Optional[str] = None
    apiVersion: Optional[str] = None
    pythonVersion: str
    packageVersions: Dict[str, str] = Field(default_factory=dict)
    results: List[FeatureResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    """Minimal typed record of what a probe/contract run actually covered.
    Deliberately has no workflow state, persistence, or identity fields —
    see PHASE_0_BASELINE.md for what Phase 0 does and does not include."""

    manifestSchemaVersion: str = MANIFEST_SCHEMA_VERSION
    browserContractVersion: str
    browserContractHash: str
    probeVersion: str = PROBE_SCHEMA_VERSION
    modelAlias: Optional[str] = None
    endpointHost: Optional[str] = None
    region: Optional[str] = None
    apiVersion: Optional[str] = None
    dependencyVersions: Dict[str, str] = Field(default_factory=dict)
    createdAtUtc: str


# =====================================================================
# Sanitization / bounding (report-writing safety net)
# =====================================================================

def bound_diagnostic(text: Optional[str], limit: int = MAX_DIAGNOSTIC_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _scrub_ocids(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub_ocids(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_ocids(v) for v in value]
    if isinstance(value, str):
        return _OCID_PATTERN.sub("<redacted-ocid>", value)
    return value


def sanitize_for_report(value: Any) -> Any:
    """Recursive redaction/bounding applied before anything is written to
    disk. Reuses ``browser_agent_tools._redact_value`` — the same
    secret-key patterns and size bounds already relied on for every
    browser-tool response — then additionally scrubs OCID-shaped
    substrings, since a project/tenancy OCID is sensitive configuration
    that ``_redact_value`` has no reason to know about by key name alone."""
    return _scrub_ocids(_redact_value(value))


# =====================================================================
# Environment / package metadata
# =====================================================================

def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def collect_package_versions() -> Dict[str, str]:
    return {name: _package_version(name) for name in RELEVANT_PACKAGES}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =====================================================================
# Individual feature probes
# =====================================================================
# Each probe is async, independent, and takes only (client, model). None of
# them may assume another probe ran first. A probe raising is treated as
# "inconclusive" by _timed() below, never as a crash of the whole run.

async def _timed(feature: str, coro_factory: Callable[[], Awaitable[FeatureResult]]) -> FeatureResult:
    start = time.perf_counter()
    try:
        result = await coro_factory()
    except Exception as exc:  # noqa: BLE001 - a probe failing is a result, not a crash
        result = FeatureResult(
            feature=feature,
            status=ProbeStatus.INCONCLUSIVE,
            diagnostic=bound_diagnostic(f"{type(exc).__name__}: {exc}"),
        )
    result.latencyMs = round((time.perf_counter() - start) * 1000, 1)
    return result


def _classify_param_error(feature: str, param_hint: str, exc: Exception) -> FeatureResult:
    """A request rejected specifically because of the parameter under test
    is 'unsupported'; anything else (auth, transport, an unrelated 400,
    an ambiguous message) is 'inconclusive' — rejection alone does not
    prove the feature itself is unsupported."""
    message = str(exc).lower()
    rejection_hints = ("unknown parameter", "unrecognized", "not supported", "unsupported", "invalid_request_error")
    if param_hint.lower() in message and any(hint in message for hint in rejection_hints):
        return FeatureResult(feature=feature, status=ProbeStatus.UNSUPPORTED, diagnostic=bound_diagnostic(str(exc)))
    return FeatureResult(
        feature=feature, status=ProbeStatus.INCONCLUSIVE,
        diagnostic=bound_diagnostic(f"{type(exc).__name__}: {exc}"),
    )


def _function_calls(response: Any) -> List[Any]:
    return [item for item in (getattr(response, "output", None) or []) if getattr(item, "type", None) == "function_call"]


async def probe_basic_request(client: Any, model: str) -> FeatureResult:
    response = await client.responses.create(model=model, input="Reply with exactly one word: pong")
    text = getattr(response, "output_text", "") or ""
    if "pong" in text.lower():
        return FeatureResult(
            feature="basic_request", status=ProbeStatus.SUPPORTED,
            diagnostic="Received the expected output_text.",
            metadata={"responseId": getattr(response, "id", None)},
        )
    return FeatureResult(
        feature="basic_request", status=ProbeStatus.INCONCLUSIVE,
        diagnostic=bound_diagnostic(f"Unexpected output_text: {text!r}"),
    )


async def probe_structured_output(client: Any, model: str) -> FeatureResult:
    schema = {
        "type": "object", "properties": {"word": {"type": "string"}},
        "required": ["word"], "additionalProperties": False,
    }
    try:
        response = await client.responses.create(
            model=model,
            input="Return the single word 'pong' as structured JSON.",
            text={"format": {"type": "json_schema", "name": "probe_word", "schema": schema, "strict": True}},
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("structured_output", "text.format", exc)
    text = getattr(response, "output_text", "") or ""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return FeatureResult(
            feature="structured_output", status=ProbeStatus.INCONCLUSIVE,
            diagnostic=bound_diagnostic(f"Response was not valid JSON: {text!r}"),
        )
    if isinstance(parsed, dict) and "word" in parsed:
        return FeatureResult(feature="structured_output", status=ProbeStatus.SUPPORTED, diagnostic="Structured JSON matched the requested schema.")
    return FeatureResult(feature="structured_output", status=ProbeStatus.INCONCLUSIVE, diagnostic="Parsed JSON did not match the requested schema shape.")


_WEATHER_TOOL_TEMPLATE: Dict[str, Any] = {
    "type": "function",
    "description": "Return the current weather for a city. Used only by the compatibility probe.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
}


def _weather_tool(name: str, strict: bool) -> Dict[str, Any]:
    tool = dict(_WEATHER_TOOL_TEMPLATE)
    tool["name"] = name
    tool["strict"] = strict
    return tool


async def probe_strict_function_schema(client: Any, model: str) -> FeatureResult:
    tool = _weather_tool("probe_get_weather_strict", strict=True)
    try:
        response = await client.responses.create(
            model=model, input="Call probe_get_weather_strict for the city 'Testville'.",
            tools=[tool], tool_choice="required",
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("strict_function_schema", "strict", exc)
    calls = _function_calls(response)
    if not calls:
        return FeatureResult(feature="strict_function_schema", status=ProbeStatus.INCONCLUSIVE, diagnostic="Model did not issue the requested tool call.")
    try:
        args = json.loads(calls[0].arguments)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return FeatureResult(feature="strict_function_schema", status=ProbeStatus.INCONCLUSIVE, diagnostic="Tool call arguments were not valid JSON.")
    if isinstance(args, dict) and set(args.keys()) <= {"city"} and "city" in args:
        return FeatureResult(feature="strict_function_schema", status=ProbeStatus.SUPPORTED, diagnostic="Strict tool call returned schema-conforming arguments.")
    return FeatureResult(
        feature="strict_function_schema", status=ProbeStatus.INCONCLUSIVE,
        diagnostic=bound_diagnostic(f"Tool call arguments did not match the strict schema: {args!r}"),
    )


async def probe_single_tool_call(client: Any, model: str) -> FeatureResult:
    tool = _weather_tool("probe_get_weather_single", strict=False)
    try:
        response = await client.responses.create(
            model=model, input="Call probe_get_weather_single for the city 'Testville'.",
            tools=[tool], tool_choice="required",
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("single_tool_call", "tools", exc)
    calls = _function_calls(response)
    if calls:
        return FeatureResult(feature="single_tool_call", status=ProbeStatus.SUPPORTED, diagnostic=f"Model issued {len(calls)} tool call(s).")
    return FeatureResult(feature="single_tool_call", status=ProbeStatus.INCONCLUSIVE, diagnostic="Model did not issue any tool call.")


async def probe_parallel_tool_calls(client: Any, model: str) -> FeatureResult:
    tools = [_weather_tool("probe_get_weather_a", strict=False), _weather_tool("probe_get_weather_b", strict=False)]
    try:
        response = await client.responses.create(
            model=model,
            input="Call both probe_get_weather_a and probe_get_weather_b, each for the city 'Testville', in the same turn.",
            tools=tools, tool_choice="required", parallel_tool_calls=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("parallel_tool_calls", "parallel_tool_calls", exc)
    calls = _function_calls(response)
    if len(calls) >= 2:
        return FeatureResult(feature="parallel_tool_calls", status=ProbeStatus.SUPPORTED, diagnostic=f"Model issued {len(calls)} tool calls in one response.")
    return FeatureResult(
        feature="parallel_tool_calls", status=ProbeStatus.INCONCLUSIVE,
        diagnostic=f"parallel_tool_calls was accepted but only {len(calls)} call(s) were issued; cannot confirm parallel execution from model behavior alone.",
    )


async def probe_streaming(client: Any, model: str) -> FeatureResult:
    delta_events = 0
    saw_completed = False
    try:
        stream = await client.responses.create(model=model, input="Count from one to five.", stream=True)
        async for event in stream:
            event_type = getattr(event, "type", "") or ""
            if "delta" in event_type:
                delta_events += 1
            if event_type.endswith("completed"):
                saw_completed = True
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("streaming", "stream", exc)
    if delta_events > 0 and saw_completed:
        return FeatureResult(feature="streaming", status=ProbeStatus.SUPPORTED, diagnostic=f"Received {delta_events} delta event(s) then a completed event.")
    if saw_completed:
        return FeatureResult(feature="streaming", status=ProbeStatus.INCONCLUSIVE, diagnostic="Stream completed but no incremental delta events were observed.")
    return FeatureResult(feature="streaming", status=ProbeStatus.INCONCLUSIVE, diagnostic="Stream never reported a completed event.")


async def probe_usage_reporting(client: Any, model: str) -> FeatureResult:
    response = await client.responses.create(model=model, input="Reply with exactly one word: pong")
    usage = getattr(response, "usage", None)
    if usage is None:
        return FeatureResult(feature="usage_reporting", status=ProbeStatus.UNSUPPORTED, diagnostic="Response had no usage field.")
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if isinstance(input_tokens, int) and isinstance(output_tokens, int) and input_tokens > 0 and output_tokens > 0:
        return FeatureResult(
            feature="usage_reporting", status=ProbeStatus.SUPPORTED,
            diagnostic="Usage reported positive input/output token counts.",
            metadata={"inputTokens": input_tokens, "outputTokens": output_tokens},
        )
    return FeatureResult(feature="usage_reporting", status=ProbeStatus.INCONCLUSIVE, diagnostic="Usage field was present but token counts were missing or zero.")


async def probe_reasoning_controls(client: Any, model: str) -> FeatureResult:
    try:
        response = await client.responses.create(model=model, input="What is 2 + 2?", reasoning={"effort": "low"})
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("reasoning_controls", "reasoning", exc)
    text = getattr(response, "output_text", "") or ""
    if text.strip():
        return FeatureResult(
            feature="reasoning_controls", status=ProbeStatus.INCONCLUSIVE,
            diagnostic="The 'reasoning' parameter was accepted, but its effect cannot be confirmed from the response content alone.",
        )
    return FeatureResult(feature="reasoning_controls", status=ProbeStatus.INCONCLUSIVE, diagnostic="Request with 'reasoning' returned an empty response.")


async def probe_conversation_continuation(client: Any, model: str) -> FeatureResult:
    first = await client.responses.create(model=model, input="Remember the code word BANANA57. Reply with only: OK")
    previous_id = getattr(first, "id", None)
    if not previous_id:
        return FeatureResult(feature="conversation_continuation", status=ProbeStatus.INCONCLUSIVE, diagnostic="First response had no id to continue from.")
    try:
        second = await client.responses.create(
            model=model, input="What was the code word? Reply with only the code word.",
            previous_response_id=previous_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("conversation_continuation", "previous_response_id", exc)
    text = (getattr(second, "output_text", "") or "").upper()
    if "BANANA57" in text:
        return FeatureResult(feature="conversation_continuation", status=ProbeStatus.SUPPORTED, diagnostic="Second response correctly recalled context via previous_response_id.")
    return FeatureResult(feature="conversation_continuation", status=ProbeStatus.INCONCLUSIVE, diagnostic="previous_response_id was accepted but continuation could not be confirmed from the response content.")


async def probe_background_execution(client: Any, model: str) -> FeatureResult:
    try:
        response = await client.responses.create(model=model, input="Count from one to five slowly.", background=True)
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("background_execution", "background", exc)
    status = getattr(response, "status", None)
    response_id = getattr(response, "id", None)
    if status not in ("queued", "in_progress") or not response_id:
        return FeatureResult(feature="background_execution", status=ProbeStatus.INCONCLUSIVE, diagnostic=f"background=True was accepted but initial status was {status!r}.")
    for _ in range(5):
        await asyncio.sleep(1)
        polled = await client.responses.retrieve(response_id)
        if getattr(polled, "status", None) == "completed":
            return FeatureResult(
                feature="background_execution", status=ProbeStatus.SUPPORTED,
                diagnostic="Background response reached completed status via polling.",
            )
    return FeatureResult(feature="background_execution", status=ProbeStatus.INCONCLUSIVE, diagnostic="Background response did not complete within the bounded polling window.")


async def probe_cancellation(client: Any, model: str) -> FeatureResult:
    try:
        response = await client.responses.create(model=model, input="Write a very long, slow, detailed essay about clouds.", background=True)
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("cancellation", "background", exc)
    response_id = getattr(response, "id", None)
    if not response_id:
        return FeatureResult(feature="cancellation", status=ProbeStatus.INCONCLUSIVE, diagnostic="No background response id was returned; cancellation cannot be probed without one.")
    try:
        cancelled = await client.responses.cancel(response_id)
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("cancellation", "cancel", exc)
    if getattr(cancelled, "status", None) == "cancelled":
        return FeatureResult(feature="cancellation", status=ProbeStatus.SUPPORTED, diagnostic="Background response reported status=cancelled after cancel().")
    return FeatureResult(
        feature="cancellation", status=ProbeStatus.INCONCLUSIVE,
        diagnostic=f"cancel() was accepted but the resulting status was {getattr(cancelled, 'status', None)!r}.",
    )


async def probe_provider_native_compaction(client: Any, model: str) -> FeatureResult:
    try:
        await client.responses.create(model=model, input="Say hello.", truncation="auto")
    except Exception as exc:  # noqa: BLE001
        return _classify_param_error("provider_native_compaction", "truncation", exc)
    return FeatureResult(
        feature="provider_native_compaction", status=ProbeStatus.INCONCLUSIVE,
        diagnostic=(
            "The 'truncation' parameter was accepted, but no provider-exposed signal "
            "confirms server-side context compaction actually occurred; an accepted "
            "request is not evidence of support."
        ),
    )


FEATURE_PROBES: Dict[str, Callable[[Any, str], Awaitable[FeatureResult]]] = {
    "basic_request": probe_basic_request,
    "structured_output": probe_structured_output,
    "strict_function_schema": probe_strict_function_schema,
    "single_tool_call": probe_single_tool_call,
    "parallel_tool_calls": probe_parallel_tool_calls,
    "streaming": probe_streaming,
    "usage_reporting": probe_usage_reporting,
    "reasoning_controls": probe_reasoning_controls,
    "conversation_continuation": probe_conversation_continuation,
    "background_execution": probe_background_execution,
    "cancellation": probe_cancellation,
    "provider_native_compaction": probe_provider_native_compaction,
}


# =====================================================================
# Orchestration
# =====================================================================

async def run_feature_probes(
    client: Any, model: str, probes: Optional[Dict[str, Callable[[Any, str], Awaitable[FeatureResult]]]] = None,
) -> List[FeatureResult]:
    """Run every feature probe independently. One probe raising never stops
    the rest — see :func:`_timed`, which turns an exception into an
    INCONCLUSIVE result instead of propagating it."""
    probes = FEATURE_PROBES if probes is None else probes
    return [await _timed(name, lambda fn=fn: fn(client, model)) for name, fn in probes.items()]


def run_feature_probes_sync(
    client: Any, model: str, probes: Optional[Dict[str, Callable[[Any, str], Awaitable[FeatureResult]]]] = None,
) -> List[FeatureResult]:
    return asyncio.run(run_feature_probes(client, model, probes))


async def _close_client(client: Any) -> None:
    inner = getattr(client, "_client", None)
    aclose = getattr(inner, "aclose", None) or getattr(client, "aclose", None) or getattr(client, "close", None)
    if aclose is None:
        return
    try:
        result = aclose()
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


async def run_probe(environment: str = "local") -> ProbeReport:
    """Build the full :class:`ProbeReport`. Never raises: missing
    configuration or a client-construction failure becomes one INCONCLUSIVE
    result per feature instead of an exception, so the probe always exits
    cleanly without live credentials (Phase 0 requirement)."""
    base_kwargs = dict(
        executedAtUtc=_utc_now_iso(), environment=environment,
        pythonVersion=platform.python_version(), packageVersions=collect_package_versions(),
    )

    try:
        config: ProviderConfig = load_provider_config()
    except ProviderConfigError as exc:
        results = [
            FeatureResult(feature=name, status=ProbeStatus.INCONCLUSIVE, diagnostic=f"Provider configuration missing: {exc}")
            for name in FEATURE_PROBES
        ]
        return ProbeReport(**base_kwargs, results=results, warnings=["Live probe skipped: provider configuration missing."])

    base_kwargs.update(region=config.region, endpointHost=config.endpoint_host, modelAlias=config.model, apiVersion=config.api_version)

    try:
        client = build_async_openai_client(config)
    except Exception as exc:  # noqa: BLE001
        results = [
            FeatureResult(feature=name, status=ProbeStatus.INCONCLUSIVE, diagnostic=f"Could not construct provider client: {exc}")
            for name in FEATURE_PROBES
        ]
        return ProbeReport(**base_kwargs, results=results, warnings=["Live probe skipped: provider client could not be constructed."])

    try:
        results = await run_feature_probes(client, config.model)
    finally:
        await _close_client(client)

    warnings = []
    if any(r.status == ProbeStatus.INCONCLUSIVE for r in results):
        warnings.append("One or more features returned inconclusive; see individual diagnostics.")

    return ProbeReport(**base_kwargs, results=results, warnings=warnings)


def run_probe_sync(environment: str = "local") -> ProbeReport:
    return asyncio.run(run_probe(environment))


def sanitize_report(report: ProbeReport) -> Dict[str, Any]:
    return sanitize_for_report(json.loads(report.model_dump_json()))


def build_run_manifest(report: ProbeReport, browser_contract_version: str, browser_contract_hash: str) -> RunManifest:
    return RunManifest(
        browserContractVersion=browser_contract_version,
        browserContractHash=browser_contract_hash,
        modelAlias=report.modelAlias,
        endpointHost=report.endpointHost,
        region=report.region,
        apiVersion=report.apiVersion,
        dependencyVersions=report.packageVersions,
        createdAtUtc=_utc_now_iso(),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OCI/Grok Responses-API compatibility probe.")
    parser.add_argument("--environment", default="local", help="Label recorded in the report (e.g. local, ci, staging).")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_PATH), help="Where to write the sanitized JSON report.")
    args = parser.parse_args(argv)

    report = run_probe_sync(environment=args.environment)
    sanitized = sanitize_report(report)

    contract_hash = compute_contract_hash(build_contract())
    manifest = build_run_manifest(report, BROWSER_CONTRACT_VERSION, contract_hash)
    sanitized["runManifest"] = json.loads(manifest.model_dump_json())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    supported = sum(1 for r in report.results if r.status == ProbeStatus.SUPPORTED)
    print(f"Wrote {out_path} ({supported}/{len(report.results)} feature(s) supported).")
    for result in report.results:
        print(f"  {result.feature:28s} {result.status.value:12s} {result.diagnostic[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
