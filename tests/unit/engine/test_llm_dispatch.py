"""Unit tests — `sovyx.engine._llm_dispatch.dispatch_llm_discovery_verdict` (Mission C6 §T2.2).

Coverage: each of the 7 record helpers + FULLY_AVAILABLE clearance +
metadata shape invariants + ADR-D14 dual-emission of legacy events.
"""

from __future__ import annotations

import logging

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


def _scan(**kwargs):
    defaults = {
        "env": {},
        "ollama_ping_result": False,
        "ollama_models": None,
        "default_provider": "",
        "default_model": "",
    }
    defaults.update(kwargs)
    return scan_llm_provider_health(**defaults)


class TestFullyAvailableClears:
    def test_clears_prior_degraded_state(self) -> None:
        # Seed degraded
        dispatch_llm_discovery_verdict(_scan())
        assert len(get_default_degraded_store().snapshot()) == 1
        # Transition healthy
        healthy = _scan(
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        dispatch_llm_discovery_verdict(healthy)
        assert get_default_degraded_store().snapshot() == []

    def test_fully_available_with_empty_store_no_op(self) -> None:
        """Calling dispatch with FULLY_AVAILABLE + empty store does not raise."""
        healthy = _scan(
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert healthy.verdict is DiscoveryVerdict.FULLY_AVAILABLE
        dispatch_llm_discovery_verdict(healthy)
        assert get_default_degraded_store().snapshot() == []


class TestNoProviderConfigured:
    def test_records_critical_severity(self) -> None:
        dispatch_llm_discovery_verdict(_scan())
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.severity == "critical"

    def test_records_axis_llm(self) -> None:
        dispatch_llm_discovery_verdict(_scan())
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.axis == "llm"
        assert entry.reason == "no_provider_configured"

    def test_chip_install_ollama_and_settings(self) -> None:
        dispatch_llm_discovery_verdict(_scan())
        entry = get_default_degraded_store().snapshot()[0]
        targets = {c.target for c in entry.action_chips}
        assert "https://ollama.ai" in targets
        assert "/settings/providers" in targets


class TestOllamaUnreachable:
    def test_records_error_severity(self) -> None:
        report = _scan(
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "ollama_unreachable"
        assert entry.severity == "error"

    def test_start_ollama_chip(self) -> None:
        report = _scan(
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        targets = {c.target for c in entry.action_chips}
        assert "https://ollama.ai/docs/start" in targets


class TestOllamaNoModels:
    def test_records_warn_severity(self) -> None:
        report = _scan(ollama_ping_result=True, ollama_models=())
        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "ollama_no_models"
        assert entry.severity == "warn"


class TestCloudKeyInvalid:
    def test_records_invalid_providers_metadata(self) -> None:
        report = _scan(
            env={"ANTHROPIC_API_KEY": "sk-bad", "OPENAI_API_KEY": "sk-bad"},
            cloud_key_validation_results={"anthropic": False, "openai": False},
        )
        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "cloud_key_invalid"
        assert entry.severity == "error"
        assert set(entry.metadata["invalid_providers"]) == {"anthropic", "openai"}


class TestPartialHealth:
    def test_records_healthy_and_unhealthy_providers(self) -> None:
        report = _scan(
            env={"ANTHROPIC_API_KEY": "sk-ok", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            cloud_key_validation_results={"anthropic": True, "openai": False},
        )
        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "partial_health"
        assert entry.severity == "warn"
        assert "anthropic" in entry.metadata["healthy_providers"]
        assert "openai" in entry.metadata["unhealthy_providers"]


class TestBaseMetadataShape:
    def test_includes_verdict_counts_and_defaults(self) -> None:
        dispatch_llm_discovery_verdict(_scan())
        entry = get_default_degraded_store().snapshot()[0]
        for key in (
            "verdict",
            "configured_count",
            "available_count",
            "default_provider",
            "default_model",
            "scan_duration_ms",
        ):
            assert key in entry.metadata


class TestDualEmission:
    def test_no_provider_configured_emits_legacy_warn(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        dispatch_llm_discovery_verdict(_scan())
        legacy = [r for r in caplog.records if r.message == "no_llm_provider_detected"]
        assert len(legacy) == 1

    def test_ollama_unreachable_emits_legacy_warn(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        dispatch_llm_discovery_verdict(
            _scan(default_provider="ollama", default_model="llama3.1:latest"),
        )
        legacy = [r for r in caplog.records if r.message == "no_llm_provider_detected"]
        assert len(legacy) == 1

    def test_ollama_no_models_emits_legacy_warn(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        dispatch_llm_discovery_verdict(_scan(ollama_ping_result=True, ollama_models=()))
        legacy = [r for r in caplog.records if r.message == "ollama_no_models"]
        assert len(legacy) == 1

    def test_fully_available_no_legacy_warn(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        dispatch_llm_discovery_verdict(
            _scan(
                ollama_ping_result=True,
                ollama_models=("llama3.1:latest",),
                default_provider="ollama",
                default_model="llama3.1:latest",
            ),
        )
        legacy = [r for r in caplog.records if "no_llm_provider" in r.message]
        assert len(legacy) == 0
