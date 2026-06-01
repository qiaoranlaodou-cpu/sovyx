"""Property-based invariants — `scan_llm_provider_health` (Mission C6 §T1.6).

Verifies determinism + bounded latency + structural correctness across
random env-map shapes via Hypothesis. Companion to the unit suite at
``tests/unit/llm/test_provider_health.py``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.llm._provider_health import (
    scan_llm_provider_health,
)
from sovyx.llm._provider_registry import LLMProviderKey

_KEY_VALUES = [key.value for key in LLMProviderKey]
_CLOUD_ENV_VARS = [key.env_var for key in LLMProviderKey if key.is_cloud]


@st.composite
def _env_map(draw: st.DrawFn) -> dict[str, str]:
    chosen = draw(st.sets(st.sampled_from(_CLOUD_ENV_VARS)))
    return {env_var: f"sk-test-{env_var.lower()}" for env_var in chosen}


@st.composite
def _ollama_models(draw: st.DrawFn) -> tuple[str, ...]:
    return tuple(
        draw(
            st.lists(
                st.text(
                    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-.:",
                    min_size=3,
                    max_size=24,
                ),
                min_size=0,
                max_size=8,
            ),
        ),
    )


class TestPerProviderCount:
    @given(env=_env_map())
    @settings(max_examples=200, deadline=2000)
    def test_per_provider_count_equals_member_count(self, env: dict[str, str]) -> None:
        report = scan_llm_provider_health(
            env,
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        assert len(report.per_provider) == len(_KEY_VALUES)


class TestConfiguredCountBounds:
    @given(env=_env_map(), ollama_ping=st.booleans())
    @settings(max_examples=200, deadline=2000)
    def test_configured_count_bounded(
        self,
        env: dict[str, str],
        ollama_ping: bool,
    ) -> None:
        report = scan_llm_provider_health(
            env,
            ollama_ping_result=ollama_ping,
            ollama_models=() if ollama_ping else None,
            default_provider="",
            default_model="",
        )
        assert 0 <= report.configured_count <= len(_KEY_VALUES)


class TestAvailableCountBounds:
    @given(env=_env_map(), ollama_ping=st.booleans(), models=_ollama_models())
    @settings(max_examples=200, deadline=2000)
    def test_available_le_configured(
        self,
        env: dict[str, str],
        ollama_ping: bool,
        models: tuple[str, ...],
    ) -> None:
        report = scan_llm_provider_health(
            env,
            ollama_ping_result=ollama_ping,
            ollama_models=models if ollama_ping else None,
            default_provider="",
            default_model="",
        )
        assert report.available_count <= report.configured_count


class TestVerdictDeterminism:
    @given(env=_env_map(), ollama_ping=st.booleans(), models=_ollama_models())
    @settings(max_examples=200, deadline=2000)
    def test_re_scan_yields_same_verdict(
        self,
        env: dict[str, str],
        ollama_ping: bool,
        models: tuple[str, ...],
    ) -> None:
        models_input = models if ollama_ping else None
        report_1 = scan_llm_provider_health(
            env,
            ollama_ping_result=ollama_ping,
            ollama_models=models_input,
            default_provider="",
            default_model="",
        )
        report_2 = scan_llm_provider_health(
            env,
            ollama_ping_result=ollama_ping,
            ollama_models=models_input,
            default_provider="",
            default_model="",
        )
        assert report_1.verdict == report_2.verdict
        assert report_1.configured_count == report_2.configured_count
        assert report_1.available_count == report_2.available_count


class TestScanDurationBounds:
    @given(env=_env_map())
    @settings(max_examples=200, deadline=2000)
    def test_scan_duration_non_negative_bounded(self, env: dict[str, str]) -> None:
        report = scan_llm_provider_health(
            env,
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        assert report.scan_duration_ms >= 0.0
        # Rule #12 / AP #31: the invariant is non-negative + finite/sane-ceiling,
        # NOT speed. A tight wall-clock bound flakes on slow/contended CI runners
        # (windows-latest measured 795 ms for an in-memory scan). Perf is the
        # perf-gate's job; this generous ceiling still catches a runaway scan.
        assert report.scan_duration_ms < 30_000.0
