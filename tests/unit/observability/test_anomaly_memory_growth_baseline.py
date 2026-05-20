"""F-001 regression — MISSION-A.1.P1 anomaly memory_growth baseline filter.

Mission anchor:
``docs-internal/missions/MISSION-A1-runtime-truth-remediation-2026-05-20.md``
§T1.2.

Pre-fix HEAD ``observability/anomaly.py:331`` filtered ``ts <= window_start``
while the inline comment claimed "oldest in-window snapshot". The filter
selected samples *outside* (before) the time window, so the baseline came
from the deque's oldest entry (up to ``maxlen * snapshot_interval`` old)
rather than from ``_memory_window_s`` ago. Growth percent was therefore
computed against a stale baseline; the detector under-fired on real bursts
and over-fired on slow steady growth.

Post-fix the filter is ``ts >= window_start``, matching the inline
comment and the corresponding governor evaluator
(``_resource_cohort_governor.py``).

These tests are deliberately written BEFORE the filter fix so each
verifies the new contract; pre-fix execution intentionally fails.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sovyx.observability import anomaly as anomaly_module
from sovyx.observability.anomaly import AnomalyDetector


@pytest.fixture()
def detector() -> AnomalyDetector:
    """Detector configured with 60s window + 10% growth threshold."""
    tuning = MagicMock()
    tuning.anomaly_window_size = 50
    tuning.anomaly_min_samples = 3
    tuning.anomaly_latency_factor = 2.0
    tuning.anomaly_error_rate_window_s = 60
    tuning.anomaly_error_rate_factor = 3.0
    # Window of 60s + 10% threshold makes the baseline-source distinction
    # easy to assert in tests below.
    tuning.anomaly_memory_growth_window_s = 60
    tuning.anomaly_memory_growth_pct = 10.0
    tuning.anomaly_cooldown_s = 60
    tuning.http_error_rate_spike_enabled = False
    tuning.http_error_rate_spike_count = 5
    tuning.http_error_rate_spike_window_s = 30
    tuning.http_error_rate_spike_cooldown_s = 300
    tuning.http_error_rate_spike_path_cap = 512
    return AnomalyDetector(tuning)


def _inject_history(
    detector: AnomalyDetector,
    *,
    rss_at_tick: list[tuple[float, int]],
) -> None:
    """Append (timestamp, rss_bytes) pairs directly into _rss_history.

    Mutating the deque directly avoids triggering ``_observe_rss``'s own
    detection logic during fixture setup so each test's *final* call to
    ``_observe_rss`` is the only one that may emit.
    """
    detector._rss_history.extend(rss_at_tick)


def _emitted_events(mock_logger: MagicMock) -> list[tuple[str, dict[str, object]]]:
    """Return [(event_name, fields), ...] from a patched anomaly logger."""
    events: list[tuple[str, dict[str, object]]] = []
    for call in mock_logger.info.call_args_list + mock_logger.warning.call_args_list:
        # _emit calls logger.{info,warning}(event_name, **fields)
        args, kwargs = call
        if args and args[0] == "anomaly.memory_growth":
            events.append((args[0], dict(kwargs)))
    return events


class TestAnomalyMemoryGrowthBaseline:
    """F-001 — baseline filter selects in-window samples."""

    def test_baseline_age_s_matches_window(self, detector: AnomalyDetector) -> None:
        """``anomaly.baseline_age_s`` ≈ ``_memory_window_s``, not deque max age.

        Construct 10 minutes of history (10 samples at 60s intervals); the
        oldest sample is 540s old. With a 60s window the baseline must be
        the sample taken 60s ago, NOT the 540s-old one. Pre-fix filter
        ``ts <= window_start`` selected the 540s-old sample, producing
        ``baseline_age_s ≈ 540``; post-fix selects the 60s-old sample.
        """
        # Inject a flat history with one sample per minute at 1 GiB.
        history = [(float(t), 1_000_000_000) for t in range(0, 540, 60)]
        _inject_history(detector, rss_at_tick=history)
        with patch.object(anomaly_module, "logger") as mock_logger:
            # Now at t=540 observe a 50% spike (well above 10% threshold).
            detector._observe_rss(rss_bytes=1_500_000_000, now=540.0)
        events = _emitted_events(mock_logger)
        assert len(events) == 1, f"expected exactly one event; got {events}"
        _, fields = events[0]
        baseline_age = fields["anomaly.baseline_age_s"]
        # Allow ±5s tick jitter; the window is 60s so baseline_age must be
        # in the [60, 65] band, NOT ~540 (the deque's oldest sample).
        assert isinstance(baseline_age, (int, float))
        assert 60.0 <= baseline_age <= 65.0, (
            f"baseline_age_s={baseline_age} — expected ≈ window_s (60); "
            f"large value indicates the filter picked a sample older than "
            f"the window (F-001 inverted-filter regression)."
        )

    def test_does_not_fire_on_slow_lifetime_growth(
        self,
        detector: AnomalyDetector,
    ) -> None:
        """Slow steady growth must NOT trip a window-scoped detector.

        20 minutes of history with 1% growth per minute = ~22% total growth
        over the deque history, but only ~1% growth within the 60s window.
        Pre-fix filter picked the oldest sample (~22% growth) → FIRES (false
        positive). Post-fix picks the 60s-old sample (~1% growth) → does NOT
        fire (correct).
        """
        # 20 samples at 60s intervals; each tick +1% on RSS.
        base = 1_000_000_000
        history = [(float(60 * i), int(base * (1.0 + 0.01 * i))) for i in range(20)]
        _inject_history(detector, rss_at_tick=history)
        # Continue the trend: at t=1200 (next tick), +1% more = ~1.21x base.
        next_rss = int(base * 1.21)
        with patch.object(anomaly_module, "logger") as mock_logger:
            detector._observe_rss(rss_bytes=next_rss, now=1200.0)
        events = _emitted_events(mock_logger)
        assert events == [], (
            f"slow ~1%/window growth must not fire 10% threshold; "
            f"got {events} (F-001 inverted-filter regression makes the "
            f"detector measure lifetime growth, not windowed growth)."
        )

    def test_fires_on_60s_burst(self, detector: AnomalyDetector) -> None:
        """Sharp burst within the 60s window MUST fire.

        Flat history at 1 GiB; then a single +30% spike on the next tick.
        Both pre-fix and post-fix filters should fire here; this test
        anchors the positive case so a future regression cannot suppress
        the event entirely.
        """
        history = [(float(60 * i), 1_000_000_000) for i in range(10)]
        _inject_history(detector, rss_at_tick=history)
        with patch.object(anomaly_module, "logger") as mock_logger:
            detector._observe_rss(rss_bytes=1_300_000_000, now=600.0)
        events = _emitted_events(mock_logger)
        assert len(events) == 1, (
            f"+30% spike within 60s window must fire 10% threshold; got {events}."
        )
        _, fields = events[0]
        # baseline_age_s must reflect the in-window sample, not the deque-oldest.
        baseline_age = fields["anomaly.baseline_age_s"]
        assert 60.0 <= baseline_age <= 65.0, (
            f"baseline_age_s={baseline_age} — expected ≈ window_s (60)."
        )
        # Growth percent should be ≈ 30%.
        growth_pct = fields["anomaly.growth_pct"]
        assert isinstance(growth_pct, (int, float))
        assert 29.0 <= growth_pct <= 31.0, f"growth_pct={growth_pct} — expected ≈ 30%."
