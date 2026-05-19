"""CaptureIntegrityCoordinator bypass + verdict metrics.

Phase 5.F.10 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the v1.3 §14 CaptureIntegrityCoordinator
observability surface — 5 record helpers + 4 metric-name constants
spanning:

* :func:`record_bypass_strategy_verdict` — per-strategy outcome
  classification (applied_healthy / applied_still_dead /
  failed_to_apply / not_applicable). Mirrors to ``_bypass_tier_state``
  for the dashboard's bypass-tier counter pair.
* :func:`record_bypass_probe_wait_ms` — post-apply probe wait
  duration (v1.3 §14.E1).
* :func:`record_bypass_probe_window_contaminated` — degraded but
  not-contaminated post-apply probe (v1.3 §14.E1 tuple-mark design).
* :func:`record_bypass_improvement_resolution` — v1.3 §14.E2
  improvement-heuristic resolution (VAD_MUTE + spectral rolloff
  improved).
* :func:`record_capture_integrity_verdict` — the underlying
  :class:`IntegrityVerdict` classifier outcome (pre_bypass /
  post_bypass / recheck phase).

Anti-pattern #20 covered: parent module ``voice/health/_metrics.py``
re-exports every symbol so production callers
(``CaptureIntegrityCoordinator``, ``CaptureIntegrityProbe``) and test
references at ``sovyx.voice.health._metrics.<name>`` continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from sovyx.observability.metrics import get_metrics
from sovyx.voice.health import _bypass_tier_state

# ── Stable name constants (v1.3 §14 CaptureIntegrityCoordinator) ────

METRIC_BYPASS_STRATEGY_VERDICTS = "sovyx.voice.health.bypass_strategy.verdicts"
METRIC_BYPASS_PROBE_WAIT_MS = "sovyx.voice.health.bypass.probe_wait_ms"
METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED = "sovyx.voice.health.bypass.probe_window_contaminated"
METRIC_BYPASS_IMPROVEMENT_RESOLUTION = "sovyx.voice.health.bypass.improvement_resolution"
METRIC_CAPTURE_INTEGRITY_VERDICTS = "sovyx.voice.health.capture_integrity.verdicts"
# Mission C1 §T1.9 metric constants — see MetricsRegistry definitions.
METRIC_VAD_FRONTEND_RESET_OUTCOMES = "sovyx.voice.health.vad_frontend_reset.outcomes"
METRIC_COORDINATOR_OUTCOMES = "sovyx.voice.health.coordinator.outcomes"
METRIC_QUARANTINE_REASON_DUAL_EMIT = "sovyx.voice.health.quarantine.reason_dual_emit"
# Mission H3 §T2.6 ADR-D20 — replaces the C1 LENIENT-phase
# ``METRIC_QUARANTINE_REASON_DUAL_EMIT`` counter at v0.53.0 STRICT. Fires
# once per ``EndpointQuarantine.add()`` call with the verdict / diagnosis
# source attribution + resolved_reason output + H2 platform metadata.
METRIC_QUARANTINE_RESOLUTION = "sovyx.voice.health.quarantine_resolution"


# ── Record helpers ──────────────────────────────────────────────────


def record_bypass_strategy_verdict(
    *,
    strategy: str,
    verdict: str,
    reason: str = "",
) -> None:
    """Record one per-strategy outcome from :class:`CaptureIntegrityCoordinator`.

    Args:
        strategy: Stable strategy identifier (``"win.wasapi_exclusive"``,
            ``"win.disable_sysfx"``, ``"linux.alsa_hw_direct"``,
            ``"macos.coreaudio_vpio_off"``).
        verdict: ``"applied_healthy"`` | ``"applied_still_dead"`` |
            ``"failed_to_apply"`` | ``"not_applicable"``. Matches the
            :class:`BypassVerdict` string values.
        reason: Optional low-cardinality tag — eligibility rejection
            reason (``"not_win32_platform"``) or apply-failure token
            (``"exclusive_downgraded_to_shared"``). Empty string when
            unset. Stable across minor versions so dashboards can
            key on it.
    """
    _bypass_tier_state.mark_strategy_verdict(strategy=strategy, verdict=verdict)
    counter = getattr(get_metrics(), "voice_health_bypass_strategy_verdicts", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "strategy": strategy or "unknown",
            "verdict": verdict or "unknown",
            "reason": reason or "",
        },
    )


def record_bypass_probe_wait_ms(*, strategy: str, wait_ms: float) -> None:
    """Record the post-apply probe wait duration (v1.3 §14.E1).

    Fed from :meth:`CaptureIntegrityCoordinator.handle_deaf_signal` —
    measures the wall-clock span between ``strategy.apply()`` returning
    and :meth:`AudioCaptureTask.tap_frames_since_mark` yielding enough
    post-apply frames for classification.

    Args:
        strategy: Stable strategy identifier (same token used by
            :func:`record_bypass_strategy_verdict`).
        wait_ms: Observed duration in milliseconds. Clamped at zero so
            a negative clock skew does not poison the histogram.
    """
    histogram = getattr(get_metrics(), "voice_health_bypass_probe_wait_ms", None)
    if histogram is None:
        return
    histogram.record(
        max(0.0, float(wait_ms)),
        attributes={"strategy": strategy or "unknown"},
    )


def record_bypass_probe_window_contaminated(*, strategy: str) -> None:
    """Record a degraded-but-not-contaminated post-apply probe (v1.3 §14.E1).

    The tuple-mark design eliminates pre-apply frame leakage entirely;
    this counter instead fires when the coordinator had to classify
    *fewer* than ``min_samples`` post-apply frames because the tap
    timed out. Distinct signal from a failed apply — the fix was
    applied, but the verdict carries reduced statistical weight.
    """
    counter = getattr(get_metrics(), "voice_health_bypass_probe_window_contaminated", None)
    if counter is None:
        return
    counter.add(1, attributes={"strategy": strategy or "unknown"})


def record_bypass_improvement_resolution(*, strategy: str) -> None:
    """Record a v1.3 §14.E2 improvement-heuristic resolution.

    Fires when the post-apply verdict is ``VAD_MUTE`` yet the spectral
    rolloff improved by at least
    :attr:`VoiceTuningConfig.improvement_rolloff_factor` — the
    coordinator treats the attempt as resolved because the spectrum
    demonstrates the fix worked even though the user stopped speaking
    during settle. Label ``strategy`` matches
    :func:`record_bypass_strategy_verdict`.
    """
    counter = getattr(get_metrics(), "voice_health_bypass_improvement_resolution", None)
    if counter is None:
        return
    counter.add(1, attributes={"strategy": strategy or "unknown"})


def record_capture_integrity_verdict(
    *,
    verdict: str,
    phase: str,
) -> None:
    """Record a :class:`CaptureIntegrityProbe` classification.

    Args:
        verdict: :class:`IntegrityVerdict` value — ``"healthy"`` |
            ``"apo_degraded"`` | ``"driver_silent"`` | ``"vad_mute"`` |
            ``"vad_frontend_dead"`` | ``"format_mismatch"`` |
            ``"inconclusive"``. Mission C1 v0.44.0 added the two
            middle values — see :class:`IntegrityVerdict` docstring.
        phase: ``"pre_bypass"`` (coordinator probe before apply),
            ``"post_bypass"`` (coordinator probe after apply + settle),
            ``"recheck"`` (watchdog APO recheck loop). Low-cardinality.
    """
    counter = getattr(get_metrics(), "voice_health_capture_integrity_verdicts", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "verdict": verdict or "unknown",
            "phase": phase or "unknown",
        },
    )


# ── Mission C1 §T1.9 — new helpers ──────────────────────────────────


def record_vad_frontend_reset_outcome(
    *,
    step: str,
    success: bool,
    elapsed_ms: float,
    reason: str = "",
) -> None:
    """Record one step of the VAD-frontend reset ladder (Mission C1 T1.4).

    The ladder runs L1..L5 in order (Silero reset → re-instantiate →
    FrameNormalizer engage → AGC2 floor lift → fallback VAD) and stops
    on the first step whose post-step integrity re-probe returns
    HEALTHY. This helper fires once per attempted step so dashboards
    can compute per-step success rates and tune the ladder ordering
    against operator-hardware data.

    Args:
        step: ``"silero_reset"`` | ``"silero_reinstantiate"`` |
            ``"normalizer_engage"`` | ``"agc2_floor_lift"`` |
            ``"fallback_vad"``. Low-cardinality (5 values).
        success: True iff the post-step integrity re-probe returned
            HEALTHY (or, for the terminal fallback_vad step, iff the
            pipeline accepted the fallback). False otherwise.
        elapsed_ms: Wall-clock time the step took. Used to compute
            ladder-step latency distributions.
        reason: Optional low-cardinality tag — failure-mode token when
            ``success=False`` (e.g. ``"onnx_session_init_failed"``,
            ``"normalizer_already_engaged"``). Empty when ``success=True``.
    """
    counter = getattr(get_metrics(), "voice_health_vad_frontend_reset_outcomes", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "step": step or "unknown",
            "success": "true" if success else "false",
            "reason": reason or "",
            "elapsed_ms_bucket": _bucket_elapsed_ms(elapsed_ms),
        },
    )


def record_coordinator_benign_skip(*, verdict: str, reason: str = "") -> None:
    """Record a coordinator early-exit on a benign verdict (Mission C1 T1.3).

    Fires when :meth:`CaptureIntegrityCoordinator.handle_deaf_signal`
    returns empty outcomes because the pre-bypass probe classified the
    signal as benign (HEALTHY false-alarm or VAD_MUTE — user not
    speaking). Distinct from
    :func:`record_bypass_strategy_verdict` because no strategy ran;
    distinct from :func:`record_coordinator_outcome` because no
    BypassOutcome was emitted at all.

    Args:
        verdict: :class:`IntegrityVerdict` value that triggered the
            skip (``"healthy"`` for false-alarm, ``"vad_mute"`` for
            user-not-speaking).
        reason: Optional low-cardinality tag (``"false_alarm"`` |
            ``"user_not_speaking"``).
    """
    counter = getattr(get_metrics(), "voice_health_coordinator_outcomes", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "kind": "benign_skip",
            "verdict": verdict or "unknown",
            "reason": reason or "",
        },
    )


def record_coordinator_outcome(*, verdict: str, reason: str = "") -> None:
    """Record a coordinator non-strategy outcome (Mission C1 T1.3 + T1.6).

    Fires when :meth:`CaptureIntegrityCoordinator.handle_deaf_signal`
    returns a :class:`BypassOutcome` whose verdict is NOT a strategy
    outcome — specifically
    :attr:`BypassVerdict.CASCADE_REEVALUATION_REQUESTED` or
    :attr:`BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED`. These are
    coordinator dispatch decisions, not strategy attempts; they MUST
    NOT route through :func:`record_bypass_strategy_verdict` because
    that helper mirrors to :mod:`_bypass_tier_state` and would falsely
    inflate strategy attempt counters.

    Args:
        verdict: :class:`BypassVerdict` value
            (``"cascade_reevaluation_requested"`` |
            ``"normalizer_engagement_requested"``).
        reason: Optional low-cardinality tag — typically the
            :class:`IntegrityVerdict` that triggered the dispatch
            (``"driver_silent"`` → cascade reevaluation,
            ``"format_mismatch"`` → normalizer engagement).
    """
    counter = getattr(get_metrics(), "voice_health_coordinator_outcomes", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "kind": verdict or "unknown",
            "verdict": verdict or "unknown",
            "reason": reason or "",
        },
    )


def record_quarantine_reason_dual_emit(
    *,
    legacy_reason: str,
    derived_reason: str,
) -> None:
    """Mission C1 §T1.7 LENIENT-phase calibration counter — TEMPORARY.

    Fires once per quarantine event during the v0.44.x LENIENT phase
    whenever the verdict-derived reason differs from the legacy
    ``"apo_degraded"`` default. Operators read this to validate the
    verdict→reason map before the v0.45.0 STRICT flip drops the legacy
    field.

    **Removal scheduled for v0.45.0** — both this helper and the
    underlying :attr:`MetricsRegistry.voice_health_quarantine_reason_dual_emit`
    counter are deleted in the STRICT-flip commit.

    Args:
        legacy_reason: The pre-mission default (typically
            ``"apo_degraded"`` for the duration of LENIENT — kept on
            :class:`QuarantineEntry.reason` so downstream consumers
            don't break).
        derived_reason: The verdict-derived value (``"apo_degraded"``
            | ``"vad_frontend_dead"`` | ``"format_mismatch"``). Stored
            on :class:`QuarantineEntry.derived_reason`.
    """
    if legacy_reason == derived_reason:
        # No drift — nothing to calibrate. The counter only fires when
        # the mapping diverges from the legacy default.
        return
    counter = getattr(get_metrics(), "voice_health_quarantine_reason_dual_emit", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "legacy_reason": legacy_reason or "unknown",
            "derived_reason": derived_reason or "unknown",
        },
    )


def record_quarantine_resolution_outcome(
    *,
    verdict: str = "",
    diagnosis: str = "",
    resolved_reason: str,
    platform: str = "",
    bypass_family: str = "",
) -> None:
    """Mission H3 §ADR-D20 — verdict/diagnosis → resolved_reason counter.

    Fires once per :meth:`EndpointQuarantine.add` call. Replaces the C1
    LENIENT-phase :func:`record_quarantine_reason_dual_emit` calibration
    counter at v0.53.0 STRICT.

    Args:
        verdict: :class:`IntegrityVerdict` value when the producer is
            the coordinator's verdict-router (capture-integrity path).
            Empty string when the producer is the cascade-layer.
        diagnosis: :class:`Diagnosis` value when the producer is the
            cascade-layer (kernel-invalidated rechecker / factory
            integration). Empty string when the producer is the
            coordinator.
        resolved_reason: The :class:`QuarantineReason` value returned by
            :func:`resolve_reason_from_verdict` /
            :func:`resolve_reason_from_diagnosis`.
        platform: Optional H2-resolved platform metadata (``"linux"`` |
            ``"windows"`` | ``"darwin"`` | ``"other"``). Inherits from
            the bypass coordinator's ``_platform_key`` field where
            available.
        bypass_family: Optional H2-resolved bypass-family metadata
            (``"voice_clarity"`` | ``"alsa_capture_chain"`` | ...).
    """
    counter = getattr(get_metrics(), "voice_health_quarantine_resolution", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "verdict": verdict or "n/a",
            "diagnosis": diagnosis or "n/a",
            "resolved_reason": resolved_reason or "unknown",
            "platform": platform or "unknown",
            "bypass_family": bypass_family or "unknown",
        },
    )


# ── Internal helpers ────────────────────────────────────────────────


def _bucket_elapsed_ms(elapsed_ms: float) -> str:
    """Bucket elapsed-ms into low-cardinality label values.

    Histogram instruments are the right home for raw latency
    distributions, but the per-step counter benefits from a coarse
    bucket label so dashboards can split "fast success" from "slow
    success" without a histogram aggregation. Buckets follow the
    standard OTel low-latency convention: <10ms, <100ms, <1s, >=1s.
    """
    elapsed_ms = max(0.0, float(elapsed_ms))
    if elapsed_ms < 10.0:
        return "lt_10ms"
    if elapsed_ms < 100.0:
        return "lt_100ms"
    if elapsed_ms < 1_000.0:
        return "lt_1s"
    return "gte_1s"


__all__ = [
    "METRIC_BYPASS_IMPROVEMENT_RESOLUTION",
    "METRIC_BYPASS_PROBE_WAIT_MS",
    "METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED",
    "METRIC_BYPASS_STRATEGY_VERDICTS",
    "METRIC_CAPTURE_INTEGRITY_VERDICTS",
    "METRIC_COORDINATOR_OUTCOMES",
    "METRIC_QUARANTINE_REASON_DUAL_EMIT",
    "METRIC_QUARANTINE_RESOLUTION",
    "METRIC_VAD_FRONTEND_RESET_OUTCOMES",
    "record_bypass_improvement_resolution",
    "record_bypass_probe_wait_ms",
    "record_bypass_probe_window_contaminated",
    "record_bypass_strategy_verdict",
    "record_capture_integrity_verdict",
    "record_coordinator_benign_skip",
    "record_coordinator_outcome",
    "record_quarantine_reason_dual_emit",
    "record_quarantine_resolution_outcome",
    "record_vad_frontend_reset_outcome",
]
