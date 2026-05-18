"""Verdict-driven composite-store dispatch for LLM discovery (Mission C6 §T2.2).

Replaces the hardcoded single-reason wire at the pre-C6 ``bootstrap.py:735-795``
with a ``match _report.verdict`` dispatch that records one of seven distinct
reason tokens — each with verdict-specific severity, body copy, action chips,
and metadata. Operators get actionable remediation instead of a one-size-fits-all
"Install Ollama" chip that's wrong for the (very common) Ollama-installed-
but-down case.

Used by:

* :mod:`sovyx.engine.bootstrap` — boot-time dispatch immediately after
  ``scan_llm_provider_health`` emits the canonical ``llm.discovery.report``.
* :mod:`sovyx.engine._llm_liveness_probe` — periodic dispatch on verdict
  transitions detected by the background probe.

The dispatch is observability-side-effect only — it does not mutate the
``LLMRouter`` or the providers. The router's own state machinery is the
authoritative routing layer; this module surfaces the *operator-visible*
representation of that state.

Dual-emission discipline (ADR-D14 LENIENT through v0.49.x):

* ``NO_PROVIDER_CONFIGURED`` and ``OLLAMA_UNREACHABLE`` BOTH dual-emit the
  legacy ``no_llm_provider_detected`` WARN — pre-C6 both cases collapsed
  into that event, and operator playbooks reference it. Phase 3 v0.50.0
  STRICT flip drops the legacy.
* ``OLLAMA_NO_MODELS`` dual-emits the legacy ``ollama_no_models`` WARN
  already present at HEAD (``bootstrap.py:731``).

Anti-pattern compliance:

* #14 — dispatch is sync + cheap; no I/O.
* #15 — bounded cardinality (7 reason tokens, no growth).
* #42 — composite-store surface is the single operator-visible signal
  for the LLM axis; this module is the producer side of #42 for C6.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    now_monotonic,
)
from sovyx.llm._provider_health import DiscoveryVerdict

if TYPE_CHECKING:
    from sovyx.llm._provider_health import LLMRouterDiscoveryReport


logger = logging.getLogger(__name__)


def dispatch_llm_discovery_verdict(report: LLMRouterDiscoveryReport) -> None:
    """Route ``report.verdict`` to the appropriate composite-store record helper.

    Idempotent on ``FULLY_AVAILABLE`` — explicitly clears the LLM axis so a
    prior boot's transient state doesn't linger. Match-statement exhaustive
    over :class:`DiscoveryVerdict`; ruff's ``E2`` (exhaustive match) and
    mypy's narrowing combine to guarantee no verdict is silently ignored.
    """
    verdict = report.verdict
    if verdict is DiscoveryVerdict.FULLY_AVAILABLE:
        get_default_degraded_store().clear_axis("llm")
        return
    if verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED:
        _record_no_provider_configured(report)
        return
    if verdict is DiscoveryVerdict.OLLAMA_UNREACHABLE:
        _record_ollama_unreachable(report)
        return
    if verdict is DiscoveryVerdict.OLLAMA_NO_MODELS:
        _record_ollama_no_models(report)
        return
    if verdict is DiscoveryVerdict.CLOUD_KEY_INVALID:
        _record_cloud_key_invalid(report)
        return
    if verdict is DiscoveryVerdict.ALL_PROVIDERS_UNHEALTHY:
        _record_all_providers_unhealthy(report)
        return
    if verdict is DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE:
        _record_default_model_unavailable(report)
        return
    if verdict is DiscoveryVerdict.PARTIAL_HEALTH:
        _record_partial_health(report)
        return


def _base_metadata(report: LLMRouterDiscoveryReport) -> dict[str, object]:
    """Common metadata block — verdict + counts + default refs.

    Each record helper adds verdict-specific fields on top of this base.
    Tests pin the shape via boundary round-trip checks (Quality Gate 8).
    """
    return {
        "verdict": report.verdict.value,
        "configured_count": report.configured_count,
        "available_count": report.available_count,
        "default_provider": report.default_provider,
        "default_model": report.default_model,
        "scan_duration_ms": round(report.scan_duration_ms, 3),
    }


def _record_no_provider_configured(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="no_provider_configured",
            severity="critical",
            title_token="degraded.llm.noProviderConfigured.title",
            body_token="degraded.llm.noProviderConfigured.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.noProviderConfigured.runSetup",
                    "navigate",
                    "/settings/providers",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.noProviderConfigured.installOllama",
                    "external_link",
                    "https://ollama.ai",
                ),
            ),
            metadata=_base_metadata(report),
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )
    # Dual-emission per ADR-D14 — drops at v0.50.0 STRICT flip.
    logger.warning(
        "no_llm_provider_detected",
        extra={
            "hint": (
                "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY. "
                "Or install Ollama: https://ollama.ai"
            ),
            "proximate_cause": "no_provider_configured",
        },
    )


def _record_ollama_unreachable(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="ollama_unreachable",
            severity="error",
            title_token="degraded.llm.ollamaUnreachable.title",
            body_token="degraded.llm.ollamaUnreachable.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.ollamaUnreachable.startOllama",
                    "external_link",
                    "https://ollama.ai/docs/start",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.ollamaUnreachable.runDoctor",
                    "external_link",
                    "https://sovyx.dev/docs/cli/llm-doctor",
                ),
            ),
            metadata=_base_metadata(report),
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )
    # Dual-emission — pre-C6 this case collapsed into no_llm_provider_detected.
    logger.warning(
        "no_llm_provider_detected",
        extra={
            "hint": "Start the Ollama daemon: 'ollama serve' (or restart the service).",
            "proximate_cause": "ollama_unreachable",
        },
    )


def _record_ollama_no_models(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="ollama_no_models",
            severity="warn",
            title_token="degraded.llm.ollamaNoModels.title",
            body_token="degraded.llm.ollamaNoModels.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.ollamaNoModels.pullModel",
                    "external_link",
                    "https://ollama.ai/library",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.ollamaNoModels.runDoctor",
                    "external_link",
                    "https://sovyx.dev/docs/cli/llm-doctor",
                ),
            ),
            metadata=_base_metadata(report),
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )
    # Pre-C6 already emitted this — preserve the legacy event name verbatim.
    logger.warning(
        "ollama_no_models",
        extra={"hint": "Run: ollama pull llama3.1"},
    )


def _record_cloud_key_invalid(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    invalid_providers = tuple(
        entry.name for entry in report.per_provider if entry.key_valid is False
    )
    metadata = _base_metadata(report)
    metadata["invalid_providers"] = invalid_providers
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="cloud_key_invalid",
            severity="error",
            title_token="degraded.llm.cloudKeyInvalid.title",
            body_token="degraded.llm.cloudKeyInvalid.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.cloudKeyInvalid.openSettings",
                    "navigate",
                    "/settings/providers",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.cloudKeyInvalid.testConnection",
                    "navigate",
                    "/settings/providers",
                ),
            ),
            metadata=metadata,
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )


def _record_all_providers_unhealthy(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    failure_reasons = tuple(
        sorted({entry.failure_reason for entry in report.per_provider if entry.failure_reason})
    )
    metadata = _base_metadata(report)
    metadata["failure_reasons"] = failure_reasons
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="all_providers_unhealthy",
            severity="error",
            title_token="degraded.llm.allUnhealthy.title",
            body_token="degraded.llm.allUnhealthy.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.allUnhealthy.viewHealth",
                    "navigate",
                    "/settings/providers",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.allUnhealthy.runDoctor",
                    "external_link",
                    "https://sovyx.dev/docs/cli/llm-doctor",
                ),
            ),
            metadata=metadata,
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )


def _record_default_model_unavailable(report: LLMRouterDiscoveryReport) -> None:
    now = now_monotonic()
    metadata = _base_metadata(report)
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="default_model_unavailable",
            severity="error",
            title_token="degraded.llm.defaultModelUnavailable.title",
            body_token="degraded.llm.defaultModelUnavailable.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.defaultModelUnavailable.openSettings",
                    "navigate",
                    "/settings/providers",
                    style="primary",
                ),
            ),
            metadata=metadata,
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )


def _record_partial_health(report: LLMRouterDiscoveryReport) -> None:
    """Informational record — surfaces in `/api/engine/degraded` so the
    composite banner can summarize "some providers degraded; routing continues".

    Severity is ``warn``; the C4 banner ADR-D6 severity escalation (1 axis =
    warn, 2 = error, 3+ = critical) handles cross-axis aggregation.
    """
    now = now_monotonic()
    metadata = _base_metadata(report)
    metadata["healthy_providers"] = tuple(
        entry.name
        for entry in report.per_provider
        if entry.configured and entry.failure_reason is None
    )
    metadata["unhealthy_providers"] = tuple(
        entry.name
        for entry in report.per_provider
        if entry.configured and entry.failure_reason is not None
    )
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="partial_health",
            severity="warn",
            title_token="degraded.llm.partialHealth.title",
            body_token="degraded.llm.partialHealth.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.partialHealth.viewHealth",
                    "navigate",
                    "/settings/providers",
                ),
            ),
            metadata=metadata,
            first_observed_monotonic=now,
            last_observed_monotonic=now,
            occurrence_count=1,
        ),
    )


__all__ = ["dispatch_llm_discovery_verdict"]
