"""Tests for the recent-SNR rolling aggregator [Phase 4 T4.36].

Coverage:

* :func:`record_sample` + :func:`window_summary` percentile
  computation across representative distributions.
* Empty buffer returns count=0 with zero placeholder p50 (the
  orchestrator gates the confidence factor on count>0).
* :func:`window_summary` is read-only — two consecutive calls
  with no new samples return identical results.
* Bounded buffer drops oldest samples on overflow (FIFO).
* Per-mind isolation + LRU eviction at the ``_MAX_MINDS`` cap
  (Phase 5.A.2 multi-mind keying — Finding 6 closure).
"""

from __future__ import annotations

import pytest

from sovyx.voice.health import _recent_snr
from sovyx.voice.health._recent_snr import (
    _MAX_MINDS,
    _WINDOW_SAMPLES,
    record_sample,
    reset_for_tests,
    window_summary,
)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestEmpty:
    def test_no_samples_returns_zero_count(self) -> None:
        s = window_summary()
        assert s.count == 0
        assert s.p50_db == 0.0


class TestPercentiles:
    def test_single_sample_p50_equals_value(self) -> None:
        record_sample(snr_db=12.5)
        s = window_summary()
        assert s.count == 1
        assert s.p50_db == 12.5

    def test_uniform_distribution_p50_picks_middle(self) -> None:
        # 1, 2, …, 11. Median idx 11//2 = 5 → value 6.
        for k in range(1, 12):
            record_sample(snr_db=float(k))
        s = window_summary()
        assert s.count == 11  # noqa: PLR2004
        assert s.p50_db == 6.0  # noqa: PLR2004

    def test_unsorted_input_sorted_internally(self) -> None:
        for v in [50.0, 10.0, 80.0, 30.0, 60.0]:
            record_sample(snr_db=v)
        s = window_summary()
        # Sorted [10, 30, 50, 60, 80]; idx 2 → 50.
        assert s.p50_db == 50.0


class TestReadOnly:
    def test_two_consecutive_summaries_identical(self) -> None:
        # window_summary is READ-ONLY (unlike the heartbeat drain).
        for v in (5.0, 12.0, 18.0, 25.0):
            record_sample(snr_db=v)
        a = window_summary()
        b = window_summary()
        assert a.p50_db == b.p50_db
        assert a.count == b.count


class TestBufferOverflow:
    def test_overflow_drops_oldest_via_fifo(self) -> None:
        # Push 1.5x the window cap with monotonically increasing
        # values; the surviving samples are the most recent N.
        total = _WINDOW_SAMPLES + (_WINDOW_SAMPLES // 2)
        for k in range(total):
            record_sample(snr_db=float(k))
        s = window_summary()
        assert s.count == _WINDOW_SAMPLES
        # Survived: [total-N, …, total-1]. Median idx N//2 →
        # value (total - N) + N//2.
        expected_p50 = float(total - (_WINDOW_SAMPLES - _WINDOW_SAMPLES // 2))
        assert s.p50_db == expected_p50


class TestPerMindIsolationAndLruEviction:
    """Phase 5.A.2 multi-mind keying contract (Finding 6 closure).

    Pre-Phase-5.A.2 the recent-SNR rolling buffer was a single module-
    level deque so transcription queries on multi-mind hosts averaged
    samples across every mind. These tests pin per-mind isolation +
    the ``_MAX_MINDS`` LRU eviction cap.
    """

    def test_samples_isolated_per_mind(self) -> None:
        for v in (5.0, 10.0, 15.0):
            record_sample(snr_db=v, mind_id="mind-a")
        for v in (40.0, 50.0, 60.0):
            record_sample(snr_db=v, mind_id="mind-b")

        a = window_summary(mind_id="mind-a")
        b = window_summary(mind_id="mind-b")

        assert a.count == 3  # noqa: PLR2004
        assert a.p50_db == 10.0  # noqa: PLR2004 — sorted [5, 10, 15] → idx 1
        assert b.count == 3  # noqa: PLR2004
        assert b.p50_db == 50.0  # noqa: PLR2004 — sorted [40, 50, 60] → idx 1

    def test_summary_does_not_clear_target_mind(self) -> None:
        # Read-only contract per mind — two consecutive summaries
        # for the same mind are identical even with no new samples.
        record_sample(snr_db=22.0, mind_id="mind-a")
        first = window_summary(mind_id="mind-a")
        second = window_summary(mind_id="mind-a")
        assert first.count == 1
        assert second.count == 1
        assert first.p50_db == second.p50_db

    def test_default_mind_back_compat(self) -> None:
        # Un-keyed call shares state with explicit mind_id="default".
        record_sample(snr_db=7.0)
        record_sample(snr_db=11.0, mind_id="default")
        s = window_summary()  # also un-keyed
        assert s.count == 2  # noqa: PLR2004
        assert s.p50_db == 11.0  # noqa: PLR2004 — sorted [7, 11] → idx 1

    def test_unknown_mind_returns_empty_without_creating_buffer(self) -> None:
        before = len(_recent_snr._per_mind_buffers)
        s = window_summary(mind_id="never-recorded")
        after = len(_recent_snr._per_mind_buffers)
        assert s.count == 0
        assert after == before

    def test_lru_eviction_at_max_minds_cap(self) -> None:
        for i in range(_MAX_MINDS):
            record_sample(snr_db=float(i), mind_id=f"mind-{i:02d}")
        assert len(_recent_snr._per_mind_buffers) == _MAX_MINDS

        record_sample(snr_db=999.0, mind_id="mind-overflow")
        assert len(_recent_snr._per_mind_buffers) == _MAX_MINDS
        assert "mind-00" not in _recent_snr._per_mind_buffers
        assert "mind-overflow" in _recent_snr._per_mind_buffers

    def test_lru_touch_protects_recently_used_mind(self) -> None:
        for i in range(_MAX_MINDS):
            record_sample(snr_db=float(i), mind_id=f"mind-{i:02d}")

        # Touch mind-00 — moves it to the most-recent position.
        record_sample(snr_db=42.0, mind_id="mind-00")

        # Trigger one eviction. mind-01 should drop, mind-00 survives.
        record_sample(snr_db=999.0, mind_id="mind-overflow")

        assert "mind-00" in _recent_snr._per_mind_buffers
        assert "mind-01" not in _recent_snr._per_mind_buffers
