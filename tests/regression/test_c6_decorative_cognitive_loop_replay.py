"""Forensic-replay regression — Mission C6 §F3 / §T2.11 + §T4.5.

Part 1 (Phase 1.B) — verdict-refinement half: replays operator log
``c:\\Users\\guipe\\Downloads\\docs_teste.txt`` L373..L377 sequence as a
synthetic env-with-zero-keys + Ollama-not-running boot, asserts the
composite store records the refined verdict (``no_provider_configured``
when no default; ``ollama_unreachable`` when default=ollama and ping
fails). Pre-mission HEAD recorded a single hardcoded ``no_llm_provider``
reason for BOTH cases — the test confirms C6 distinguishes them.

Part 2 (Phase 1.D) — cognitive-loop-dependency-gate half — lands in the
v0.49.3 atomic ship and asserts zero ``cognitive.{perceive,attend,think,
act,reflect}`` events fire when LLM is absent + the synthetic
``ActionResult(failed=True, reason="cognitive_dependency_missing")``
short-circuit fires within 100 ms.

Forensic anchor: ``docs-internal/FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md``
§C6 + §H5.
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


class TestC6OperatorL373L377Replay:
    """Replays L373..L377 operator-log sequence as a synthetic boot."""

    def test_l373_ollama_ping_failed_l374_no_llm_provider_yields_no_provider_configured(
        self,
    ) -> None:
        """L373 ``ollama_ping_failed`` + L374 ``no_llm_provider_detected``
        (operator's actual case at v0.43.1) → refined verdict is
        ``NO_PROVIDER_CONFIGURED`` because mind.yaml default is empty.

        Pre-mission: composite store had ``reason="no_llm_provider"``.
        Post-mission: refined to ``reason="no_provider_configured"`` with
        the more actionable ``Run sovyx llm setup`` chip.
        """
        # Replay: empty env (no cloud keys) + Ollama ping failed + no default
        # in mind config (operator's actual state).
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",  # Operator's mind.yaml had no default
            default_model="",
        )
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
        assert report.configured_count == 0
        assert report.available_count == 0

        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.axis == "llm"
        assert entry.reason == "no_provider_configured"
        assert entry.severity == "critical"

        # The refined chip surface — operator now sees BOTH paths:
        # "Run sovyx llm setup" (navigate) + "Install Ollama" (external).
        chip_targets = {c.target for c in entry.action_chips}
        assert "/settings/providers" in chip_targets
        assert "https://ollama.ai" in chip_targets

    def test_l377_llm_router_config_empty_providers_no_regression_signal(self) -> None:
        """L377 ``llm_router_config providers=[] default_model='' default_provider=''``
        means the router booted with no providers. The discovery scan's
        ``configured_count`` reflects this — anti-pattern #44 dependency-
        gated workers consult this state via
        ``LLMRouter.has_available_provider()``."""
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        assert report.available_count == 0
        # No provider was previously known good — this is a fresh install,
        # NOT a regression. The verdict reflects that distinction.
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED

    def test_operator_with_default_ollama_yields_unreachable_regression_signal(
        self,
    ) -> None:
        """Distinct from the operator's actual v0.43.1 case: this synthetic
        scenario asserts that if the operator HAD configured Ollama as default
        (mind.yaml ``default_provider: ollama``) AND the daemon is now down,
        the verdict promotes to ``OLLAMA_UNREACHABLE`` (regression signal).

        Pre-mission this case collapsed to ``no_llm_provider`` with the
        wrong remediation chip ("Install Ollama" when Ollama is already
        installed). Post-mission the refined chip says "Start Ollama".
        """
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report.verdict is DiscoveryVerdict.OLLAMA_UNREACHABLE

        dispatch_llm_discovery_verdict(report)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "ollama_unreachable"
        # Critical distinction: "Start Ollama" (NOT "Install Ollama")
        chip_targets = {c.target for c in entry.action_chips}
        assert "https://ollama.ai/docs/start" in chip_targets

    def test_l412_cognitive_loop_started_would_be_decorative(self) -> None:
        """Pre-Phase-1.D: ``cognitive_loop_started`` fired at L412 with
        ``providers=[]`` AND ``cognitive-gate-worker`` ran 439 s with zero
        perception events. The structural gap is closed by Phase 1.D
        (CognitiveLoop dependency gate); this assertion documents that
        the verdict scanner sees the precondition.

        Anti-pattern #44 dependency-gated workers consult
        ``has_available_provider()`` to refuse silent no-op work.
        """
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        # The dependency-readiness signal Phase 1.D consumes:
        assert report.available_count == 0
        # The composite-banner producer Phase 1.B installs:
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        # An operator opening the dashboard now sees ONE actionable banner
        # instead of staring at a clean lifecycle log.


class TestC6PreMissionWouldHaveFailed:
    """Documents what the pre-mission scanner could NOT distinguish.

    Each test fixes the input shape that pre-mission HEAD would have
    classified as ``no_llm_provider`` (the single C4 reason) and asserts
    the post-mission scanner produces a distinct refined token.

    If a future refactor RE-COLLAPSES these into a single reason, these
    tests FAIL — the refined taxonomy is enforced at the test layer.
    """

    def test_no_keys_no_ollama_no_default_yields_distinct_reason(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        assert report.verdict.value == "no_provider_configured"

    def test_no_keys_no_ollama_with_default_yields_distinct_reason(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert report.verdict.value == "ollama_unreachable"

    def test_no_keys_ollama_running_no_models_yields_distinct_reason(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=True,
            ollama_models=(),
            default_provider="",
            default_model="",
        )
        assert report.verdict.value == "ollama_no_models"

    def test_all_three_were_collapsed_pre_mission_now_distinct(self) -> None:
        """The three reasons produced by the three above scenarios are all
        distinct post-mission. Pre-mission they were the single
        ``no_llm_provider`` reason."""
        reasons = {
            scan_llm_provider_health(
                env={},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="",
                default_model="",
            ).verdict.value,
            scan_llm_provider_health(
                env={},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="ollama",
                default_model="llama3.1:latest",
            ).verdict.value,
            scan_llm_provider_health(
                env={},
                ollama_ping_result=True,
                ollama_models=(),
                default_provider="",
                default_model="",
            ).verdict.value,
        }
        assert len(reasons) == 3
        assert reasons == {
            "no_provider_configured",
            "ollama_unreachable",
            "ollama_no_models",
        }
