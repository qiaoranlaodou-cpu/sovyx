"""F3 regression — Mission H4 §T2.2 anomaly memory-growth spike consumer fix.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§3 F3 + §10.4.

Pre-mission HEAD: ``observability/anomaly.py:224`` read
``event_dict.get("system.rss_bytes")`` whereas
``observability/resources.py:149`` emitted ``process.rss_bytes``. The
``anomaly.memory_growth_spike`` detector was silently dead since
landing — the v0.43.1 forensic session +1.1 GB RSS Δ over 60 s never
fired the structured event.

Post-mission (v0.49.15): the consumer reads ``process.rss_bytes`` first
with a dual-read fallback to ``system.rss_bytes`` during the LENIENT
calibration window (drops at v0.54.0 STRICT). This test verifies the
detector observes the canonical key + maintains compatibility with the
legacy alias during dual-emit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovyx.observability.anomaly import AnomalyDetector


@pytest.fixture()
def detector() -> AnomalyDetector:
    tuning = MagicMock()
    tuning.anomaly_window_size = 50
    tuning.anomaly_min_samples = 3
    tuning.anomaly_latency_factor = 2.0
    tuning.anomaly_error_rate_window_s = 60
    tuning.anomaly_error_rate_factor = 3.0
    tuning.anomaly_memory_growth_window_s = 300
    tuning.anomaly_memory_growth_pct = 10.0
    tuning.anomaly_cooldown_s = 60
    tuning.http_error_rate_spike_enabled = True
    tuning.http_error_rate_spike_count = 5
    tuning.http_error_rate_spike_window_s = 30
    tuning.http_error_rate_spike_cooldown_s = 300
    tuning.http_error_rate_spike_path_cap = 512
    return AnomalyDetector(tuning)


class TestH4AnomalyMemoryGrowthSpikeFires:
    """F3 regression — Mission H4 §T2.2."""

    def test_process_rss_bytes_key_is_observed(self, detector: AnomalyDetector) -> None:
        """The canonical key triggers _observe_rss; pre-mission this was dead."""
        payload = {
            "event": "self.health.snapshot",
            "process.rss_bytes": 1_770_000_000,
            "level": "info",
        }
        detector(None, "info", payload)
        # _rss_history accumulates the observation only if _observe_rss fired.
        assert len(detector._rss_history) == 1
        assert detector._rss_history[0][1] == 1_770_000_000

    def test_legacy_system_rss_bytes_fallback_works(self, detector: AnomalyDetector) -> None:
        """ADR-D9 dual-read: legacy system.rss_bytes key still triggers detection.

        The dual-read fallback is preserved through the LENIENT window so
        external dashboards / log forwarders keyed on the legacy name
        keep working. v0.54.0 STRICT drops the fallback.
        """
        payload = {
            "event": "self.health.snapshot",
            "system.rss_bytes": 1_770_000_000,
            "level": "info",
        }
        detector(None, "info", payload)
        assert len(detector._rss_history) == 1
        assert detector._rss_history[0][1] == 1_770_000_000

    def test_canonical_key_preferred_over_legacy(self, detector: AnomalyDetector) -> None:
        """When both keys are present, ``process.rss_bytes`` is preferred.

        ResourceSnapshotter (Phase 1.B §T2.1) dual-emits BOTH keys.
        The anomaly consumer reads canonical first; legacy is fallback only.
        """
        payload = {
            "event": "self.health.snapshot",
            "process.rss_bytes": 200_000_000,
            "system.rss_bytes": 100_000_000,  # different value
            "level": "info",
        }
        detector(None, "info", payload)
        assert len(detector._rss_history) == 1
        # Observed value MUST be the canonical (200M), not legacy (100M).
        assert detector._rss_history[0][1] == 200_000_000

    def test_no_rss_key_does_nothing(self, detector: AnomalyDetector) -> None:
        """Payloads without either key should not record any observation."""
        payload = {
            "event": "self.health.snapshot",
            "level": "info",
        }
        detector(None, "info", payload)
        assert len(detector._rss_history) == 0
