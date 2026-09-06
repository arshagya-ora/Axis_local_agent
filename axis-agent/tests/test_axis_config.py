"""Deterministic tests for axis/config.py (axis.yaml loading/validation).

No Chrome, no OCI credentials, no live bridge — pure YAML parsing and
validation against a mapping already loaded in memory (or a temp file for
the file-loading path).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from axis.config import (  # noqa: E402
    AxisConfigError,
    load_axis_config,
    parse_axis_config,
)
from browser_agent_tools import FirewallConfig  # noqa: E402

MINIMAL_VALID = {
    "browser": {},
    "limits": {},
}


class TestParseAxisConfig(unittest.TestCase):
    def test_minimal_config_uses_documented_defaults(self):
        config = parse_axis_config(MINIMAL_VALID)
        self.assertTrue(config.browser.whole_browser_access)
        self.assertEqual(config.browser.initial_tab, "focused")
        self.assertEqual(config.browser.max_open_tabs, 30)
        self.assertEqual(config.limits.max_requests, 20)
        self.assertEqual(config.max_retries, 3)
        self.assertEqual(config.cli.trace, "verbose")

    def test_real_axis_yaml_loads_and_matches_documented_values(self):
        config = load_axis_config()
        self.assertTrue(config.browser.whole_browser_access)
        self.assertEqual(config.browser.max_open_tabs, 30)
        self.assertEqual(config.limits.max_requests, 60)
        self.assertEqual(config.limits.max_tool_calls, 120)
        self.assertEqual(config.limits.max_total_tokens, 500_000)
        self.assertEqual(config.limits.max_wall_clock_seconds, 900)
        self.assertEqual(config.max_retries, 3)
        self.assertIn("cookies.get", config.browser.firewall.deny_methods)
        self.assertIn("chrome", config.browser.firewall.deny_schemes)

    def test_missing_required_section_is_a_controlled_error(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"limits": {}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}})

    def test_unknown_top_level_key_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "unexpected": 1})

    def test_unknown_nested_key_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"unexpected": 1}, "limits": {}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {"unexpected": 1}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"firewall": {"unexpected": 1}}, "limits": {}})

    def test_invalid_type_is_a_controlled_error(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"max_open_tabs": "thirty"}, "limits": {}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": "not-a-mapping", "limits": {}})

    def test_negative_or_zero_limit_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {"max_requests": 0}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {"max_tool_calls": -5}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {"max_retries": 0}})

    def test_null_disables_each_individual_limit(self):
        config = parse_axis_config({
            "browser": {},
            "limits": {
                "max_requests": None, "max_tool_calls": None,
                "max_total_tokens": None, "max_wall_clock_seconds": None,
            },
        })
        self.assertIsNone(config.limits.max_requests)
        self.assertIsNone(config.limits.max_tool_calls)
        self.assertIsNone(config.limits.max_total_tokens)
        self.assertIsNone(config.limits.max_wall_clock_seconds)

    def test_max_open_tabs_and_max_retries_must_be_positive_integers(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"max_open_tabs": 0}, "limits": {}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"max_open_tabs": -1}, "limits": {}})
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {"max_retries": "three"}})

    def test_firewall_section_parses_into_firewall_config(self):
        config = parse_axis_config({
            "browser": {"firewall": {
                "default": "deny",
                "allow_urls": ["https://example.com/*"],
                "deny_urls": ["*://blocked.example/*"],
                "deny_schemes": ["chrome"],
                "allow_methods": ["page.navigate"],
                "deny_methods": ["cookies.get"],
            }},
            "limits": {},
        })
        self.assertIsInstance(config.browser.firewall, FirewallConfig)
        self.assertEqual(config.browser.firewall.default, "deny")
        self.assertEqual(config.browser.firewall.allow_urls, ("https://example.com/*",))
        self.assertEqual(config.browser.firewall.deny_methods, ("cookies.get",))

    def test_invalid_firewall_default_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {"firewall": {"default": "sometimes"}}, "limits": {}})

    def test_invalid_cli_trace_value_is_rejected(self):
        with self.assertRaises(AxisConfigError):
            parse_axis_config({"browser": {}, "limits": {}, "cli": {"trace": "loud"}})


class TestLoadAxisConfigFromFile(unittest.TestCase):
    def test_missing_file_is_a_controlled_error(self):
        with self.assertRaises(AxisConfigError):
            load_axis_config(Path("this/file/does/not/exist.yaml"))

    def test_invalid_yaml_is_a_controlled_startup_failure(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp.write("browser: [this is not: valid: yaml\n")
            path = Path(tmp.name)
        try:
            with self.assertRaises(AxisConfigError):
                load_axis_config(path)
        finally:
            path.unlink()

    def test_empty_file_is_a_controlled_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with self.assertRaises(AxisConfigError):
                load_axis_config(path)
        finally:
            path.unlink()

    def test_axis_config_path_env_var_overrides_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp.write("browser: {max_open_tabs: 7}\nlimits: {max_requests: 5}\n")
            path = tmp.name
        backup = os.environ.get("AXIS_CONFIG_PATH")
        try:
            os.environ["AXIS_CONFIG_PATH"] = path
            config = load_axis_config()
            self.assertEqual(config.browser.max_open_tabs, 7)
            self.assertEqual(config.limits.max_requests, 5)
        finally:
            if backup is None:
                os.environ.pop("AXIS_CONFIG_PATH", None)
            else:
                os.environ["AXIS_CONFIG_PATH"] = backup
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
