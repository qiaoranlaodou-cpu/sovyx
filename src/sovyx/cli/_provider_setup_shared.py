"""Shared provider-setup helpers (Mission C6 §T3.4).

Extracted from ``dashboard/routes/onboarding.py:387-505`` so both the
dashboard onboarding endpoints AND the CLI ``sovyx llm setup`` wizard
consume the same validation + persistence code path.

Anti-pattern #20 compliance: the original ``onboarding.py`` re-exports
the legacy underscore-prefixed names (``_create_provider`` etc.) so
test patches against the legacy paths continue to work. This module is
the SOURCE OF TRUTH; ``onboarding.py`` is a thin adaptor.

Public API:

* :func:`create_provider` — instantiate a provider class by canonical
  name + API key. Returns ``None`` on failure.
* :func:`test_provider` — async — minimal generation against a transient
  provider to validate the key.
* :func:`persist_api_key` — write the env-var + key pair into
  ``<data_dir>/secrets.env`` with idempotent replace-or-append. Sets
  ``0o600`` permissions where the platform allows.
* :func:`default_model_for` — return a conservative default model name
  for a provider, or ``""`` if unknown.
* :func:`resolve_data_dir` — extract the engine data directory from a
  FastAPI ``Request`` state, falling back to ``Path.home() / ".sovyx"``
  for CLI / first-boot scenarios.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


_DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "google": "gemini-2.5-pro-preview-03-25",
    "xai": "grok-2",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
    "groq": "llama-3.1-70b-versatile",
    "together": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    "fireworks": "accounts/fireworks/models/llama-v3p1-70b-instruct",
    "ollama": "llama3.1:latest",
}


def create_provider(name: str, api_key: str) -> object | None:
    """Instantiate a provider class by canonical name + API key.

    Lazy imports keep the cost of importing this module bounded — the
    provider classes themselves pull in httpx + SDK shims that are heavy.
    """
    try:
        if name == "anthropic":
            from sovyx.llm.providers.anthropic import AnthropicProvider

            return AnthropicProvider(api_key=api_key)
        if name == "openai":
            from sovyx.llm.providers.openai import OpenAIProvider

            return OpenAIProvider(api_key=api_key)
        if name == "google":
            from sovyx.llm.providers.google import GoogleProvider

            return GoogleProvider(api_key=api_key)
        if name == "xai":
            from sovyx.llm.providers.xai import XAIProvider

            return XAIProvider(api_key=api_key)
        if name == "deepseek":
            from sovyx.llm.providers.deepseek import DeepSeekProvider

            return DeepSeekProvider(api_key=api_key)
        if name == "mistral":
            from sovyx.llm.providers.mistral import MistralProvider

            return MistralProvider(api_key=api_key)
        if name == "groq":
            from sovyx.llm.providers.groq import GroqProvider

            return GroqProvider(api_key=api_key)
        if name == "together":
            from sovyx.llm.providers.together import TogetherProvider

            return TogetherProvider(api_key=api_key)
        if name == "fireworks":
            from sovyx.llm.providers.fireworks import FireworksProvider

            return FireworksProvider(api_key=api_key)
    except Exception:  # noqa: BLE001
        logger.warning("provider_creation_failed", extra={"provider": name})
    return None


async def test_provider(provider: object) -> tuple[bool, str]:
    """Validate a provider by attempting a minimal generation.

    Returns ``(success, message)``. Always catches exceptions — the
    caller MUST NOT depend on this raising. Used by the dashboard
    onboarding endpoint, the ``/api/llm/test-connection`` endpoint, and
    the CLI ``sovyx llm setup`` wizard.
    """
    try:
        from sovyx.engine.protocols import LLMProvider

        if not isinstance(provider, LLMProvider):
            return False, "Not a valid provider"
        default_model = default_model_for(getattr(provider, "name", ""))
        resp = await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
            model=default_model,
            temperature=0.0,
            max_tokens=5,
        )
        if resp and hasattr(resp, "content") and resp.content:
            return True, "OK"
        return True, "Connected (empty response)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def default_model_for(provider_name: str) -> str:
    """Return sensible default model identifier for a provider, or ``""``."""
    return _DEFAULT_MODEL_BY_PROVIDER.get(provider_name, "")


def persist_api_key(data_dir: Path, env_var: str, api_key: str) -> Path:
    """Idempotent write of ``ENV_VAR=value`` into ``<data_dir>/secrets.env``.

    Replaces an existing matching line if present; appends otherwise.
    Creates the parent directory if missing. Sets ``0o600`` permissions
    on POSIX-y platforms (no-op on Windows).

    Returns the resolved secrets-env path.
    """
    secrets_path = Path(data_dir) / "secrets.env"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if secrets_path.exists():
        existing_lines = secrets_path.read_text(encoding="utf-8").splitlines()

    found = False
    new_lines: list[str] = []
    for line in existing_lines:
        if line.strip().startswith(f"{env_var}="):
            new_lines.append(f"{env_var}={api_key}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{env_var}={api_key}")

    secrets_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        secrets_path.chmod(0o600)

    logger.info(
        "api_key_persisted",
        extra={"env_var": env_var, "path": str(secrets_path)},
    )
    return secrets_path


def resolve_data_dir(request: Request | None = None) -> Path:
    """Resolve the effective data directory.

    1. ``request.app.state.data_dir`` (set by dashboard at boot).
    2. ``request.app.state.engine_config.data_dir`` (fallback).
    3. ``Path.home() / ".sovyx"`` (CLI / pre-boot fallback).
    """
    if request is not None:
        data_dir = getattr(request.app.state, "data_dir", None)
        if data_dir is not None:
            return Path(data_dir)
        engine_config = getattr(request.app.state, "engine_config", None)
        if engine_config is not None:
            return Path(engine_config.data_dir)
    return Path.home() / ".sovyx"


__all__ = [
    "create_provider",
    "default_model_for",
    "persist_api_key",
    "resolve_data_dir",
    "test_provider",
]
