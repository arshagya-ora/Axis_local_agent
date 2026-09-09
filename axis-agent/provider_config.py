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
from dataclasses import dataclass, field
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
    project_ocid: Optional[str]
    oci_profile: str
    region: Optional[str]
    api_version: Optional[str]
    # Set only in API-key mode. `repr=False` keeps it out of tracebacks,
    # probe reports, and anything else that stringifies this dataclass — the
    # value is a live credential.
    api_key: Optional[str] = field(default=None, repr=False)

    @property
    def endpoint_host(self) -> Optional[str]:
        return urlsplit(self.base_url).hostname

    @property
    def auth_mode(self) -> str:
        """``"api_key"`` when a key was supplied, else ``"oci_signing"``.

        These are the only two ways to reach the endpoint, and they are
        mutually exclusive: a key means plain bearer auth and no OCI request
        signing, so no ``~/.oci/config`` and no private key are needed.
        """
        return "api_key" if self.api_key else "oci_signing"


def _env(*names: str) -> Optional[str]:
    """First of ``names`` that is set to a non-blank value.

    Blank matters: ``.env.example`` ships keys with empty values, and an
    empty ``AXIS_API_KEY=`` must read as "not set" rather than selecting key
    auth and then failing with a 401 at request time.
    """
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def load_provider_config() -> ProviderConfig:
    """Read provider configuration from the environment, in one of two modes.

    **API-key mode** — selected by setting ``AXIS_API_KEY``. The key is sent
    as a plain bearer token, so no ``~/.oci/config``, no private key, and no
    OCI profile are involved. This is the path for someone who was handed a
    key rather than an OCI signing identity.

    **OCI-signing mode** — the default when no key is set. Unchanged: each
    request is signed via ``oci_openai.OciUserPrincipalAuth``, which reads
    the OCI CLI config itself. This function still never reads or returns
    the signing key.

    ``AXIS_BASE_URL``/``AXIS_MODEL`` are provider-neutral names that work in
    either mode and take precedence over the ``AXIS_OCI_GENAI_*`` spellings,
    so an endpoint that isn't OCI doesn't have to be configured through
    OCI-shaped variable names.

    Raises :class:`ProviderConfigError` with a clear, actionable message if a
    required variable is missing.
    """
    api_key = _env("AXIS_API_KEY")
    region = _env("AXIS_OCI_GENAI_REGION")
    base_url = _env("AXIS_BASE_URL", "AXIS_OCI_GENAI_BASE_URL")
    if not base_url:
        if not region:
            raise ProviderConfigError(
                "Missing provider configuration: set AXIS_BASE_URL (the full endpoint "
                "URL), or set AXIS_OCI_GENAI_REGION so the default OCI GenAI endpoint "
                "can be derived from it."
            )
        base_url = f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1"

    model = _env("AXIS_MODEL", "AXIS_OCI_GENAI_MODEL")
    if not model:
        raise ProviderConfigError(
            "Missing provider configuration: set AXIS_MODEL (the model alias/id, "
            "e.g. 'openai.gpt-5.6-sol')."
        )

    # Required only for signing mode. With a key the OCI GenAI endpoint still
    # accepts a project OCID and AXIS forwards it when set, but an endpoint
    # that has no concept of one must not be forced to invent it.
    project_ocid = _env("AXIS_OCI_GENAI_PROJECT_OCID")
    if not project_ocid and not api_key:
        raise ProviderConfigError(
            "Missing provider configuration: set AXIS_OCI_GENAI_PROJECT_OCID, or set "
            "AXIS_API_KEY to authenticate with an API key instead of OCI request signing."
        )

    oci_profile = os.environ.get("AXIS_OCI_PROFILE", "DEFAULT")
    # Optional: the Responses API does not report its own version in a
    # standard field, so this is only ever populated when an operator
    # already knows it and wants it recorded in the probe report/manifest.
    api_version = _env("AXIS_OCI_GENAI_API_VERSION")

    return ProviderConfig(
        base_url=base_url,
        model=model,
        project_ocid=project_ocid,
        oci_profile=oci_profile,
        region=region,
        api_version=api_version,
        api_key=api_key,
    )


def build_async_openai_client(config: ProviderConfig):
    """Build the ``openai.AsyncOpenAI`` client for the configured endpoint.

    Shared by ``axis/agents.py`` and ``provider_compatibility_probe.py`` so
    the auth/HTTP/client wiring exists in exactly one place. Which of the two
    auth modes runs is decided entirely by ``config.auth_mode``; nothing
    upstream of here has to know the difference.
    """
    from openai import AsyncOpenAI

    if config.auth_mode == "api_key":
        # Plain bearer auth: no httpx auth hook, no OCI profile, no signing
        # key on disk. `project` is forwarded only when set, because a
        # non-OCI endpoint would reject the header outright.
        return AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            project=config.project_ocid,
            max_retries=0,
        )

    import httpx
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
