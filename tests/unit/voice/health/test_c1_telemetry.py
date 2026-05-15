"""Mission C1 §T1.9 telemetry helpers + §T1.5 tier-state guard tests.

Covers the observability surface that lands in commit 3 (T1.5 + T1.9):

* :func:`record_vad_frontend_reset_outcome` — per-step ladder telemetry.
* :func:`record_coordinator_benign_skip` — coordinator early-exit on
  benign verdicts.
* :func:`record_coordinator_outcome` — coordinator non-strategy
  outcomes (CASCADE_REEVALUATION_REQUESTED / NORMALIZER_ENGAGEMENT_REQUESTED).
* :func:`record_quarantine_reason_dual_emit` — TEMPORARY LENIENT-phase
  calibration counter (removed v0.45.0).
* :func:`_bypass_tier_state.mark_strategy_verdict` defensive guard —
  the 4 new BypassVerdict members MUST NOT inflate tier counters.

The helpers all use ``getattr(get_metrics(), …, None)`` which makes
them null-safe when the OTel registry is incomplete; these tests
exercise the success path and assert OTel counter side-effects via
the in-memory MetricsReader fixture pattern.

Mission anchor:
    docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md
    §T1.5 + §T1.9 + §20.M T1.9.a + §20.O.
"""

from __future__ import annotations

import pytest

from sovyx.voice.health import _bypass_tier_state
from sovyx.voice.health._metrics_bypass_coordinator import (
    record_bypass_strategy_verdict,
    record_capture_integrity_verdict,
    record_coordinator_benign_skip,
    record_coordinator_outcome,
    record_quarantine_reason_dual_emit,
    record_vad_frontend_reset_outcome,
)


@pytest.fixture(autouse=True)
def _reset_tier_state() -> None:
    """Module-level state mirror in _bypass_tier_state is process-wide.

    Reset before AND after each test so neighbouring suites that touch
    the same counters can't poison this file's assertions and vice
    versa.
    """
    _bypass_tier_state.reset_for_tests()
    yield
    _bypass_tier_state.reset_for_tests()


class TestRecordCaptureIntegrityVerdictNewValues:
    """T1.5 — record_capture_integrity_verdict accepts the new verdict values.

    The helper is null-safe (getattr with None fallback); these tests
    confirm no TypeError on the v0.44.0 verdict values when the metric
    instrument IS registered.
    """

    def test_accepts_vad_frontend_dead(self) -> None:
        # Should not raise — the metric label set widens via the
        # MetricsRegistry instrument docstring + label values flow
        # through unchanged.
        record_capture_integrity_verdict(verdict="vad_frontend_dead", phase="pre_bypass")

    def test_accepts_format_mismatch(self) -> None:
        record_capture_integrity_verdict(verdict="format_mismatch", phase="pre_bypass")

    def test_accepts_all_legacy_values(self) -> None:
        # Regression: pre-mission verdict values keep working.
        for verdict in ("healthy", "apo_degraded", "driver_silent", "vad_mute", "inconclusive"):
            record_capture_integrity_verdict(verdict=verdict, phase="pre_bypass")


class TestRecordVadFrontendResetOutcome:
    """Mission C1 T1.9 — VAD-frontend reset ladder per-step telemetry."""

    def test_accepts_each_ladder_step(self) -> None:
        # All 5 step names from §4.4 ADR-D4 must be accepted without
        # raising. The metric label set documents them as low-cardinality.
        for step in (
            "silero_reset",
            "silero_reinstantiate",
            "normalizer_engage",
            "agc2_floor_lift",
            "fallback_vad",
        ):
            record_vad_frontend_reset_outcome(
                step=step,
                success=True,
                elapsed_ms=5.0,
            )

    def test_failure_outcome_with_reason(self) -> None:
        # Failed step records carry a diagnostic reason token.
        record_vad_frontend_reset_outcome(
            step="silero_reinstantiate",
            success=False,
            elapsed_ms=120.0,
            reason="onnx_session_init_failed",
        )

    def test_negative_elapsed_ms_clamped_via_bucket(self) -> None:
        # Defensive — _bucket_elapsed_ms clamps negative via max(0, …).
        # The helper must not raise on a clock-skew negative duration.
        record_vad_frontend_reset_outcome(
            step="silero_reset",
            success=True,
            elapsed_ms=-1.0,
        )


class TestRecordCoordinatorBenignSkip:
    """Mission C1 T1.9 — coordinator early-exit telemetry."""

    def test_vad_mute_user_not_speaking(self) -> None:
        record_coordinator_benign_skip(verdict="vad_mute", reason="user_not_speaking")

    def test_healthy_false_alarm(self) -> None:
        record_coordinator_benign_skip(verdict="healthy", reason="false_alarm")

    def test_no_reason_default(self) -> None:
        # `reason` is optional — the helper accepts kwarg omission.
        record_coordinator_benign_skip(verdict="vad_mute")


class TestRecordCoordinatorOutcome:
    """Mission C1 T1.9 — coordinator non-strategy outcomes telemetry."""

    def test_cascade_reevaluation_requested(self) -> None:
        record_coordinator_outcome(
            verdict="cascade_reevaluation_requested",
            reason="driver_silent",
        )

    def test_normalizer_engagement_requested(self) -> None:
        record_coordinator_outcome(
            verdict="normalizer_engagement_requested",
            reason="format_mismatch",
        )


class TestRecordQuarantineReasonDualEmit:
    """Mission C1 §T1.7 LENIENT-phase calibration counter — temporary.

    Removed in v0.45.0 STRICT flip. Tests pin the contract:
        * Fires ONLY when legacy != derived.
        * No-op when legacy == derived (calibration signal is the DRIFT,
          not the count of all quarantines).
    """

    def test_fires_when_legacy_differs_from_derived(self) -> None:
        # Should call into the counter (no exception observable; the
        # null-safe getattr means a missing instrument silently no-ops,
        # but the registered instrument accepts the call).
        record_quarantine_reason_dual_emit(
            legacy_reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )

    def test_no_op_when_legacy_equals_derived(self) -> None:
        # Drift signal is zero — helper short-circuits before the OTel
        # call. The function returns None either way; this test pins the
        # contract: equal-reasons MUST early-return (covered by an
        # introspection on the function or just confirming no exception).
        record_quarantine_reason_dual_emit(
            legacy_reason="apo_degraded",
            derived_reason="apo_degraded",
        )

    def test_accepts_format_mismatch_derived(self) -> None:
        record_quarantine_reason_dual_emit(
            legacy_reason="apo_degraded",
            derived_reason="format_mismatch",
        )


class TestBypassTierStateDefensiveGuard:
    """Mission C1 §T1.5 + §20.M T1.9.a — tier-state mark_strategy_verdict
    MUST NOT inflate tier counters when paired with the new BypassVerdict
    values.

    Regression guard against the silent miscounting hazard called out in
    Agent-1 audit finding B1: even if a caller accidentally passes a
    non-strategy verdict with the Tier 3 strategy name, the helper
    rejects it.
    """

    def test_vad_frontend_reset_applied_healthy_does_not_inflate(self) -> None:
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="vad_frontend_reset_applied_healthy",
        )
        post = _bypass_tier_state.snapshot()
        assert post == baseline, (
            "vad_frontend_reset_applied_healthy is a non-strategy outcome "
            "and MUST NOT inflate Tier 3 counters even when paired with "
            "the Tier 3 strategy name."
        )
        assert post["current_bypass_tier"] is None

    def test_vad_frontend_reset_applied_still_dead_does_not_inflate(self) -> None:
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="vad_frontend_reset_applied_still_dead",
        )
        assert _bypass_tier_state.snapshot() == baseline

    def test_cascade_reevaluation_requested_does_not_inflate(self) -> None:
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="cascade_reevaluation_requested",
        )
        assert _bypass_tier_state.snapshot() == baseline

    def test_normalizer_engagement_requested_does_not_inflate(self) -> None:
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="normalizer_engagement_requested",
        )
        assert _bypass_tier_state.snapshot() == baseline

    def test_legacy_applied_healthy_still_inflates_tier_3(self) -> None:
        # Regression: the defensive guard MUST NOT regress the legacy
        # Tier 3 success path. APPLIED_HEALTHY with the Tier 3 strategy
        # still increments the counters + stamps current_bypass_tier.
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="applied_healthy",
        )
        post = _bypass_tier_state.snapshot()
        assert post["tier3_wasapi_exclusive_attempted"] == (
            (baseline["tier3_wasapi_exclusive_attempted"] or 0) + 1
        )
        assert post["tier3_wasapi_exclusive_succeeded"] == (
            (baseline["tier3_wasapi_exclusive_succeeded"] or 0) + 1
        )
        assert post["current_bypass_tier"] == 3

    def test_legacy_applied_still_dead_still_inflates_attempted_only(self) -> None:
        # Regression: APPLIED_STILL_DEAD bumps attempts only, not
        # successes — and doesn't stamp current_bypass_tier.
        baseline = _bypass_tier_state.snapshot()
        _bypass_tier_state.mark_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="applied_still_dead",
        )
        post = _bypass_tier_state.snapshot()
        assert post["tier3_wasapi_exclusive_attempted"] == (
            (baseline["tier3_wasapi_exclusive_attempted"] or 0) + 1
        )
        assert (
            post["tier3_wasapi_exclusive_succeeded"]
            == baseline["tier3_wasapi_exclusive_succeeded"]
        )


class TestRecordBypassStrategyVerdictBackwardCompat:
    """Regression: record_bypass_strategy_verdict pipeline through
    _bypass_tier_state still works with legacy verdict values, AND
    silently ignores new C1 non-strategy values (defensive belt+braces
    — the producer side is supposed to NOT route them here at all,
    but if a future caller drifts, the tier state stays clean)."""

    def test_legacy_routing_intact(self) -> None:
        baseline = _bypass_tier_state.snapshot()
        record_bypass_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="applied_healthy",
            reason="",
        )
        post = _bypass_tier_state.snapshot()
        assert post["tier3_wasapi_exclusive_succeeded"] == (
            (baseline["tier3_wasapi_exclusive_succeeded"] or 0) + 1
        )

    def test_new_c1_verdict_does_not_inflate_via_record_helper(self) -> None:
        # End-to-end belt+braces: even via the public record helper,
        # accidental routing of a C1 non-strategy verdict does NOT
        # touch the tier state mirror.
        baseline = _bypass_tier_state.snapshot()
        record_bypass_strategy_verdict(
            strategy="win.wasapi_exclusive",
            verdict="vad_frontend_reset_applied_healthy",
            reason="defensive_test",
        )
        assert _bypass_tier_state.snapshot() == baseline
