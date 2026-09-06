"""Phase 0 baseline tests: browser-tool contract freeze + provider probe safety.

Does not duplicate tests/test_browser_agent_tools.py's 114 tests — see that
file for full tool-behavior coverage. This file proves only the Phase 0
deliverables: the seven-tool contract is frozen and verifiable, it is never
silently rewritten, model-facing schemas leak no raw bridge identifiers, and
the compatibility-probe/report/manifest machinery is safe to run without
live provider credentials.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = AGENT_DIR / "scripts"
for path in (AGENT_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import browser_agent_tools as bat  # noqa: E402
import browser_contract  # noqa: E402
import provider_compatibility_probe as probe  # noqa: E402
import provider_config  # noqa: E402
from provider_config import ProviderConfigError, load_provider_config  # noqa: E402

EXPECTED_TOOL_NAMES = (
    "browser_observe",
    "browser_act",
    "browser_navigate",
    "browser_wait",
    "browser_assert",
    "browser_capture_evidence",
    "browser_diagnose",
)

# Identifiers/selectors the model must never be able to supply directly —
# these are resolved internally from browserSessionId/ref/locator instead
# (see browser_agent_tools.py's module docstring).
FORBIDDEN_INPUT_PROPERTY_NAMES = frozenset({
    "tabId", "windowId", "groupId", "frameId", "bridgeSessionId",
    "bridgeMethod", "method", "rpcMethod", "snapshotId",
})

_PROVIDER_ENV_VARS = (
    "AXIS_OCI_GENAI_BASE_URL",
    "AXIS_OCI_GENAI_REGION",
    "AXIS_OCI_GENAI_MODEL",
    "AXIS_OCI_GENAI_PROJECT_OCID",
)


def _iter_schema_property_names(schema):
    """Yield every property name anywhere in a JSON-Schema object, recursing
    into nested object 'properties' and array 'items'."""
    if not isinstance(schema, dict):
        return
    for name, subschema in (schema.get("properties") or {}).items():
        yield name
        yield from _iter_schema_property_names(subschema)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _iter_schema_property_names(items)


class _EnvSandbox:
    """Removes/sets provider env vars for one test, restoring the prior
    environment afterward regardless of outcome."""

    def __init__(self, remove=(), set_values=None):
        self._remove = remove
        self._set_values = set_values or {}
        self._backup = {}

    def __enter__(self):
        for key in self._remove:
            self._backup[key] = os.environ.pop(key, None)
        for key, value in self._set_values.items():
            self._backup.setdefault(key, os.environ.get(key))
            os.environ[key] = value
        return self

    def __exit__(self, *exc_info):
        for key, value in self._backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# 1. The seven-tool contract, as exposed today
# ---------------------------------------------------------------------------

class TestSevenToolContract(unittest.TestCase):
    def test_exactly_seven_tools_exposed(self):
        self.assertEqual(len(bat.get_tool_definitions()), 7)

    def test_tool_names_unchanged(self):
        names = tuple(tool["function"]["name"] for tool in bat.get_tool_definitions())
        self.assertEqual(names, EXPECTED_TOOL_NAMES)

    def test_no_raw_bridge_method_selection_in_schemas(self):
        for tool in bat.get_tool_definitions():
            props = set(_iter_schema_property_names(tool["function"]["parameters"]))
            leaked = props & {"method", "bridgeMethod", "rpcMethod"}
            self.assertFalse(leaked, f"{tool['function']['name']} exposes raw method selection: {leaked}")

    def test_no_raw_bridge_identifiers_in_schemas(self):
        for tool in bat.get_tool_definitions():
            props = set(_iter_schema_property_names(tool["function"]["parameters"]))
            leaked = props & FORBIDDEN_INPUT_PROPERTY_NAMES
            self.assertFalse(leaked, f"{tool['function']['name']} exposes raw identifiers: {leaked}")


# ---------------------------------------------------------------------------
# 2. The frozen contract snapshot
# ---------------------------------------------------------------------------

class TestContractSnapshot(unittest.TestCase):
    def test_generated_contract_matches_committed_snapshot(self):
        generated = browser_contract.generate_snapshot()
        committed = browser_contract.load_committed_snapshot()
        self.assertIsNotNone(
            committed,
            "No committed snapshot found; run "
            "'python scripts/browser_contract.py --update' once and commit the result.",
        )
        self.assertEqual(generated, committed)

    def test_contract_drift_produces_a_readable_message(self):
        generated = browser_contract.generate_snapshot()
        drifted = copy.deepcopy(generated)
        drifted["tools"][0]["function"]["name"] = "browser_observe_renamed"
        message = browser_contract.format_drift_report(generated, drifted)
        self.assertIn("drift", message.lower())
        self.assertIn("browser_observe_renamed", message)

    def test_no_drift_produces_an_empty_message(self):
        generated = browser_contract.generate_snapshot()
        self.assertEqual(browser_contract.format_drift_report(generated, generated), "")

    def test_missing_snapshot_produces_a_readable_message(self):
        generated = browser_contract.generate_snapshot()
        message = browser_contract.format_drift_report(None, generated)
        self.assertIn("--update", message)

    def test_verify_does_not_rewrite_the_committed_file(self):
        path = browser_contract.CONTRACT_PATH
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        self.assertTrue(browser_contract.verify())
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_contract_hash_is_deterministic(self):
        first = browser_contract.generate_snapshot()
        second = browser_contract.generate_snapshot()
        self.assertEqual(first["contractHash"], second["contractHash"])
        self.assertEqual(len(first["contractHash"]), 64)  # sha256 hex digest

    def test_contract_hash_changes_when_content_changes(self):
        contract = browser_contract.build_contract()
        hash_a = browser_contract.compute_contract_hash(contract)
        mutated = copy.deepcopy(contract)
        mutated["tools"][0]["function"]["description"] += " (mutated)"
        hash_b = browser_contract.compute_contract_hash(mutated)
        self.assertNotEqual(hash_a, hash_b)

    def test_error_codes_and_forbidden_methods_are_present_and_sorted(self):
        contract = browser_contract.build_contract()
        self.assertIn("SCOPE_DENIED", contract["errorCodes"])
        self.assertIn("cookies.get", contract["forbiddenBridgeMethods"])
        self.assertEqual(contract["errorCodes"], sorted(contract["errorCodes"]))
        self.assertEqual(contract["forbiddenBridgeMethods"], sorted(contract["forbiddenBridgeMethods"]))

    def test_response_envelope_shape_is_documented(self):
        contract = browser_contract.build_contract()
        envelope = contract["responseEnvelope"]
        self.assertEqual(set(envelope.keys()), {"ok", "browserSessionId", "data", "error"})
        self.assertEqual(set(envelope["error"].keys()), {"code", "message", "retryable", "diagnostic"})


# ---------------------------------------------------------------------------
# 3. Provider configuration
# ---------------------------------------------------------------------------

class TestProviderConfig(unittest.TestCase):
    def test_missing_configuration_raises_a_clear_error(self):
        with _EnvSandbox(remove=_PROVIDER_ENV_VARS):
            with self.assertRaises(ProviderConfigError):
                load_provider_config()

    def test_region_derives_the_default_base_url(self):
        with _EnvSandbox(
            remove=("AXIS_OCI_GENAI_BASE_URL",),
            set_values={
                "AXIS_OCI_GENAI_REGION": "us-ashburn-1",
                "AXIS_OCI_GENAI_MODEL": "test-model",
                "AXIS_OCI_GENAI_PROJECT_OCID": "ocid1.generativeaiproject.oc1..testonly",
            },
        ):
            config = load_provider_config()
        self.assertIn("us-ashburn-1", config.base_url)
        self.assertEqual(config.endpoint_host, "inference.generativeai.us-ashburn-1.oci.oraclecloud.com")

    def test_dotenv_file_populates_missing_env_vars(self):
        dotenv_path = AGENT_DIR / "_test_dotenv_fixture.env"
        dotenv_path.write_text(
            "# a comment\n"
            "\n"
            "AXIS_OCI_GENAI_REGION=from-dotenv-region\n"
            "AXIS_OCI_GENAI_MODEL='from-dotenv-model'\n",
            encoding="utf-8",
        )
        try:
            with _EnvSandbox(remove=("AXIS_OCI_GENAI_REGION", "AXIS_OCI_GENAI_MODEL")):
                provider_config._load_dotenv(dotenv_path)
                self.assertEqual(os.environ.get("AXIS_OCI_GENAI_REGION"), "from-dotenv-region")
                self.assertEqual(os.environ.get("AXIS_OCI_GENAI_MODEL"), "from-dotenv-model")
        finally:
            dotenv_path.unlink(missing_ok=True)

    def test_dotenv_file_never_overrides_a_real_env_var(self):
        dotenv_path = AGENT_DIR / "_test_dotenv_fixture.env"
        dotenv_path.write_text("AXIS_OCI_GENAI_REGION=from-dotenv-region\n", encoding="utf-8")
        try:
            with _EnvSandbox(set_values={"AXIS_OCI_GENAI_REGION": "real-env-region"}):
                provider_config._load_dotenv(dotenv_path)
                self.assertEqual(os.environ.get("AXIS_OCI_GENAI_REGION"), "real-env-region")
        finally:
            dotenv_path.unlink(missing_ok=True)

    def test_missing_dotenv_file_is_not_an_error(self):
        provider_config._load_dotenv(AGENT_DIR / "_does_not_exist.env")  # must not raise

    def test_explicit_base_url_overrides_region(self):
        with _EnvSandbox(
            set_values={
                "AXIS_OCI_GENAI_BASE_URL": "https://example.invalid/openai/v1",
                "AXIS_OCI_GENAI_MODEL": "test-model",
                "AXIS_OCI_GENAI_PROJECT_OCID": "ocid1.generativeaiproject.oc1..testonly",
            },
        ):
            config = load_provider_config()
        self.assertEqual(config.endpoint_host, "example.invalid")


# ---------------------------------------------------------------------------
# 4. Probe result / report / manifest models
# ---------------------------------------------------------------------------

class TestProbeResultModels(unittest.TestCase):
    def test_feature_result_rejects_an_invalid_status(self):
        with self.assertRaises(Exception):
            probe.FeatureResult(feature="basic_request", status="not-a-real-status", diagnostic="x")

    def test_feature_result_accepts_a_valid_status(self):
        result = probe.FeatureResult(feature="basic_request", status=probe.ProbeStatus.SUPPORTED, diagnostic="ok")
        self.assertEqual(result.status, probe.ProbeStatus.SUPPORTED)

    def test_probe_report_serializes(self):
        report = probe.ProbeReport(
            executedAtUtc="2026-01-01T00:00:00Z",
            environment="test",
            pythonVersion="3.12.0",
            packageVersions={"openai": "3.8.0"},
            results=[probe.FeatureResult(feature="basic_request", status=probe.ProbeStatus.INCONCLUSIVE, diagnostic="no config")],
        )
        payload = json.loads(report.model_dump_json())
        self.assertEqual(payload["results"][0]["feature"], "basic_request")
        self.assertEqual(payload["results"][0]["status"], "inconclusive")

    def test_run_manifest_serializes(self):
        manifest = probe.RunManifest(
            browserContractVersion="1.0.0",
            browserContractHash="a" * 64,
            dependencyVersions={"openai": "3.8.0"},
            createdAtUtc="2026-01-01T00:00:00Z",
        )
        payload = json.loads(manifest.model_dump_json())
        self.assertEqual(payload["manifestSchemaVersion"], probe.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(payload["browserContractHash"], "a" * 64)


# ---------------------------------------------------------------------------
# 5. Sanitization / bounding
# ---------------------------------------------------------------------------

class TestSanitization(unittest.TestCase):
    def test_secret_like_keys_are_redacted(self):
        raw = {
            "authorization": "Bearer abc",
            "api_key": "sk-123",
            "nested": {"password": "hunter2"},
            "safe": "keep-me",
        }
        sanitized = probe.sanitize_for_report(raw)
        self.assertEqual(sanitized["authorization"], bat.REDACTED)
        self.assertEqual(sanitized["api_key"], bat.REDACTED)
        self.assertEqual(sanitized["nested"]["password"], bat.REDACTED)
        self.assertEqual(sanitized["safe"], "keep-me")

    def test_ocid_like_values_are_redacted(self):
        sanitized = probe.sanitize_for_report({"note": "project ocid1.generativeaiproject.oc1.iad.exampleexampleexample"})
        self.assertNotIn("oc1.iad.exampleexampleexample", sanitized["note"])
        self.assertIn("<redacted-ocid>", sanitized["note"])

    def test_diagnostic_is_bounded(self):
        huge = "x" * 10_000
        bounded = probe.bound_diagnostic(huge)
        self.assertLess(len(bounded), 10_000)
        self.assertIn("truncated", bounded)

    def test_short_diagnostic_is_unchanged(self):
        self.assertEqual(probe.bound_diagnostic("short message"), "short message")


# ---------------------------------------------------------------------------
# 6. Probe orchestration safety (no live credentials required)
# ---------------------------------------------------------------------------

class TestProbeRunsWithoutLiveCredentials(unittest.TestCase):
    def test_probe_reports_inconclusive_when_configuration_is_missing(self):
        with _EnvSandbox(remove=_PROVIDER_ENV_VARS):
            report = probe.run_probe_sync(environment="test")
        self.assertEqual(len(report.results), len(probe.FEATURE_PROBES))
        self.assertTrue(all(r.status == probe.ProbeStatus.INCONCLUSIVE for r in report.results))
        self.assertTrue(any("configuration" in r.diagnostic.lower() for r in report.results))
        self.assertTrue(any("skipped" in w.lower() for w in report.warnings))

    def test_report_is_json_serializable_and_sanitizable_without_live_access(self):
        with _EnvSandbox(remove=_PROVIDER_ENV_VARS):
            report = probe.run_probe_sync(environment="test")
        sanitized = probe.sanitize_report(report)
        json.dumps(sanitized)  # must not raise

    def test_one_failing_probe_does_not_stop_the_others(self):
        async def boom(client, model):
            raise RuntimeError("simulated failure")

        async def ok(client, model):
            return probe.FeatureResult(feature="ok_feature", status=probe.ProbeStatus.SUPPORTED, diagnostic="fine")

        results = probe.run_feature_probes_sync(
            client=None, model="test-model",
            probes={"boom_feature": boom, "ok_feature": ok},
        )
        by_name = {r.feature: r for r in results}
        self.assertEqual(len(results), 2)
        self.assertEqual(by_name["ok_feature"].status, probe.ProbeStatus.SUPPORTED)
        self.assertEqual(by_name["boom_feature"].status, probe.ProbeStatus.INCONCLUSIVE)
        self.assertIn("simulated failure", by_name["boom_feature"].diagnostic)


if __name__ == "__main__":
    unittest.main()
