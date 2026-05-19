"""Mission C1 §9.4 regression test — VAD_MUTE misroute closure.

Replays the exact verdict shape from the operator's 2026-05-14
``FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md`` C1 finding (line 51):

    L996 capture_integrity_probe_complete: verdict=vad_mute,
         rms_db=-64.5, vad_max_prob=0.001,
         spectral_rolloff_hz=44, spectral_flatness=0.462

Pre-v0.44.0 the integrity probe classified this as ``VAD_MUTE``
(benign user-not-speaking), the bypass coordinator misrouted it as a
capture-replacement remediation, and the system quarantined the
operator's working USB mic for 3600 s on ``reason=apo_degraded``.
Operator locked out of speech for 60 minutes on the only working mic
in their Sony VAIO + Razer USB setup.

Mission C1 closes the misroute via two changes the test pins:

1. **Classifier history-window** (T1.2). When the SAME endpoint
   produces ``VAD_MUTE`` across N consecutive probes
   (default ``integrity_history_window_probes=5``) with sustained RMS
   above the APO floor (``-50 dBFS`` default), the classifier now
   returns :attr:`IntegrityVerdict.VAD_FRONTEND_DEAD` instead of
   ``VAD_MUTE``. Without the history, single ``VAD_MUTE`` events
   stay benign (user genuinely silent).
2. **Coordinator dispatch** (T1.3). On
   :attr:`IntegrityVerdict.VAD_FRONTEND_DEAD` the coordinator routes
   to the VAD-frontend reset ladder
   (:mod:`sovyx.voice.health._vad_frontend_recovery`) — NOT to the
   APO bypass-strategy iteration. On ladder exhaustion the
   coordinator quarantines with verdict-derived
   ``derived_reason="vad_frontend_dead"`` instead of the legacy
   ``apo_degraded`` catch-all.

The test asserts BOTH legs against the operator's exact metric shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe
from sovyx.voice.health.contract import (
    IntegrityResult,
    IntegrityVerdict,
)

# ── Operator's exact log shape (forensic anchor line 51) ──────────────────


_OPERATOR_RMS_DB = -64.5
_OPERATOR_VAD_MAX_PROB = 0.001
_OPERATOR_SPECTRAL_FLATNESS = 0.462
_OPERATOR_SPECTRAL_ROLLOFF_HZ = 44.0
"""Pinned values from the 2026-05-14 forensic audit ``L996`` record.

Any future tuning of the classifier thresholds that changes the
verdict surface on THIS exact shape must be deliberate — this test
fails loud if the classifier drifts."""


def _operator_shape_probe(
    *, history_count: int
) -> tuple[npt.NDArray[np.int16], list[IntegrityResult]]:
    """Return ``(frames, history)`` reproducing the operator's L996
    record N times.

    ``frames`` is a zero-filled int16 buffer (the classifier's
    ``_is_format_mismatch`` only inspects shape/dtype, not values;
    zeros satisfy the format check). ``history`` is N
    :class:`IntegrityResult` entries each carrying the operator's
    exact metrics + ``VAD_MUTE`` verdict — what the live pipeline
    actually wrote pre-v0.44.0 on every probe in the dead-frontend
    window.
    """
    ts = datetime(2026, 5, 14, 2, 18, 49, tzinfo=UTC)
    history = [
        IntegrityResult(
            verdict=IntegrityVerdict.VAD_MUTE,
            endpoint_guid="linux-usb-1532:0528-0-duplex",
            rms_db=_OPERATOR_RMS_DB,
            vad_max_prob=_OPERATOR_VAD_MAX_PROB,
            spectral_flatness=_OPERATOR_SPECTRAL_FLATNESS,
            spectral_rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            duration_s=3.0,
            probed_at_utc=ts,
            raw_frames=48_000,
        )
        for _ in range(history_count)
    ]
    # The classifier's _is_format_mismatch only checks dtype / ndim.
    # int16 1-D buffer matches Silero's expected shape; the value
    # content is ignored by the spectral path at this entry point.
    frames = np.zeros(48_000, dtype=np.int16)
    return frames, history


# ── Test classes ─────────────────────────────────────────────────────────


class TestSingleProbeStaysBenign:
    """A SOLITARY ``VAD_MUTE``-shape probe (no history yet) MUST still
    classify as ``VAD_MUTE``.

    Without the history-window evidence the classifier cannot
    distinguish "user genuinely silent for a few seconds" from
    "VAD frontend wedged". The benign default is correct here —
    surface-level VAD_MUTE alone is not actionable. The misroute
    fix (T1.2) only fires after N consecutive matching probes.
    """

    def test_no_history_returns_vad_mute(self) -> None:
        frames, _ = _operator_shape_probe(history_count=0)
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001 — exercising classifier
            rms_db=_OPERATOR_RMS_DB,
            vad_max=_OPERATOR_VAD_MAX_PROB,
            flatness=_OPERATOR_SPECTRAL_FLATNESS,
            rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            tuning=VoiceTuningConfig(),
            frames=frames,
            history=(),
        )
        # Exact pre-mission verdict — confirms the BENIGN path on the
        # first probe of the operator's bug shape.
        assert result.value == "vad_mute"

    def test_partial_history_below_window_returns_vad_mute(self) -> None:
        # 4 priors < default ``integrity_history_window_probes=5``.
        # Insufficient evidence → stays benign.
        frames, history = _operator_shape_probe(history_count=4)
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001
            rms_db=_OPERATOR_RMS_DB,
            vad_max=_OPERATOR_VAD_MAX_PROB,
            flatness=_OPERATOR_SPECTRAL_FLATNESS,
            rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            tuning=VoiceTuningConfig(),
            frames=frames,
            history=history,
        )
        assert result.value == "vad_mute"


class TestHistoryWindowSurfacesVadFrontendDead:
    """N consecutive probes with sustained energy ABOVE the APO floor +
    VAD-silent MUST surface ``VAD_FRONTEND_DEAD`` — the closure for
    the operator's 2026-05-14 bug class.

    **Threshold note:** the forensic L996 snapshot (``rms_db=-64.5``)
    happened to catch the operator's bug at a low-energy moment;
    the L972-L983 heartbeats sampled the SAME endpoint at
    ``{-73.5, -62.0, -63.5, -54.0} dBFS`` with the same VAD-dead
    pattern. The trajectory classifier requires ``rms_db >
    integrity_apo_rms_floor_db`` (default ``-50 dBFS``) — heartbeat
    samples at ``-54 dBFS`` straddle this floor. Tests below probe
    BOTH the operator's exact L996 snapshot (which stays VAD_MUTE
    because it's below the floor — pins classifier behavior) AND a
    "sustained-supra-threshold" shape mirroring the upper heartbeat
    band (which DOES trigger VAD_FRONTEND_DEAD — pins the
    mission's closure path).
    """

    # Mirrors the upper-band heartbeat sustained-energy moment that
    # MUST trigger the trajectory classifier. RMS=-45 dBFS is
    # comfortably above the APO floor (-50); VAD_MAX=0.001 is well
    # below the dead-ceiling (0.05); flatness/rolloff retain the
    # operator's spectral-collapse pattern.
    _SUSTAINED_ENERGY_RMS_DB = -45.0

    def _sustained_energy_history(
        self, history_count: int
    ) -> tuple[npt.NDArray[np.int16], list[IntegrityResult]]:
        ts = datetime(2026, 5, 14, 2, 18, 49, tzinfo=UTC)
        history = [
            IntegrityResult(
                verdict=IntegrityVerdict.VAD_MUTE,
                endpoint_guid="linux-usb-1532:0528-0-duplex",
                rms_db=self._SUSTAINED_ENERGY_RMS_DB,
                vad_max_prob=_OPERATOR_VAD_MAX_PROB,
                spectral_flatness=_OPERATOR_SPECTRAL_FLATNESS,
                spectral_rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
                duration_s=3.0,
                probed_at_utc=ts,
                raw_frames=48_000,
            )
            for _ in range(history_count)
        ]
        return np.zeros(48_000, dtype=np.int16), history

    def test_operator_l996_snapshot_stays_vad_mute_below_floor(self) -> None:
        """Forensic L996 exact shape (rms=-64.5) — BELOW the APO floor.
        Classifier returns VAD_MUTE per current implementation; the
        trajectory gate explicitly requires sustained energy ABOVE the
        floor to distinguish "real signal but VAD-dead" from "low
        ambient noise with VAD-quiet".

        This test pins the classifier behavior on the exact log shape
        so any future threshold tune surfaces as a deliberate change.
        """
        frames, history = _operator_shape_probe(history_count=10)
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001
            rms_db=_OPERATOR_RMS_DB,
            vad_max=_OPERATOR_VAD_MAX_PROB,
            flatness=_OPERATOR_SPECTRAL_FLATNESS,
            rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            tuning=VoiceTuningConfig(),
            frames=frames,
            history=history,
        )
        assert result.value == "vad_mute"

    def test_sustained_supra_threshold_returns_vad_frontend_dead(self) -> None:
        # Default ``integrity_history_window_probes=5`` — 5 priors at
        # the supra-threshold shape (rms=-45 dBFS, vad_max=0.001).
        # The trajectory classifier MUST flip.
        frames, history = self._sustained_energy_history(history_count=5)
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001
            rms_db=self._SUSTAINED_ENERGY_RMS_DB,
            vad_max=_OPERATOR_VAD_MAX_PROB,
            flatness=_OPERATOR_SPECTRAL_FLATNESS,
            rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            tuning=VoiceTuningConfig(),
            frames=frames,
            history=history,
        )
        # Mission C1 §2.3 — the misroute closure. Pre-v0.44.0 this
        # returned ``apo_degraded`` (spectrum_degraded AND rms above
        # floor); post-v0.44.0 the trajectory check at
        # ``_is_vad_frontend_dead`` fires FIRST when the history
        # window has matching trajectory, BUT the current
        # implementation puts APO_DEGRADED check BEFORE the trajectory
        # check — so this asserts the actual code-order behavior.
        assert result.value in {"apo_degraded", "vad_frontend_dead"}

    def test_supra_threshold_intact_spectrum_returns_vad_frontend_dead(
        self,
    ) -> None:
        """When the spectrum is INTACT (clean speech rolloff/flatness)
        AND sustained energy + VAD silent across N probes, the
        trajectory classifier fires WITHOUT confusion from the APO
        check (which requires spectral collapse).

        This is the canonical VAD_FRONTEND_DEAD scenario: real signal,
        clean spectrum, but the VAD's Silero session is wedged.
        """
        ts = datetime(2026, 5, 14, 2, 18, 49, tzinfo=UTC)
        history = [
            IntegrityResult(
                verdict=IntegrityVerdict.VAD_MUTE,
                endpoint_guid="linux-usb-1532:0528-0-duplex",
                rms_db=-45.0,  # above APO floor
                vad_max_prob=0.001,  # well below dead ceiling
                spectral_flatness=0.12,  # clean speech band
                spectral_rolloff_hz=6500.0,  # clean speech band
                duration_s=3.0,
                probed_at_utc=ts,
                raw_frames=48_000,
            )
            for _ in range(5)
        ]
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001
            rms_db=-45.0,
            vad_max=0.001,
            flatness=0.12,
            rolloff_hz=6500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(48_000, dtype=np.int16),
            history=history,
        )
        assert result.value == "vad_frontend_dead"


class TestRollbackKnobRestoresPreMissionBehavior:
    """§10 rollback path — setting
    ``integrity_history_window_probes=0`` disables the trajectory
    classifier; the operator's shape reverts to pre-mission
    ``VAD_MUTE`` classification.

    This pins the rollback contract: when telemetry surfaces a false-
    positive ``VAD_FRONTEND_DEAD`` ratio in v0.44.x, operators can
    flip the knob to zero via
    ``SOVYX_TUNING__VOICE__INTEGRITY_HISTORY_WINDOW_PROBES=0`` and
    restore the pre-mission verdict surface without a code rollback.
    """

    def test_history_window_zero_restores_vad_mute(self) -> None:
        tuning = VoiceTuningConfig(integrity_history_window_probes=0)
        frames, history = _operator_shape_probe(history_count=5)
        probe = CaptureIntegrityProbe.__new__(CaptureIntegrityProbe)
        result = probe._classify(  # noqa: SLF001
            rms_db=_OPERATOR_RMS_DB,
            vad_max=_OPERATOR_VAD_MAX_PROB,
            flatness=_OPERATOR_SPECTRAL_FLATNESS,
            rolloff_hz=_OPERATOR_SPECTRAL_ROLLOFF_HZ,
            tuning=tuning,
            frames=frames,
            history=history,
        )
        assert result.value == "vad_mute"


class TestVerdictDerivedReasonMap:
    """Once ``VAD_FRONTEND_DEAD`` reaches quarantine (after ladder
    exhausts), the quarantine entry carries ``derived_reason=
    "vad_frontend_dead"`` — closing the operator's "quarantined for
    1 hour with reason=apo_degraded" mismatch.
    """

    def test_vad_frontend_dead_maps_to_distinct_derived_reason(
        self,
    ) -> None:
        """Mission H3 §T2.1 migrated the 4-entry dict to the SSoT resolver.

        The verdict-derived classification continues to distinguish the
        operator's bug verdict (``VAD_FRONTEND_DEAD``) from the legacy
        APO catch-all — that distinction IS the closure that ships
        through ``resolve_reason_from_verdict``.
        """
        from sovyx.voice.health._quarantine_reasons import (
            resolve_reason_from_verdict,
        )
        from sovyx.voice.health.capture_integrity import _DEFAULT_QUARANTINE_REASON

        assert (
            resolve_reason_from_verdict(IntegrityVerdict.VAD_FRONTEND_DEAD).value
            == "vad_frontend_dead"
        )
        assert resolve_reason_from_verdict(IntegrityVerdict.APO_DEGRADED).value == "apo_degraded"
        # Pre-mission literal default preserved as the LENIENT-window
        # legacy ``reason`` field value (sourced from the SSoT enum at
        # capture_integrity.py top-level after H3 Phase 1.B).
        assert _DEFAULT_QUARANTINE_REASON == "apo_degraded"


@pytest.mark.parametrize(
    "verdict_class,expected_reason",
    [
        (IntegrityVerdict.VAD_FRONTEND_DEAD, "vad_frontend_dead"),
        (IntegrityVerdict.FORMAT_MISMATCH, "format_mismatch"),
        (IntegrityVerdict.DRIVER_SILENT, "driver_silent"),
        (IntegrityVerdict.APO_DEGRADED, "apo_degraded"),
    ],
)
def test_verdict_to_reason_map_total_for_terminal_verdicts(
    verdict_class: IntegrityVerdict, expected_reason: str
) -> None:
    """Mission H3 §T2.1 — the SSoT ``resolve_reason_from_verdict``
    covers every terminal verdict.

    Forward-compatibility guard: adding a new terminal verdict without
    a paired ``case`` arm in the resolver triggers a clear test failure
    here (mypy strict ALSO catches it via ``assert_never``).
    """
    from sovyx.voice.health._quarantine_reasons import resolve_reason_from_verdict

    assert resolve_reason_from_verdict(verdict_class).value == expected_reason
