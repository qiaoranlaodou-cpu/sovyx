"""End-to-end tests for the Mission C3 §T2.7 deaf-warning throttle.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.7 + §9.1.

Pin the post-ladder-exhaustion throttle BEHAVIOR (not just the helper
resolver — that's covered separately in
``test_heartbeat_terminal_deaf_throttle.py``). These tests drive the
actual ``_emit_heartbeat`` code path with the deaf-warning branch
active, asserting:

1. **post-exhaustion-throttle** — when ``_coordinator_terminated AND
   _failover_ladder_exhausted`` are both True AND the last terminal
   emit is within the interval, ``voice_pipeline_deaf_warning`` MUST
   NOT fire.
2. **pre-exhaustion-unthrottled** — without ``_failover_ladder_exhausted``,
   the emit fires every deaf heartbeat (legacy behavior).
3. **interval-knob** — when ``failover_terminal_deaf_warn_min_interval_s``
   elapses, the emit fires again with the same throttle window.
4. **coordinator_terminal-tag** — every emission (throttled OR not)
   carries the ``coordinator_terminal=True/False`` tag for dashboard
   splittability.

Uses a thin ``_StubHeartbeatHost`` that composes :class:`HeartbeatMixin`
with the minimal state the deaf-warning branch needs, plus mocked
collaborators (drain_window_stats + the module logger).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.pipeline import _heartbeat_mixin as heartbeat_mod
from sovyx.voice.pipeline._heartbeat_mixin import HeartbeatMixin


def _make_host(
    *,
    coordinator_terminated: bool = False,
    failover_ladder_exhausted: bool = False,
    interval_s: float = 60.0,
):
    """Build a minimal HeartbeatMixin host with the state the deaf-
    warning branch reads/writes.
    """

    class _StubHost(HeartbeatMixin):
        pass

    host = _StubHost()
    # Config required by every emit.
    host._config = SimpleNamespace(
        mind_id="jonny",
        failover_terminal_deaf_warn_min_interval_s=interval_s,
    )
    host._state = SimpleNamespace(name="LISTENING")
    host._running = True
    host._voice_clarity_active = False
    # Per-window VAD aggregate — primed so the is_deaf branch hits.
    host._max_vad_prob_since_heartbeat = 0.001  # below _DEAF_VAD_MAX_THRESHOLD
    host._vad_frames_since_heartbeat = 200  # above _DEAF_MIN_FRAMES (150)
    host._last_heartbeat_monotonic = 0.0
    host._last_vad_probability_snapshot = 0.0
    host._last_vad_probability_snapshot_at = 0.0
    # SNR/freshness state — kept default so the SNR branch is no-op.
    host._snr_low_consecutive_heartbeats = 0
    host._snr_low_alert_active = False
    host._noise_floor_drift_consecutive_heartbeats = 0
    host._noise_floor_drift_alert_active = False
    # Deaf-warning state.
    host._deaf_warnings_consecutive = 0
    # Mission C3 §T2.7 throttle state.
    host._coordinator_terminated = coordinator_terminated
    host._failover_ladder_exhausted = failover_ladder_exhausted
    host._last_terminal_deaf_warn_monotonic = 0.0
    # Cross-mixin call-site stub.
    host._maybe_trigger_bypass_coordinator = MagicMock()
    return host


def _spy_warnings(monkeypatch) -> list[dict]:
    """Spy on logger.warning, capturing every emission's kwargs dict."""
    captured: list[dict] = []
    real = heartbeat_mod.logger

    def _spy(event: str, *args, **kwargs):
        captured.append({"event": event, **kwargs})
        return None

    monkeypatch.setattr(heartbeat_mod.logger, "warning", _spy)
    # Suppress info/error paths that aren't under test.
    monkeypatch.setattr(heartbeat_mod.logger, "info", lambda *a, **kw: None)
    # The SNR drain returns an empty result by default in our setup so
    # this is the only emit we expect.
    return captured


class TestThrottleBehavior:
    """Mission C3 §T2.7 — 4 documented behavior cases."""

    def test_pre_exhaustion_unthrottled(self, monkeypatch) -> None:
        """Deaf heartbeat WITHOUT ladder-exhausted flag → emits every
        time + carries ``coordinator_terminal=False`` tag.
        """
        captured = _spy_warnings(monkeypatch)
        host = _make_host(
            coordinator_terminated=False,
            failover_ladder_exhausted=False,
        )

        # Drive 3 deaf heartbeats with monotonically-advancing clock.
        for tick in (1.0, 2.0, 3.0):
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(tick)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        assert len(deaf_warns) == 3
        for warn in deaf_warns:
            assert warn["coordinator_terminal"] is False

    def test_post_exhaustion_throttle_within_window(self, monkeypatch) -> None:
        """Deaf heartbeat WITH BOTH flags set + emit_2 within interval
        → emit_1 fires; emit_2 suppressed (throttled).
        """
        captured = _spy_warnings(monkeypatch)
        host = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=True,
            interval_s=60.0,
        )

        # First deaf heartbeat at t=1.0 → emit + stamps
        # _last_terminal_deaf_warn_monotonic with time.monotonic().
        host._max_vad_prob_since_heartbeat = 0.001
        host._vad_frames_since_heartbeat = 200
        host._emit_heartbeat(1.0)

        # Second deaf heartbeat IMMEDIATELY after → within window →
        # suppressed (no emit).
        host._max_vad_prob_since_heartbeat = 0.001
        host._vad_frames_since_heartbeat = 200
        host._emit_heartbeat(2.0)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        # Only the first emit should have fired.
        assert len(deaf_warns) == 1
        assert deaf_warns[0]["coordinator_terminal"] is True

        # Coordinator-trigger MUST have been called on both heartbeats
        # (heartbeat path stays alive even when log is suppressed).
        assert host._maybe_trigger_bypass_coordinator.call_count == 2

    def test_interval_knob_elapses_re_emits(self, monkeypatch) -> None:
        """When the interval (default 60 s) elapses, the next deaf
        heartbeat MUST emit again.
        """
        captured = _spy_warnings(monkeypatch)
        host = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=True,
            interval_s=0.1,  # tight interval to test re-emit
        )

        with patch.object(heartbeat_mod.time, "monotonic") as mock_clock:
            # First emit at monotonic=1000.0.
            mock_clock.return_value = 1000.0
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(1.0)

            # Second emit at monotonic=1000.05 (within 0.1s window) →
            # suppressed.
            mock_clock.return_value = 1000.05
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(2.0)

            # Third emit at monotonic=1000.20 (window elapsed) →
            # re-emits.
            mock_clock.return_value = 1000.20
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(3.0)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        # First and third emits fire; second is suppressed.
        assert len(deaf_warns) == 2
        for warn in deaf_warns:
            assert warn["coordinator_terminal"] is True

    def test_coordinator_terminal_tag_on_every_emission(self, monkeypatch) -> None:
        """Every ``voice_pipeline_deaf_warning`` (throttled or not)
        carries the ``coordinator_terminal`` boolean tag.
        """
        captured = _spy_warnings(monkeypatch)

        # Case 1: pre-exhaustion → tag=False.
        host_a = _make_host(
            coordinator_terminated=False,
            failover_ladder_exhausted=False,
        )
        host_a._max_vad_prob_since_heartbeat = 0.001
        host_a._vad_frames_since_heartbeat = 200
        host_a._emit_heartbeat(1.0)

        # Case 2: post-exhaustion → tag=True.
        host_b = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=True,
            interval_s=60.0,
        )
        host_b._max_vad_prob_since_heartbeat = 0.001
        host_b._vad_frames_since_heartbeat = 200
        host_b._emit_heartbeat(1.0)

        # Case 3: only one flag set (incomplete latch) → tag=False.
        host_c = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=False,
        )
        host_c._max_vad_prob_since_heartbeat = 0.001
        host_c._vad_frames_since_heartbeat = 200
        host_c._emit_heartbeat(1.0)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        assert len(deaf_warns) == 3
        # Case-by-case verification of the tag value.
        assert deaf_warns[0]["coordinator_terminal"] is False
        assert deaf_warns[1]["coordinator_terminal"] is True
        assert deaf_warns[2]["coordinator_terminal"] is False


class TestThrottleKnobInteraction:
    """The ``failover_terminal_deaf_warn_min_interval_s`` knob drives
    the throttle window precisely.
    """

    def test_knob_default_60s_throttles(self, monkeypatch) -> None:
        """Default 60 s interval — second emit within 60 s suppressed."""
        captured = _spy_warnings(monkeypatch)
        # Use VoiceTuningConfig default explicitly.
        default_interval = VoiceTuningConfig().failover_terminal_deaf_warn_min_interval_s
        assert default_interval == 60.0
        host = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=True,
            interval_s=default_interval,
        )

        with patch.object(heartbeat_mod.time, "monotonic") as mock_clock:
            mock_clock.return_value = 1000.0
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(1.0)

            mock_clock.return_value = 1059.0  # 59 s later — within 60 s
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(2.0)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        assert len(deaf_warns) == 1

    def test_knob_override_via_config(self, monkeypatch) -> None:
        """Operator-overridden interval is honored."""
        captured = _spy_warnings(monkeypatch)
        host = _make_host(
            coordinator_terminated=True,
            failover_ladder_exhausted=True,
            interval_s=300.0,  # 5 min — operator's override
        )

        with patch.object(heartbeat_mod.time, "monotonic") as mock_clock:
            mock_clock.return_value = 1000.0
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(1.0)

            # 200 s later — STILL within the 300 s window.
            mock_clock.return_value = 1200.0
            host._max_vad_prob_since_heartbeat = 0.001
            host._vad_frames_since_heartbeat = 200
            host._emit_heartbeat(2.0)

        deaf_warns = [c for c in captured if c["event"] == "voice_pipeline_deaf_warning"]
        assert len(deaf_warns) == 1
