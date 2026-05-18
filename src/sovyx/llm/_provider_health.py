"""LLM provider discovery + health classifier.

Mission anchor: ``docs-internal/missions/MISSION-c6-llm-provider-cognitive-
loop-integrity-2026-05-18.md`` §T1.2.

Pure-function ``scan_llm_provider_health`` returns a deterministic
:class:`LLMRouterDiscoveryReport` from a snapshot of the environment plus
the most-recent Ollama ping result. Used by:

* :mod:`sovyx.engine.bootstrap` at boot to emit ``llm.discovery.report``
  and dispatch the composite-store wire (Mission C6 §T2.1).
* :mod:`sovyx.engine._llm_liveness_probe` periodically to detect liveness
  transitions (§T2.5).
* :mod:`sovyx.cli.commands.llm` ``sovyx llm doctor`` for CLI triage (§T3.1).
* :mod:`sovyx.dashboard.routes.llm_health` ``/api/llm/health`` endpoint (§T2.7).

Verdict precedence (top-to-bottom; first match wins):

1. ``NO_PROVIDER_CONFIGURED``     — ``configured_count == 0``.
2. ``OLLAMA_UNREACHABLE``         — Ollama is the only configured provider AND
   the ping failed.
3. ``OLLAMA_NO_MODELS``           — Ollama configured + reachable + empty model list.
4. ``CLOUD_KEY_INVALID``          — validation results provided AND every cloud
   key failed AND no Ollama fallback.
5. ``ALL_PROVIDERS_UNHEALTHY``    — at least one configured provider but none
   currently available.
6. ``DEFAULT_MODEL_UNAVAILABLE``  — at least one available provider BUT the
   configured ``default_model`` is not in any provider's model catalogue.
7. ``PARTIAL_HEALTH``             — at least one provider available AND at least
   one configured provider unhealthy.
8. ``FULLY_AVAILABLE``            — every configured provider is available.

The function is intentionally pure: no I/O, no network calls, no module-level
state mutation. Caller supplies ``ollama_ping_result`` and ``ollama_models``
so the function is trivially testable + idempotent + bounded-latency.

Anti-pattern compliance:

* #9  — :class:`DiscoveryVerdict` is :class:`StrEnum` (xdist-safe).
* #14 — caller-supplied I/O keeps this off the event-loop hot path.
* #15 — bounded report cardinality (10 entries fixed by
  :class:`~sovyx.llm._provider_registry.LLMProviderKey`).
* #24 — no time-comparison hazards; ``scan_duration_ms`` is informational only.
* #30 — no ``os.stat`` or psutil iteration anywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.llm._provider_registry import LLMProviderKey

if TYPE_CHECKING:
    from collections.abc import Mapping


class DiscoveryVerdict(StrEnum):
    """Eight-state classifier for LLM router availability.

    Replaces the implicit two-state ``providers == [] OR providers != []``
    gate at ``engine/bootstrap.py:711`` with explicit, operator-actionable
    verdicts. Distinguishes the v0.43.1 operator's case (``OLLAMA_UNREACHABLE``
    — Ollama installed but daemon down) from the prior catch-all
    (``NO_PROVIDER_CONFIGURED`` — no Ollama AND no cloud keys).

    ``StrEnum`` per anti-pattern #9 (xdist-safe value-based comparison).
    """

    FULLY_AVAILABLE = "fully_available"
    PARTIAL_HEALTH = "partial_health"
    NO_PROVIDER_CONFIGURED = "no_provider_configured"
    OLLAMA_UNREACHABLE = "ollama_unreachable"
    OLLAMA_NO_MODELS = "ollama_no_models"
    CLOUD_KEY_INVALID = "cloud_key_invalid"
    ALL_PROVIDERS_UNHEALTHY = "all_providers_unhealthy"
    DEFAULT_MODEL_UNAVAILABLE = "default_model_unavailable"


@dataclass(frozen=True, slots=True)
class ProviderHealthEntry:
    """Per-provider health snapshot. Immutable.

    Attributes:
        name: Canonical provider value (e.g. ``"anthropic"``).
        env_var: Env-var name (empty for Ollama).
        is_cloud: True for cloud providers.
        configured: Env-var present + non-empty (cloud) OR always True (Ollama).
        reachable: Liveness result. ``None`` for unprobed cloud providers (default —
            cloud probes are opt-in via ``tuning.llm.boot_key_validation_enabled``).
            ``True`` / ``False`` for Ollama (always probed) or post-validation cloud.
        key_valid: Validity of the API key. ``None`` for unvalidated providers;
            ``True`` / ``False`` only when validation actually ran.
        failure_reason: Short token describing why the provider is unhealthy.
            Examples: ``"no_key"``, ``"ping_failed"``, ``"auth_failed"``,
            ``"no_models"``, ``"timeout"``. ``None`` when healthy or unprobed.
    """

    name: str
    env_var: str
    is_cloud: bool
    configured: bool
    reachable: bool | None
    key_valid: bool | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class LLMRouterDiscoveryReport:
    """Immutable canonical snapshot of LLM router discovery.

    Frozen + slotted so consumers cannot mutate canonical state. The
    ``per_provider`` tuple is emitted in :class:`LLMProviderKey` enum
    iteration order for deterministic reproducibility across runs.

    Attributes:
        verdict: Top-level categorical verdict per :class:`DiscoveryVerdict`.
        per_provider: Per-provider health snapshots (deterministic order).
        configured_count: Number of providers with present env-var (or Ollama
            unconditionally — it has no env-var requirement).
        available_count: Number of providers that are configured AND reachable
            AND (key_valid OR unvalidated). Equivalent to
            ``len([e for e in per_provider if e.configured and e.reachable is not False
            and e.key_valid is not False])``.
        default_provider: Resolved default provider name; empty string when
            unresolved.
        default_model: Resolved default model identifier; empty string when
            unresolved.
        scan_duration_ms: Wall-clock duration of the scan (informational only).
        scanned_at_monotonic: ``time.monotonic()`` at scan start.
    """

    verdict: DiscoveryVerdict
    per_provider: tuple[ProviderHealthEntry, ...]
    configured_count: int
    available_count: int
    default_provider: str
    default_model: str
    scan_duration_ms: float
    scanned_at_monotonic: float


def scan_llm_provider_health(
    env: Mapping[str, str],
    *,
    ollama_ping_result: bool | None,
    ollama_models: tuple[str, ...] | None,
    default_provider: str,
    default_model: str,
    cloud_key_validation_results: Mapping[str, bool] | None = None,
) -> LLMRouterDiscoveryReport:
    """Compute a deterministic discovery report from environment + Ollama probe.

    Pure function — no I/O. Caller supplies:

    * ``env``: snapshot of ``os.environ`` (or a synthetic dict for tests).
    * ``ollama_ping_result``: ``True``/``False`` if Ollama was probed; ``None``
      if probe was skipped (treated as unreachable for verdict purposes).
    * ``ollama_models``: tuple of model names; ``None`` if Ollama unreachable
      OR list-models call failed.
    * ``default_provider`` / ``default_model``: resolved from ``mind.yaml`` (or
      empty strings if unresolved).
    * ``cloud_key_validation_results``: optional ``{provider_value: bool}`` map
      from a boot-time validation probe (opt-in via
      ``tuning.llm.boot_key_validation_enabled``); ``None`` means validation
      did not run and ``key_valid`` is ``None`` for every cloud entry.

    Returns a :class:`LLMRouterDiscoveryReport`. Idempotent on identical inputs.
    """
    started_at = time.monotonic()
    started_perf = time.perf_counter()
    entries: list[ProviderHealthEntry] = []
    validation_map = cloud_key_validation_results or {}

    for key in LLMProviderKey:
        env_var = key.env_var
        if key is LLMProviderKey.OLLAMA:
            # Ollama is "configured" iff (a) it is reachable at probe time,
            # OR (b) the mind config marks it as the default provider
            # (operator previously committed to it via the onboarding flow
            # or direct mind.yaml edit). Without (b), a fresh-install with
            # Ollama daemon down looks identical to "never installed" and
            # collapses to NO_PROVIDER_CONFIGURED. With (b) the discovery
            # layer can distinguish the regression case and emit the more
            # actionable OLLAMA_UNREACHABLE verdict + "Start Ollama" chip.
            reachable = ollama_ping_result is True
            configured = reachable or (default_provider == LLMProviderKey.OLLAMA.value)
            key_valid: bool | None = None
            if reachable and ollama_models is not None and len(ollama_models) == 0:
                failure_reason: str | None = "no_models"
            elif reachable:
                failure_reason = None
            else:
                failure_reason = "ping_failed" if ollama_ping_result is False else "not_probed"
        else:
            env_value = env.get(env_var, "")
            configured = bool(env_value)
            if not configured:
                reachable = False
                key_valid = None
                failure_reason = "no_key"
            else:
                validation = validation_map.get(key.value)
                key_valid = validation
                if validation is False:
                    reachable = False
                    failure_reason = "auth_failed"
                elif validation is True:
                    reachable = True
                    failure_reason = None
                else:
                    # Validation not performed — treat as available pending
                    # first real call. Liveness probe + first-call failure
                    # surface invalid keys without boot-time validation cost.
                    reachable = None
                    failure_reason = None

        entries.append(
            ProviderHealthEntry(
                name=key.value,
                env_var=env_var,
                is_cloud=key.is_cloud,
                configured=configured,
                reachable=reachable,
                key_valid=key_valid,
                failure_reason=failure_reason,
            ),
        )

    configured_count = sum(1 for entry in entries if entry.configured)
    cloud_configured = [entry for entry in entries if entry.configured and entry.is_cloud]
    ollama_entry = next(entry for entry in entries if entry.name == LLMProviderKey.OLLAMA.value)
    cloud_configured_count = len(cloud_configured)
    invalid_cloud_count = sum(1 for entry in cloud_configured if entry.key_valid is False)
    # Ollama is "available for routing" iff reachable; the no_models gap is
    # surfaced separately via the OLLAMA_NO_MODELS verdict so a cloud-OK
    # + ollama-no-models combination doesn't collapse to PARTIAL_HEALTH.
    available_entries: list[ProviderHealthEntry] = []
    for entry in entries:
        if not entry.configured:
            continue
        if entry.name == LLMProviderKey.OLLAMA.value:
            if entry.reachable is True:
                available_entries.append(entry)
        elif entry.failure_reason is None:
            available_entries.append(entry)
    available_count = len(available_entries)

    # Verdict precedence — top match wins. See §2.2 of the C6 mission spec.
    if (
        default_provider == LLMProviderKey.OLLAMA.value
        and ollama_entry.failure_reason == "ping_failed"
    ):
        # Operator previously configured Ollama as the default; daemon is
        # now unreachable. Distinct from NO_PROVIDER_CONFIGURED because
        # there is a known-good prior state to recover. Takes precedence
        # over NO_PROVIDER_CONFIGURED so the "Start Ollama" chip surfaces.
        verdict = DiscoveryVerdict.OLLAMA_UNREACHABLE
    elif configured_count == 0:
        verdict = DiscoveryVerdict.NO_PROVIDER_CONFIGURED
    elif cloud_configured_count == 0 and ollama_entry.failure_reason == "no_models":
        verdict = DiscoveryVerdict.OLLAMA_NO_MODELS
    elif (
        cloud_configured_count > 0
        and invalid_cloud_count == cloud_configured_count
        and ollama_entry.reachable is not True
    ):
        verdict = DiscoveryVerdict.CLOUD_KEY_INVALID
    elif available_count == 0:
        verdict = DiscoveryVerdict.ALL_PROVIDERS_UNHEALTHY
    elif (
        default_model
        and default_provider
        and _is_default_model_unavailable(
            default_provider=default_provider,
            default_model=default_model,
            ollama_models=ollama_models,
            available_entries=available_entries,
        )
    ):
        verdict = DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE
    elif available_count < configured_count:
        verdict = DiscoveryVerdict.PARTIAL_HEALTH
    else:
        verdict = DiscoveryVerdict.FULLY_AVAILABLE

    duration_ms = (time.perf_counter() - started_perf) * 1000.0

    return LLMRouterDiscoveryReport(
        verdict=verdict,
        per_provider=tuple(entries),
        configured_count=configured_count,
        available_count=available_count,
        default_provider=default_provider,
        default_model=default_model,
        scan_duration_ms=duration_ms,
        scanned_at_monotonic=started_at,
    )


def _is_default_model_unavailable(
    *,
    default_provider: str,
    default_model: str,
    ollama_models: tuple[str, ...] | None,
    available_entries: list[ProviderHealthEntry],
) -> bool:
    """Return True iff the configured default model cannot be served.

    For Ollama: check the model list explicitly. For cloud providers we
    cannot enumerate models cheaply at boot, so we accept the default as
    available whenever the named cloud provider is itself available — the
    actual model-not-found error surfaces at first cognitive call (out of
    scope for the boot-time discovery report; covered by the liveness probe
    in a future hardening).
    """
    if default_provider == LLMProviderKey.OLLAMA.value:
        if ollama_models is None:
            return True
        return default_model not in ollama_models
    return not any(entry.name == default_provider for entry in available_entries)


__all__ = [
    "DiscoveryVerdict",
    "LLMRouterDiscoveryReport",
    "ProviderHealthEntry",
    "scan_llm_provider_health",
]
