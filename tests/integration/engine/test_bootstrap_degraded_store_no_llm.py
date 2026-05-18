"""Integration test — bootstrap's LLM-discovery dispatch populates EngineDegradedStore.

Mission anchor: ``docs-internal/missions/MISSION-c6-llm-provider-cognitive-
loop-integrity-2026-05-18.md`` §T2.2 (replaces the pre-C6 Mission C4 §T1.2
hardcoded ``reason="no_llm_provider"`` shim).

The Phase 1.B wire at ``engine/bootstrap.py`` now invokes
:func:`sovyx.engine._llm_dispatch.dispatch_llm_discovery_verdict` against
the result of :func:`scan_llm_provider_health`. This test exercises the
live dispatch against a synthetic ``NO_PROVIDER_CONFIGURED`` report (the
v0.43.1 operator's actual case — no cloud keys + Ollama not running) and
asserts the store entry lands with the refined reason taxonomy.

Anti-pattern #20 compliance: this test calls the LIVE dispatch helper
rather than mimicking the bootstrap shape inline. When production
refactors the dispatch, the test follows automatically.
"""

from __future__ import annotations

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    scan_llm_provider_health,
)


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _fresh_install_no_provider_report():
    """Replicates the v0.43.1 operator case via the pure-function scanner."""
    return scan_llm_provider_health(
        env={},
        ollama_ping_result=False,
        ollama_models=None,
        default_provider="",
        default_model="",
    )


class TestBootstrapDispatchNoProviderConfigured:
    def test_record_lands_axis_llm(self) -> None:
        report = _fresh_install_no_provider_report()
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "llm"
        assert entries[0].reason == "no_provider_configured"

    def test_record_severity_critical(self) -> None:
        report = _fresh_install_no_provider_report()
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].severity == "critical"

    def test_record_has_canonical_chip_targets(self) -> None:
        report = _fresh_install_no_provider_report()
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        targets = {c.target for c in entries[0].action_chips}
        assert "https://ollama.ai" in targets
        assert "/settings/providers" in targets

    def test_metadata_includes_verdict_token(self) -> None:
        report = _fresh_install_no_provider_report()
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].metadata["verdict"] == "no_provider_configured"

    def test_fully_available_clears_axis(self) -> None:
        """Verdict transition to FULLY_AVAILABLE clears the axis."""
        # First record degraded state
        report_degraded = _fresh_install_no_provider_report()
        dispatch_llm_discovery_verdict(report_degraded)
        assert len(get_default_degraded_store().snapshot()) == 1

        # Then transition to healthy
        report_healthy = scan_llm_provider_health(
            env={},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report_healthy.verdict is DiscoveryVerdict.FULLY_AVAILABLE
        dispatch_llm_discovery_verdict(report_healthy)
        assert get_default_degraded_store().snapshot() == []


class TestBootstrapDispatchOllamaUnreachable:
    def test_default_ollama_down_records_ollama_unreachable(self) -> None:
        """Operator previously configured Ollama as default; daemon down → regression."""
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_UNREACHABLE
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].reason == "ollama_unreachable"
        assert entries[0].severity == "error"
        targets = {c.target for c in entries[0].action_chips}
        # "Start Ollama" chip (NOT "Install Ollama" — operator already has it)
        assert "https://ollama.ai/docs/start" in targets


class TestBootstrapDispatchOllamaNoModels:
    def test_ollama_no_models_records_warn_severity(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=True,
            ollama_models=(),
            default_provider="",
            default_model="",
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_NO_MODELS
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].reason == "ollama_no_models"
        assert entries[0].severity == "warn"


class TestBootstrapDispatchCloudKeyInvalid:
    def test_all_cloud_invalid_records_cloud_key_invalid(self) -> None:
        report = scan_llm_provider_health(
            env={"ANTHROPIC_API_KEY": "sk-bad", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
            cloud_key_validation_results={"anthropic": False, "openai": False},
        )
        assert report.verdict is DiscoveryVerdict.CLOUD_KEY_INVALID
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].reason == "cloud_key_invalid"
        assert "invalid_providers" in entries[0].metadata
        assert set(entries[0].metadata["invalid_providers"]) == {"anthropic", "openai"}


class TestBootstrapDispatchPartialHealth:
    def test_some_unhealthy_records_partial_warn(self) -> None:
        report = scan_llm_provider_health(
            env={"ANTHROPIC_API_KEY": "sk-ok", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="",
            default_model="",
            cloud_key_validation_results={"anthropic": True, "openai": False},
        )
        assert report.verdict is DiscoveryVerdict.PARTIAL_HEALTH
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert entries[0].reason == "partial_health"
        assert entries[0].severity == "warn"
        assert "healthy_providers" in entries[0].metadata
        assert "unhealthy_providers" in entries[0].metadata


class TestBootstrapDispatchDualEmission:
    def test_no_provider_configured_dual_emits_legacy_warn(self, caplog) -> None:
        """ADR-D14 LENIENT: legacy `no_llm_provider_detected` WARN preserved."""
        import logging

        caplog.set_level(logging.WARNING)
        report = _fresh_install_no_provider_report()
        dispatch_llm_discovery_verdict(report)
        legacy = [r for r in caplog.records if r.message == "no_llm_provider_detected"]
        assert len(legacy) == 1
        assert getattr(legacy[0], "proximate_cause", "") == "no_provider_configured"

    def test_ollama_unreachable_dual_emits_legacy_warn(self, caplog) -> None:
        import logging

        caplog.set_level(logging.WARNING)
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        dispatch_llm_discovery_verdict(report)
        legacy = [r for r in caplog.records if r.message == "no_llm_provider_detected"]
        assert len(legacy) == 1
        assert getattr(legacy[0], "proximate_cause", "") == "ollama_unreachable"
