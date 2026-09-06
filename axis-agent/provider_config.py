"""Environment-based provider configuration for the OCI/Grok POC and the
Phase 0 compatibility probe.

Deliberately tiny: one config loader, one client-bootstrap helper, one
exception. Phase 0 needs just enough indirection to get the endpoint,
model, and project identifiers out of ``tests/pydandic_agents_test.py`` and
``scripts/provider_compatibility_probe.py`` and into the environment (see
``PHASE_0_BASELINE.md``) — nothing else. This is not a configuration
framework; there is exactly one place that reads these variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# axis-agent/.env — a plain KEY=VALUE file for local development so the
# AXIS_OCI_* (and, incidentally, BROWSER_AGENT_BRIDGE_*) variables don't
# need to be exported by hand every session. See .env.example for the full
# list of keys. Deliberately not python-dotenv: this is ~15 lines covering
# exactly the KEY=VALUE / #comment / blank-line subset this project needs,
# not a general-purpose parser.
_DOTENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_dotenv(path: Path = _DOTENV_PATH) -> None:
    """Populate os.environ from a .env file, without overriding a variable
    the environment already has (a real `export FOO=bar` always wins over
    the file). Missing file is not an error — most CI/production
    environments will have no .env at all and rely on real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class ProviderConfigError(RuntimeError):
    """Raised when required provider configuration is missing or invalid."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    project_ocid: str
    oci_profile: str
    region: Optional[str]
    api_version: Optional[str]

    @property
    def endpoint_host(self) -> Optional[str]:
        return urlsplit(self.base_url).hostname


def load_provider_config() -> ProviderConfig:
    """Read OCI/Grok provider configuration from the environment.

    Raises :class:`ProviderConfigError` with a clear, actionable message if
    a required variable is missing. Never reads or returns credentials —
    those stay in ``oci_openai.OciUserPrincipalAuth``'s own handling of the
    OCI CLI config (``~/.oci/config``), exactly as before this change.
    """
    region = os.environ.get("AXIS_OCI_GENAI_REGION")
    base_url = os.environ.get("AXIS_OCI_GENAI_BASE_URL")
    if not base_url:
        if not region:
            raise ProviderConfigError(
                "Missing provider configuration: set AXIS_OCI_GENAI_BASE_URL (the full "
                "endpoint URL), or set AXIS_OCI_GENAI_REGION so the default OCI GenAI "
                "endpoint can be derived from it."
            )
        base_url = f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1"

    model = os.environ.get("AXIS_OCI_GENAI_MODEL")
    if not model:
        raise ProviderConfigError(
            "Missing provider configuration: set AXIS_OCI_GENAI_MODEL (the model alias/id, "
            "e.g. 'openai.gpt-5.4-mini')."
        )

    project_ocid = os.environ.get("AXIS_OCI_GENAI_PROJECT_OCID")
    if not project_ocid:
        raise ProviderConfigError(
            "Missing provider configuration: set AXIS_OCI_GENAI_PROJECT_OCID."
        )

    oci_profile = os.environ.get("AXIS_OCI_PROFILE", "DEFAULT")
    # Optional: the Responses API does not report its own version in a
    # standard field, so this is only ever populated when an operator
    # already knows it and wants it recorded in the probe report/manifest.
    api_version = os.environ.get("AXIS_OCI_GENAI_API_VERSION")

    return ProviderConfig(
        base_url=base_url,
        model=model,
        project_ocid=project_ocid,
        oci_profile=oci_profile,
        region=region,
        api_version=api_version,
    )


def build_async_openai_client(config: ProviderConfig):
    """Build the ``openai.AsyncOpenAI`` client used against OCI's
    Responses-API-compatible endpoint. Shared by ``pydandic_agents_test.py``
    and ``provider_compatibility_probe.py`` so this OCI auth/HTTP/client
    wiring exists in exactly one place instead of being duplicated."""
    import httpx
    from openai import AsyncOpenAI
    from oci_openai import OciUserPrincipalAuth

    oci_auth = OciUserPrincipalAuth(profile_name=config.oci_profile)
    http_client = httpx.AsyncClient(auth=oci_auth)
    return AsyncOpenAI(
        base_url=config.base_url,
        api_key="unused",
        project=config.project_ocid,
        http_client=http_client,
        max_retries=0,
    )
