"""Unit tests — `sovyx.llm._provider_health.scan_llm_provider_health` (Mission C6 §T1.5).

Coverage: every DiscoveryVerdict, precedence rules, per-provider determinism,
scan_duration bounds, idempotency, dataclass immutability.

Verdict semantics:
* Ollama is "configured" iff ``ollama_ping_result is True``. An unreachable
  Ollama is treated as unconfigured at the scanner level — the dual-chip
  remediation on ``NO_PROVIDER_CONFIGURED`` covers both "not installed"
  and "down".
* ``OLLAMA_UNREACHABLE`` fires only when ``default_provider == "ollama"``
  AND the daemon is now down — distinguishes a regression from a known-good
  state versus a never-configured fresh install.
"""

from __future__ import annotations

import pytest

from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    LLMRouterDiscoveryReport,
    ProviderHealthEntry,
    scan_llm_provider_health,
)
from sovyx.llm._provider_registry import LLMProviderKey


def _scan(
    env: dict[str, str] | None = None,
    *,
    ollama_ping_result: bool | None = False,
    ollama_models: tuple[str, ...] | None = None,
    default_provider: str = "",
    default_model: str = "",
    validation: dict[str, bool] | None = None,
) -> LLMRouterDiscoveryReport:
    return scan_llm_provider_health(
        env or {},
        ollama_ping_result=ollama_ping_result,
        ollama_models=ollama_models,
        default_provider=default_provider,
        default_model=default_model,
        cloud_key_validation_results=validation,
    )


class TestVerdictNoProviderConfigured:
    def test_empty_env_no_ollama_returns_no_provider(self) -> None:
        """Operator's v0.43.1 case: no cloud keys, Ollama not installed."""
        report = _scan({}, ollama_ping_result=False)
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
        assert report.configured_count == 0
        assert report.available_count == 0

    def test_empty_env_ollama_reachable_with_models_returns_fully_available(self) -> None:
        report = _scan(
            {},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
        )
        assert report.verdict is DiscoveryVerdict.FULLY_AVAILABLE


class TestVerdictOllamaUnreachable:
    def test_default_ollama_ping_failed_returns_ollama_unreachable(self) -> None:
        """Operator's previously-configured Ollama default + daemon down = regression."""
        report = _scan(
            {},
            ollama_ping_result=False,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_UNREACHABLE

    def test_no_default_ollama_ping_failed_returns_no_provider(self) -> None:
        """Fresh install — no prior known-good state — collapses to NO_PROVIDER."""
        report = _scan({}, ollama_ping_result=False)
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED


class TestVerdictOllamaNoModels:
    def test_only_ollama_with_zero_models(self) -> None:
        report = _scan(
            {},
            ollama_ping_result=True,
            ollama_models=(),
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_NO_MODELS

    def test_cloud_present_ollama_no_models_returns_fully_available(self) -> None:
        """When cloud is healthy, Ollama-no-models is informational, not actionable."""
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-test"},
            ollama_ping_result=True,
            ollama_models=(),
        )
        assert report.verdict is DiscoveryVerdict.FULLY_AVAILABLE


class TestVerdictCloudKeyInvalid:
    def test_only_cloud_keys_all_invalid_no_ollama(self) -> None:
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-bad", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=False,
            validation={"anthropic": False, "openai": False},
        )
        assert report.verdict is DiscoveryVerdict.CLOUD_KEY_INVALID

    def test_some_cloud_keys_valid_returns_partial(self) -> None:
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-ok", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=False,
            validation={"anthropic": True, "openai": False},
        )
        assert report.verdict is DiscoveryVerdict.PARTIAL_HEALTH

    def test_all_cloud_invalid_but_ollama_works_returns_partial(self) -> None:
        """Ollama carries the router; not CLOUD_KEY_INVALID severity."""
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-bad"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            validation={"anthropic": False},
        )
        assert report.verdict is DiscoveryVerdict.PARTIAL_HEALTH


class TestVerdictAllProvidersUnhealthy:
    def test_only_validated_cloud_all_false_with_ollama_down(self) -> None:
        """CLOUD_KEY_INVALID takes precedence when all are validated False."""
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-bad"},
            ollama_ping_result=False,
            validation={"anthropic": False},
        )
        # Single invalid cloud + no Ollama → CLOUD_KEY_INVALID (more specific)
        assert report.verdict is DiscoveryVerdict.CLOUD_KEY_INVALID


class TestVerdictDefaultModelUnavailable:
    def test_ollama_default_model_not_in_list(self) -> None:
        report = _scan(
            {},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.3:70b",
        )
        assert report.verdict is DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE

    def test_cloud_default_provider_not_configured(self) -> None:
        """Default provider names a cloud whose key isn't set."""
        report = _scan(
            {},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="anthropic",
            default_model="claude-sonnet-4-6",
        )
        assert report.verdict is DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE


class TestVerdictPartialHealth:
    def test_cloud_key_present_ollama_down(self) -> None:
        """Cloud carries the router; Ollama unconfigured = not available."""
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-test"},
            ollama_ping_result=False,
        )
        # Only cloud configured (Ollama treated as unconfigured) → available == configured → FULLY
        assert report.verdict is DiscoveryVerdict.FULLY_AVAILABLE

    def test_cloud_present_invalid_with_other_cloud_ok(self) -> None:
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-ok", "OPENAI_API_KEY": "sk-bad"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            validation={"anthropic": True, "openai": False},
        )
        assert report.verdict is DiscoveryVerdict.PARTIAL_HEALTH


class TestVerdictFullyAvailable:
    def test_only_ollama_reachable_with_models(self) -> None:
        report = _scan(
            {},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
        )
        assert report.verdict is DiscoveryVerdict.FULLY_AVAILABLE

    def test_anthropic_key_present_no_validation(self) -> None:
        """Without explicit validation, key presence is treated as available."""
        report = _scan(
            {"ANTHROPIC_API_KEY": "sk-test"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
        )
        assert report.verdict is DiscoveryVerdict.FULLY_AVAILABLE


class TestPrecedence:
    def test_no_provider_beats_ollama_unreachable(self) -> None:
        """Empty default + Ollama down = NO_PROVIDER, not OLLAMA_UNREACHABLE."""
        report = _scan({}, ollama_ping_result=False)
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED

    def test_default_ollama_beats_no_provider_when_default_set(self) -> None:
        """default=ollama + Ollama down = OLLAMA_UNREACHABLE (regression signal)."""
        report = _scan(
            {},
            ollama_ping_result=False,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_UNREACHABLE


class TestPerProviderInvariants:
    def test_entries_count_equals_member_count(self) -> None:
        report = _scan()
        assert len(report.per_provider) == len(list(LLMProviderKey))

    def test_entries_ordered_by_enum_iteration(self) -> None:
        report = _scan()
        actual = [entry.name for entry in report.per_provider]
        expected = [key.value for key in LLMProviderKey]
        assert actual == expected

    def test_entries_are_frozen(self) -> None:
        report = _scan()
        entry = report.per_provider[0]
        with pytest.raises(Exception) as exc_info:
            entry.name = "mutated"  # type: ignore[misc]
        assert type(exc_info.value).__name__ in {
            "FrozenInstanceError",
            "AttributeError",
        }

    def test_ollama_entry_marks_not_cloud(self) -> None:
        report = _scan()
        ollama = next(e for e in report.per_provider if e.name == "ollama")
        assert ollama.is_cloud is False
        assert ollama.env_var == ""

    def test_unconfigured_cloud_failure_reason_no_key(self) -> None:
        report = _scan({})
        anthropic = next(e for e in report.per_provider if e.name == "anthropic")
        assert anthropic.configured is False
        assert anthropic.failure_reason == "no_key"

    def test_ollama_ping_failed_failure_reason(self) -> None:
        report = _scan({}, ollama_ping_result=False)
        ollama = next(e for e in report.per_provider if e.name == "ollama")
        assert ollama.failure_reason == "ping_failed"

    def test_ollama_ping_none_failure_reason_not_probed(self) -> None:
        report = _scan({}, ollama_ping_result=None)
        ollama = next(e for e in report.per_provider if e.name == "ollama")
        assert ollama.failure_reason == "not_probed"


class TestReportShape:
    def test_report_is_frozen(self) -> None:
        report = _scan()
        with pytest.raises(Exception) as exc_info:
            report.verdict = DiscoveryVerdict.FULLY_AVAILABLE  # type: ignore[misc]
        assert type(exc_info.value).__name__ in {
            "FrozenInstanceError",
            "AttributeError",
        }

    def test_scan_duration_bounded(self) -> None:
        report = _scan()
        # Rule #12 / AP #31: non-negative + sane-ceiling invariant, not speed —
        # a tight wall-clock bound flakes on slow CI (perf is the perf-gate's job).
        assert 0.0 <= report.scan_duration_ms < 30_000.0

    def test_scan_duration_non_negative(self) -> None:
        for _ in range(5):
            report = _scan()
            assert report.scan_duration_ms >= 0.0

    def test_configured_count_correctness_cloud_only(self) -> None:
        report = _scan({"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})
        # Ollama not reachable → not configured. 2 cloud + 0 Ollama = 2.
        assert report.configured_count == 2

    def test_configured_count_with_ollama_reachable(self) -> None:
        report = _scan(
            {"ANTHROPIC_API_KEY": "k"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
        )
        # 1 cloud + Ollama reachable = 2.
        assert report.configured_count == 2

    def test_available_count_lte_configured(self) -> None:
        report = _scan(
            {"ANTHROPIC_API_KEY": "k"},
            ollama_ping_result=False,
        )
        assert report.available_count <= report.configured_count


class TestIdempotency:
    def test_re_scan_yields_same_verdict(self) -> None:
        env = {"ANTHROPIC_API_KEY": "sk-test"}
        report_1 = _scan(env, ollama_ping_result=True, ollama_models=("a:b",))
        report_2 = _scan(env, ollama_ping_result=True, ollama_models=("a:b",))
        assert report_1.verdict == report_2.verdict
        assert report_1.configured_count == report_2.configured_count
        assert report_1.available_count == report_2.available_count


class TestStrEnumValues:
    def test_verdict_values_are_strings(self) -> None:
        assert DiscoveryVerdict.FULLY_AVAILABLE == "fully_available"
        assert DiscoveryVerdict.NO_PROVIDER_CONFIGURED == "no_provider_configured"
        assert DiscoveryVerdict.OLLAMA_UNREACHABLE == "ollama_unreachable"

    def test_provider_health_entry_constructible(self) -> None:
        entry = ProviderHealthEntry(
            name="anthropic",
            env_var="ANTHROPIC_API_KEY",
            is_cloud=True,
            configured=True,
            reachable=True,
            key_valid=None,
            failure_reason=None,
        )
        assert entry.name == "anthropic"
