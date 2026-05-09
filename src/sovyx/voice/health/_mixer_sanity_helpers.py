"""Pure helpers for L2.5 mixer-sanity orchestrator.

Phase 5.F.12 god-file extraction from
``voice/health/_mixer_sanity.py`` (anti-pattern #16). Owns 4 pure
helper functions used by the ``_SanityOrchestrator`` state machine:

* :func:`_check_validation_gates` — every declared gate must pass.
* :func:`_classify_regime_heuristically` — fallback regime
  classification when no KB profile matches.
* :func:`_diagnosis_for_regime` — Literal regime → Diagnosis dispatch
  with mypy-strict ``assert_never`` exhaustiveness.
* :func:`_defer_platform_result` — canonical DEFERRED_PLATFORM
  result shape for non-Linux short-circuits.

All four helpers are pure / observability-free / side-effect-free.
``_SanityOrchestrator`` calls them via the parent module's namespace
(re-exported below in the parent file), so production paths resolve
correctly.

Anti-pattern #20 covered: parent module ``voice/health/_mixer_sanity.py``
re-exports every symbol so the in-class call sites
(``_SanityOrchestrator.run`` + ``_SanityOrchestrator._step_classify`` +
``_SanityOrchestrator._step_validate`` + ``_check_and_maybe_heal_impl``)
continue to resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sovyx.voice.health.contract import (
    Diagnosis,
    MixerSanityDecision,
    MixerSanityResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice.health.contract import (
        MixerCardSnapshot,
        MixerValidationMetrics,
        ValidationGates,
    )


def _check_validation_gates(
    metrics: MixerValidationMetrics,
    gates: ValidationGates,
) -> bool:
    """Every declared gate must pass — any single failure → False."""
    lo, hi = gates.rms_dbfs_range
    if not (lo <= metrics.rms_dbfs <= hi):
        return False
    if metrics.peak_dbfs > gates.peak_dbfs_max:
        return False
    if metrics.snr_db_vocal_band < gates.snr_db_vocal_band_min:
        return False
    if metrics.silero_max_prob < gates.silero_prob_min:
        return False
    return metrics.wake_word_stage2_prob >= gates.wake_word_stage2_prob_min


def _classify_regime_heuristically(
    snapshot: Sequence[MixerCardSnapshot],
) -> Literal["saturation", "attenuation", "mixed", "healthy", "unknown"]:
    """Fallback regime classification when no KB profile matches.

    Looks at the probe's own saturation flags + aggregated boost dB
    to split ``"saturation"`` / ``"healthy"`` / ``"unknown"``.
    Attenuation has no reliable signal without KB knowledge (a low
    Capture can be intentional), so we return ``"unknown"`` rather
    than guess.
    """
    if not snapshot:
        return "unknown"
    for card in snapshot:
        if card.saturation_warning:
            return "saturation"
    # No saturation flags → assume healthy; probe didn't surface any
    # obvious red flags.
    return "healthy"


def _diagnosis_for_regime(
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"],
) -> Diagnosis:
    """Map a regime label to the L2.5 Diagnosis value.

    Paranoid-QA R3 HIGH #8: exhaustiveness enforced via
    ``assert_never`` — a future edit that adds a new ``Literal``
    value to the regime type WITHOUT updating this dispatch will
    fail mypy-strict at the ``assert_never(regime)`` call. Earlier
    the trailing ``return Diagnosis.MIXER_UNKNOWN_PATTERN`` silently
    absorbed any new value, producing a potentially-wrong diagnosis
    with zero type-checker signal.
    """
    from typing import assert_never  # noqa: PLC0415 — Python 3.11+ only, local import

    if regime == "attenuation":
        return Diagnosis.MIXER_ZEROED
    if regime == "saturation":
        return Diagnosis.MIXER_SATURATED
    if regime == "mixed":
        return Diagnosis.MIXER_SATURATED  # bias to the more actionable side
    if regime == "healthy":
        return Diagnosis.HEALTHY
    if regime == "unknown":
        return Diagnosis.MIXER_UNKNOWN_PATTERN
    assert_never(regime)


def _defer_platform_result() -> MixerSanityResult:
    """Build the canonical DEFERRED_PLATFORM result shape."""
    return MixerSanityResult(
        decision=MixerSanityDecision.DEFERRED_PLATFORM,
        diagnosis_before=Diagnosis.UNKNOWN,
        diagnosis_after=None,
        regime="unknown",
        matched_kb_profile=None,
        kb_match_score=0.0,
        user_customization_score=0.0,
        cards_probed=(),
        controls_modified=(),
        rollback_snapshot=None,
        probe_duration_ms=0,
        apply_duration_ms=None,
        validation_passed=None,
        validation_metrics=None,
    )


__all__ = [
    "_check_validation_gates",
    "_classify_regime_heuristically",
    "_defer_platform_result",
    "_diagnosis_for_regime",
]
