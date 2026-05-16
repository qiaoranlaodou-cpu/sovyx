"""Mission C1 §4.4 L2 + L4 + L5 — ladder depth coverage.

Tests for the three deeper ladder steps shipped on top of the
v0.44.0 LENIENT L1+L3 foundation:

* **L2 ``silero_reinstantiate``** — fresh ONNX session build via
  :meth:`VoicePipeline.reinstantiate_vad` using stored
  ``SileroVAD.model_path`` + ``config``.
* **L4 ``agc2_floor_lift``** — bounded gain delta lift via
  :meth:`AGC2.lift_speech_level` + :meth:`AudioCaptureTask.apply_agc2_floor_lift`,
  capped by ``vad_frontend_reset_max_gain_lift_db`` (default 6 dB)
  AND by ``AGC2Config.max_gain_db`` (§20.I).
* **L5 ``fallback_vad``** — last-resort swap to
  :class:`FallbackEnergyVAD` via :meth:`VoicePipeline.swap_vad`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._agc2 import AGC2, AGC2Config
from sovyx.voice._vad_fallback import (
    FallbackEnergyVAD,
    FallbackVADConfig,
)
from sovyx.voice.health._vad_frontend_recovery import (
    _LADDER_STEPS,
    VADFrontendRecovery,
)
from sovyx.voice.health.contract import (
    BypassContext,
    BypassVerdict,
    IntegrityResult,
    IntegrityVerdict,
)
from sovyx.voice.vad import VADConfig, VADState

# ── L2: VoicePipeline.reinstantiate_vad ────────────────────────────────


class TestL2SileroReinstantiate:
    """L2 ladder step — fresh ONNX session via ``reinstantiate_vad``."""

    def test_ladder_steps_includes_silero_reinstantiate(self) -> None:
        """L2 is in the canonical 5-step ordering."""
        assert "silero_reinstantiate" in _LADDER_STEPS
        # And precedes L3 normalizer_engage per §4.4 cheapest-to-most-invasive.
        l2_idx = _LADDER_STEPS.index("silero_reinstantiate")
        l3_idx = _LADDER_STEPS.index("normalizer_engage")
        assert l2_idx < l3_idx

    @pytest.mark.asyncio()
    async def test_ladder_dispatches_silero_reinstantiate_to_pipeline(
        self,
    ) -> None:
        """When the step name reaches ``_apply_step``, the dispatcher
        calls ``pipeline.reinstantiate_vad()`` exactly once.

        Validates the L2 wire-up without depending on a real ONNX
        session — the pipeline is a mock with the structural
        Protocol surface.
        """
        pipeline = MagicMock()
        pipeline.reinstantiate_vad = AsyncMock(return_value=None)
        pipeline.reset_vad = AsyncMock(return_value=None)
        capture_task = MagicMock()
        capture_task.engage_frame_normalizer = AsyncMock(return_value=None)
        ladder = VADFrontendRecovery(
            probe=MagicMock(),
            capture_task=capture_task,
            tuning=VoiceTuningConfig(),
        )
        await ladder._apply_step(  # noqa: SLF001 — exercising dispatcher
            step_name="silero_reinstantiate",
            pipeline=pipeline,
        )
        pipeline.reinstantiate_vad.assert_awaited_once()
        # Sanity — L2 dispatch does NOT call L1 reset or L3 engage.
        pipeline.reset_vad.assert_not_awaited()
        capture_task.engage_frame_normalizer.assert_not_awaited()


# ── L4: AGC2 bounded floor lift ────────────────────────────────────────


class TestL4AGC2FloorLift:
    """L4 ladder step — bounded AGC2 floor lift via §20.M T1.4.b knob."""

    def test_lift_speech_level_zero_or_negative_noop(self) -> None:
        agc2 = AGC2(AGC2Config())
        before = agc2.speech_level_dbfs
        assert agc2.lift_speech_level(0.0) == 0.0
        assert agc2.speech_level_dbfs == before
        assert agc2.lift_speech_level(-3.0) == 0.0
        assert agc2.speech_level_dbfs == before

    def test_lift_speech_level_drops_estimate_by_delta(self) -> None:
        """Positive delta drops ``speech_level_dbfs`` so the P-controller
        next demands MORE gain — the operational definition of "floor
        lift".
        """
        agc2 = AGC2(AGC2Config())
        before = agc2.speech_level_dbfs
        applied = agc2.lift_speech_level(3.0)
        assert applied == pytest.approx(3.0)
        assert agc2.speech_level_dbfs == pytest.approx(before - 3.0)

    def test_lift_speech_level_caps_at_max_gain_db(self) -> None:
        """§20.I — applied delta caps at ``max_gain_db``. Requesting
        more never produces a gain demand beyond the AGC2 hard cap.
        """
        config = AGC2Config(target_dbfs=-18.0, max_gain_db=10.0)
        agc2 = AGC2(config)
        # Initial speech_level == target; implied gain == 0.
        # Request a 20 dB lift (way past max_gain_db=10).
        applied = agc2.lift_speech_level(20.0)
        # Floor lifts by exactly max_gain_db so the controller's next
        # demand sits at the cap, not beyond.
        assert applied == pytest.approx(10.0)
        # Resulting speech_level_dbfs is at the cap-implied floor.
        # target - max_gain = -18 - 10 = -28.
        assert agc2.speech_level_dbfs == pytest.approx(-28.0)
        # Subsequent lifts return 0 — already at the cap.
        assert agc2.lift_speech_level(5.0) == 0.0

    def test_lift_speech_level_partial_after_prior_lift(self) -> None:
        """When the prior speech-level already advanced toward the cap
        via the P-controller, a subsequent lift admits only the
        remaining headroom."""
        config = AGC2Config(target_dbfs=-18.0, max_gain_db=10.0)
        agc2 = AGC2(config)
        agc2.lift_speech_level(4.0)
        # 4 dB consumed; 6 dB headroom remains.
        applied = agc2.lift_speech_level(10.0)
        assert applied == pytest.approx(6.0)
        assert agc2.speech_level_dbfs == pytest.approx(-28.0)

    @pytest.mark.asyncio()
    async def test_ladder_dispatches_agc2_floor_lift_with_knob_delta(
        self,
    ) -> None:
        """L4 dispatcher passes the ``vad_frontend_reset_max_gain_lift_db``
        knob value to ``capture_task.apply_agc2_floor_lift``."""
        capture_task = MagicMock()
        capture_task.apply_agc2_floor_lift = MagicMock(return_value=2.5)
        pipeline = MagicMock()
        ladder = VADFrontendRecovery(
            probe=MagicMock(),
            capture_task=capture_task,
            tuning=VoiceTuningConfig(vad_frontend_reset_max_gain_lift_db=3.0),
        )
        await ladder._apply_step(  # noqa: SLF001
            step_name="agc2_floor_lift",
            pipeline=pipeline,
        )
        capture_task.apply_agc2_floor_lift.assert_called_once_with(3.0)


# ── L5: FallbackEnergyVAD ─────────────────────────────────────────────


class TestFallbackEnergyVAD:
    """L5 fallback — energy-based VAD with hysteresis FSM."""

    def test_initial_state_is_silence(self) -> None:
        vad = FallbackEnergyVAD()
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False
        assert isinstance(vad.config, VADConfig)
        # Model path is the empty sentinel (no ONNX artefact).
        assert str(vad.model_path) == "."  # Path("") normalises to "."

    def test_silent_frame_stays_silence(self) -> None:
        vad = FallbackEnergyVAD()
        frame = np.zeros(512, dtype=np.int16)
        evt = vad.process_frame(frame)
        assert evt.is_speech is False
        assert evt.state == VADState.SILENCE
        # Probability for empty signal sits at the lower clamp.
        assert evt.probability == pytest.approx(0.0, abs=0.1)

    def test_loud_frames_trigger_speech_onset_after_hysteresis(self) -> None:
        cfg = FallbackVADConfig(
            window_size=512,
            speech_rms_threshold_dbfs=-45.0,
            onset_consecutive_frames=3,
        )
        vad = FallbackEnergyVAD(cfg)
        # Build an int16 frame at RMS ~ -20 dBFS (well above threshold).
        # Constant amplitude 3000 → RMS = 3000/32768 ~= 0.092 → -20.7 dBFS.
        loud_frame = np.full(512, 3000, dtype=np.int16)
        # First two frames trigger ONSET state via FSM advance.
        evt1 = vad.process_frame(loud_frame)
        assert evt1.state == VADState.SILENCE
        evt2 = vad.process_frame(loud_frame)
        assert evt2.state == VADState.SILENCE
        # Third supra-threshold frame trips into SPEECH_ONSET; fourth
        # confirms into SPEECH.
        evt3 = vad.process_frame(loud_frame)
        assert evt3.state == VADState.SPEECH_ONSET
        evt4 = vad.process_frame(loud_frame)
        assert evt4.state == VADState.SPEECH
        assert evt4.is_speech is True

    def test_silent_frames_after_speech_trigger_offset_after_hysteresis(
        self,
    ) -> None:
        cfg = FallbackVADConfig(
            window_size=512,
            speech_rms_threshold_dbfs=-45.0,
            onset_consecutive_frames=2,
            offset_consecutive_frames=3,
        )
        vad = FallbackEnergyVAD(cfg)
        loud = np.full(512, 3000, dtype=np.int16)
        silent = np.zeros(512, dtype=np.int16)
        # Cross into SPEECH.
        for _ in range(3):
            vad.process_frame(loud)
        assert vad.state == VADState.SPEECH
        # 3 silent frames → OFFSET (per offset_consecutive_frames=3).
        vad.process_frame(silent)
        assert vad.state == VADState.SPEECH  # still speech (1 silent)
        vad.process_frame(silent)
        assert vad.state == VADState.SPEECH  # still (2 silent)
        vad.process_frame(silent)
        assert vad.state == VADState.SPEECH_OFFSET
        # is_speaking still True in OFFSET (matches Silero semantics).
        assert vad.is_speaking is True
        # One more silent → SILENCE.
        vad.process_frame(silent)
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False

    def test_reset_clears_fsm_and_counters(self) -> None:
        vad = FallbackEnergyVAD()
        loud = np.full(512, 3000, dtype=np.int16)
        for _ in range(5):
            vad.process_frame(loud)
        assert vad.state != VADState.SILENCE
        vad.reset()
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False

    def test_probability_monotonic_in_rms(self) -> None:
        """Probability proxy increases with RMS amplitude (sanity for
        downstream consumers reading ``VADEvent.probability``)."""
        vad = FallbackEnergyVAD()
        quiet = np.full(512, 100, dtype=np.int16)
        medium = np.full(512, 1000, dtype=np.int16)
        loud = np.full(512, 10000, dtype=np.int16)
        p_quiet = vad.process_frame(quiet).probability
        p_medium = vad.process_frame(medium).probability
        p_loud = vad.process_frame(loud).probability
        assert p_quiet < p_medium < p_loud

    def test_float32_frame_processed_correctly(self) -> None:
        """Fallback accepts float32 frames (matches SileroVAD contract)."""
        vad = FallbackEnergyVAD()
        loud_f32 = np.full(512, 0.1, dtype=np.float32)
        evt = vad.process_frame(loud_f32)
        # float32 0.1 ~= -20 dBFS — supra-threshold by default.
        assert evt.probability > 0.5


class TestL5FallbackLadderDispatch:
    """L5 dispatcher swaps pipeline VAD to fallback."""

    @pytest.mark.asyncio()
    async def test_ladder_dispatches_fallback_swap(self) -> None:
        pipeline = MagicMock()
        pipeline.swap_vad = AsyncMock(return_value=None)
        capture_task = MagicMock()
        ladder = VADFrontendRecovery(
            probe=MagicMock(),
            capture_task=capture_task,
            tuning=VoiceTuningConfig(),
        )
        await ladder._apply_step(  # noqa: SLF001
            step_name="fallback_vad",
            pipeline=pipeline,
        )
        pipeline.swap_vad.assert_awaited_once()
        # The arg should be a FallbackEnergyVAD instance.
        call = pipeline.swap_vad.await_args
        assert call is not None
        passed_vad = call.args[0]
        assert isinstance(passed_vad, FallbackEnergyVAD)


# ── End-to-end ladder run with all 5 steps ──────────────────────────────


class TestLadderRunsAllFiveSteps:
    """Verify the ladder iterates L1..L5 in order when each step's
    post-probe stays VAD_FRONTEND_DEAD."""

    @pytest.mark.asyncio()
    async def test_ladder_exhausts_all_steps_on_persistent_dead(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        # Probe always returns VAD_FRONTEND_DEAD post-step.
        probe = MagicMock(spec=CaptureIntegrityProbe)

        async def _probe_warm(_task):  # type: ignore[no-untyped-def]  # noqa: ANN001
            return IntegrityResult(
                verdict=IntegrityVerdict.VAD_FRONTEND_DEAD,
                endpoint_guid="g",
                rms_db=-45.0,
                vad_max_prob=0.001,
                spectral_flatness=0.12,
                spectral_rolloff_hz=6500.0,
                duration_s=3.0,
                probed_at_utc=datetime.now(UTC),
                raw_frames=48_000,
            )

        probe.probe_warm = _probe_warm

        pipeline = MagicMock()
        pipeline.reset_vad = AsyncMock(return_value=None)
        pipeline.reinstantiate_vad = AsyncMock(return_value=None)
        pipeline.swap_vad = AsyncMock(return_value=None)

        capture_task = MagicMock()
        capture_task.engage_frame_normalizer = AsyncMock(return_value=None)
        capture_task.apply_agc2_floor_lift = MagicMock(return_value=2.0)

        context = BypassContext(
            endpoint_guid="g",
            endpoint_friendly_name="Fake",
            host_api_name="ALSA",
            platform_key="linux",
            capture_task=capture_task,
            probe_fn=AsyncMock(return_value=None),
            current_device_index=0,
            current_device_kind="input",
            pipeline_ref=pipeline,
        )
        before = IntegrityResult(
            verdict=IntegrityVerdict.VAD_FRONTEND_DEAD,
            endpoint_guid="g",
            rms_db=-45.0,
            vad_max_prob=0.001,
            spectral_flatness=0.12,
            spectral_rolloff_hz=6500.0,
            duration_s=3.0,
            probed_at_utc=datetime.now(UTC),
            raw_frames=48_000,
        )

        ladder = VADFrontendRecovery(
            probe=probe,
            capture_task=capture_task,
            tuning=VoiceTuningConfig(),
        )
        outcomes = await ladder.run(context, before)

        # All 5 steps attempted; every outcome is STILL_DEAD.
        assert len(outcomes) == 5
        assert all(
            o.verdict == BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD for o in outcomes
        )
        # Each pipeline mutation method called exactly once.
        pipeline.reset_vad.assert_awaited_once()  # L1
        pipeline.reinstantiate_vad.assert_awaited_once()  # L2
        capture_task.engage_frame_normalizer.assert_awaited_once()  # L3
        capture_task.apply_agc2_floor_lift.assert_called_once()  # L4
        pipeline.swap_vad.assert_awaited_once()  # L5
