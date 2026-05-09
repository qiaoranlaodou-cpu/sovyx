"""Tests for the noise-floor trend aggregator [Phase 4 T4.38].

Coverage:

* :func:`record_noise_floor_sample` + :func:`compute_drift`
  short-vs-long mean computation for representative trends.
* Empty buffer returns ``ready=False`` with zero placeholders.
* ``ready`` gate: long window must be fully populated AND short
  window has ≥25% of capacity.
* Sustained upward drift produces positive ``drift_db``;
  downward drift produces negative.
* :func:`compute_drift` does NOT clear the buffer (read-only —
  unlike :func:`._snr_heartbeat.drain_window_stats`).
* Bounded buffer drops oldest samples on overflow (FIFO).
* Per-mind isolation + LRU eviction at the ``_MAX_MINDS`` cap
  (Phase 5.A multi-mind keying — Finding 6 closure).
"""

from __future__ import annotations

import pytest

from sovyx.voice.health import _noise_floor_trending
from sovyx.voice.health._noise_floor_trending import (
    _LONG_WINDOW_SAMPLES,
    _MAX_MINDS,
    _SHORT_WINDOW_SAMPLES,
    compute_drift,
    record_noise_floor_sample,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestEmptyBuffer:
    def test_no_samples_not_ready(self) -> None:
        drift = compute_drift()
        assert drift.short_count == 0
        assert drift.long_count == 0
        assert drift.ready is False
        assert drift.drift_db == 0.0


class TestReadyGate:
    def test_short_window_only_not_ready(self) -> None:
        # Push samples up to a fraction of the short window —
        # baseline (long window) is still empty so ready=False.
        for _ in range(_SHORT_WINDOW_SAMPLES // 4):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.short_count > 0
        assert drift.ready is False

    def test_long_window_underfilled_not_ready(self) -> None:
        # Long window not yet populated: ready=False.
        for _ in range(_LONG_WINDOW_SAMPLES // 2):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.long_count == _LONG_WINDOW_SAMPLES // 2
        assert drift.ready is False

    def test_full_long_window_ready(self) -> None:
        # Fully populated long window → ready=True.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        assert drift.long_count == _LONG_WINDOW_SAMPLES
        assert drift.short_count == _SHORT_WINDOW_SAMPLES
        assert drift.ready is True


class TestDriftComputation:
    def test_steady_floor_zero_drift(self) -> None:
        # All samples at the same level → drift = 0.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-55.0)
        drift = compute_drift()
        assert drift.short_avg_db == pytest.approx(-55.0)
        assert drift.long_avg_db == pytest.approx(-55.0)
        assert drift.drift_db == pytest.approx(0.0)
        assert drift.ready is True

    def test_upward_drift_positive_delta(self) -> None:
        # First fill the long window with -55 dB.
        baseline_count = _LONG_WINDOW_SAMPLES - _SHORT_WINDOW_SAMPLES
        for _ in range(baseline_count):
            record_noise_floor_sample(noise_floor_db=-55.0)
        # Then top off with -40 dB samples filling the short window.
        for _ in range(_SHORT_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-40.0)
        drift = compute_drift()
        # Short window = last _SHORT_WINDOW_SAMPLES samples = all -40.
        assert drift.short_avg_db == pytest.approx(-40.0)
        # Long window = full buffer; mean ≈ weighted avg of the
        # two regions.
        weighted_avg = (
            baseline_count * -55.0 + _SHORT_WINDOW_SAMPLES * -40.0
        ) / _LONG_WINDOW_SAMPLES
        assert drift.long_avg_db == pytest.approx(weighted_avg)
        assert drift.drift_db > 0  # short > long means upward drift
        assert drift.drift_db == pytest.approx(drift.short_avg_db - drift.long_avg_db)
        assert drift.ready is True

    def test_downward_drift_negative_delta(self) -> None:
        # Inverse: baseline -40 dB then short window of -55 dB.
        baseline_count = _LONG_WINDOW_SAMPLES - _SHORT_WINDOW_SAMPLES
        for _ in range(baseline_count):
            record_noise_floor_sample(noise_floor_db=-40.0)
        for _ in range(_SHORT_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-55.0)
        drift = compute_drift()
        assert drift.drift_db < 0  # short < long means downward
        assert drift.short_avg_db == pytest.approx(-55.0)


class TestReadOnlyContract:
    def test_compute_drift_does_not_clear_buffer(self) -> None:
        # Unlike the SNR drain, compute_drift MUST NOT clear.
        # Two consecutive calls must return identical results.
        for k in range(_LONG_WINDOW_SAMPLES):
            # Fill with a slight ramp so any clear would be
            # detectable (different averages).
            record_noise_floor_sample(noise_floor_db=-50.0 + k * 0.001)
        first = compute_drift()
        second = compute_drift()
        assert first.short_avg_db == pytest.approx(second.short_avg_db)
        assert first.long_avg_db == pytest.approx(second.long_avg_db)
        assert first.drift_db == pytest.approx(second.drift_db)


class TestBufferOverflow:
    def test_overflow_drops_oldest_via_fifo(self) -> None:
        # Push 1.5x the long-window cap — the oldest 0.5x should
        # drop out, leaving only the newest _LONG_WINDOW_SAMPLES.
        # If the cap weren't enforced, drift computation would
        # see all 1.5x samples and produce wrong percentile.
        first_block_count = _LONG_WINDOW_SAMPLES // 2
        for _ in range(first_block_count):
            record_noise_floor_sample(noise_floor_db=-70.0)
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-50.0)
        drift = compute_drift()
        # The buffer should ONLY hold -50.0 samples (the -70.0
        # block scrolled out via FIFO drop).
        assert drift.long_avg_db == pytest.approx(-50.0)
        assert drift.short_avg_db == pytest.approx(-50.0)
        assert drift.drift_db == pytest.approx(0.0)


class TestPerMindIsolationAndLruEviction:
    """Phase 5.A multi-mind keying contract (Finding 6 closure).

    Pre-Phase-5.A this aggregator merged noise-floor samples from every
    mind into one rolling buffer; sustained drift in one mind would
    falsely inflate / mask drift in another. These tests pin per-mind
    isolation + the ``_MAX_MINDS`` LRU eviction cap.
    """

    def test_drift_isolated_per_mind(self) -> None:
        # Mind A sees a sustained upward drift; mind B stays steady.
        # Each mind's drift computation must reflect only its own
        # samples — no cross-mind contamination.
        baseline = _LONG_WINDOW_SAMPLES - _SHORT_WINDOW_SAMPLES
        for _ in range(baseline):
            record_noise_floor_sample(noise_floor_db=-55.0, mind_id="mind-a")
        for _ in range(_SHORT_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-40.0, mind_id="mind-a")
        # Mind B fills its long window steady at -55.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-55.0, mind_id="mind-b")

        a = compute_drift(mind_id="mind-a")
        b = compute_drift(mind_id="mind-b")

        assert a.ready is True
        assert a.short_avg_db == pytest.approx(-40.0)
        assert a.drift_db > 0  # upward drift in mind A
        assert b.ready is True
        assert b.short_avg_db == pytest.approx(-55.0)
        assert b.drift_db == pytest.approx(0.0)  # steady in mind B

    def test_compute_drift_does_not_clear_target_mind(self) -> None:
        # Read-only contract per mind — two consecutive computes
        # for the same mind return identical drift even with no
        # new samples.
        for _ in range(_LONG_WINDOW_SAMPLES):
            record_noise_floor_sample(noise_floor_db=-50.0, mind_id="mind-a")
        first = compute_drift(mind_id="mind-a")
        second = compute_drift(mind_id="mind-a")
        assert first.long_count == second.long_count
        assert first.long_avg_db == pytest.approx(second.long_avg_db)

    def test_default_mind_back_compat(self) -> None:
        # Un-keyed call shares state with explicit mind_id="default".
        record_noise_floor_sample(noise_floor_db=-60.0)
        record_noise_floor_sample(noise_floor_db=-60.0, mind_id="default")
        drift = compute_drift()  # also un-keyed
        assert drift.short_count == 2  # noqa: PLR2004
        assert drift.short_avg_db == pytest.approx(-60.0)

    def test_unknown_mind_returns_empty_without_creating_buffer(self) -> None:
        before = len(_noise_floor_trending._per_mind_buffers)
        drift = compute_drift(mind_id="never-recorded")
        after = len(_noise_floor_trending._per_mind_buffers)
        assert drift.long_count == 0
        assert drift.ready is False
        assert after == before

    def test_lru_eviction_at_max_minds_cap(self) -> None:
        for i in range(_MAX_MINDS):
            record_noise_floor_sample(noise_floor_db=-50.0, mind_id=f"mind-{i:02d}")
        assert len(_noise_floor_trending._per_mind_buffers) == _MAX_MINDS

        record_noise_floor_sample(noise_floor_db=-50.0, mind_id="mind-overflow")
        assert len(_noise_floor_trending._per_mind_buffers) == _MAX_MINDS
        assert "mind-00" not in _noise_floor_trending._per_mind_buffers
        assert "mind-overflow" in _noise_floor_trending._per_mind_buffers

    def test_lru_touch_protects_recently_used_mind(self) -> None:
        for i in range(_MAX_MINDS):
            record_noise_floor_sample(noise_floor_db=-50.0, mind_id=f"mind-{i:02d}")

        # Touch mind-00 — moves it to the most-recent position.
        record_noise_floor_sample(noise_floor_db=-45.0, mind_id="mind-00")

        # Trigger one eviction. mind-01 should drop, mind-00 survives.
        record_noise_floor_sample(noise_floor_db=-50.0, mind_id="mind-overflow")

        assert "mind-00" in _noise_floor_trending._per_mind_buffers
        assert "mind-01" not in _noise_floor_trending._per_mind_buffers
