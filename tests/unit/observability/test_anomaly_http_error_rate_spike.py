"""Unit tests for the Mission C2 §T2.5 path-keyed HTTP 5xx spike detector.

Closes M2 from the v0.43.1 forensic audit: the existing global
``anomaly.error_rate_spike`` keyed on ``level == error|critical``
never sees ``HttpTelemetryMiddleware``'s WARNING-level 5xx
emits. The new ``anomaly.http_error_rate_spike`` is path-keyed
and observes the same structlog ``net.http.response`` event the
middleware already publishes — closing the visibility gap that
allowed the 8-unique-500 storm on ``/api/voice/status`` to pass
silently in the operator session.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T2.5.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sovyx.engine.config import ObservabilityTuningConfig
from sovyx.observability.anomaly import AnomalyDetector


def _tuning(**overrides: Any) -> ObservabilityTuningConfig:
    """Tuning config with HTTP spike defaults overridable per test."""
    base: dict[str, Any] = {
        "anomaly_window_size": 100,
        "anomaly_min_samples": 10,
        "anomaly_latency_factor": 2.0,
        "anomaly_error_rate_window_s": 60,
        "anomaly_error_rate_factor": 3.0,
        "anomaly_memory_growth_window_s": 60,
        "anomaly_memory_growth_pct": 10.0,
        "anomaly_cooldown_s": 60,
        # New C2 §T2.5 fields.
        "http_error_rate_spike_enabled": True,
        "http_error_rate_spike_count": 5,
        "http_error_rate_spike_window_s": 30,
        "http_error_rate_spike_cooldown_s": 300,
        "http_error_rate_spike_path_cap": 64,
    }
    base.update(overrides)
    return ObservabilityTuningConfig(**base)


def _response_entry(
    path: str,
    status_code: int,
    *,
    method: str = "GET",
    latency_ms: int = 3,
) -> dict[str, Any]:
    """Shape that mirrors ``HttpTelemetryMiddleware``'s emit verbatim."""
    return {
        "event": "net.http.response",
        "level": "warning" if status_code >= 500 else "info",  # noqa: PLR2004
        "net.method": method,
        "net.path": path,
        "net.client": "127.0.0.1:51234",
        "net.status_code": status_code,
        "net.response_bytes": 22,
        "net.latency_ms": latency_ms,
    }


class TestHttpErrorRateSpikeBasics:
    """Threshold + window + cooldown behavior."""

    def test_below_threshold_does_not_emit(self) -> None:
        """4 of 5 required 5xx in window — silent."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=5))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(4):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []

    def test_at_threshold_emits_once(self) -> None:
        """Exactly 5 5xx in window — one emit."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=5))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(5):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert len(emits) == 1
            fields = emits[0].kwargs
            assert fields["anomaly.path"] == "/api/voice/status"
            assert fields["anomaly.count"] >= 5
            assert fields["anomaly.threshold"] == 5
            assert fields["anomaly.window_s"] == 30
            assert fields["anomaly.status_code_sample"] == 500

    def test_cooldown_suppresses_duplicate_emits(self) -> None:
        """During cooldown, subsequent 5xx storms on same path are silent.

        Cooldown default 300s ≫ window default 30s, so a sustained
        outage produces 1 event per 5 min per path — not per minute.
        """
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=3))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            # Burst 1: triggers
            for _ in range(3):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            # Burst 2: still inside cooldown
            for _ in range(10):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert len(emits) == 1

    def test_4xx_does_not_count(self) -> None:
        """Only ``>= 500`` codes are observed; client errors are silent."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=3))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for code in (400, 401, 403, 404, 422):
                detector(None, "warning", _response_entry("/api/voice/status", code))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []

    def test_2xx_does_not_count(self) -> None:
        """Healthy responses don't poison the bucket."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=3))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(50):
                detector(None, "info", _response_entry("/api/voice/status", 200))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []

    def test_503_504_500_504_500_triggers(self) -> None:
        """Mixed 5xx codes all count toward the threshold."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=5))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for code in (503, 504, 500, 504, 500):
                detector(None, "warning", _response_entry("/api/voice/status", code))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert len(emits) == 1
            # ``status_code_sample`` is the LAST status_code that
            # pushed over the threshold — surface as evidence.
            assert emits[0].kwargs["anomaly.status_code_sample"] == 500


class TestHttpErrorRateSpikePathIsolation:
    """Path-keying: storms on different endpoints surface independently."""

    def test_two_paths_each_get_own_emit(self) -> None:
        """Concurrent storms on two paths produce two separate emits."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=3))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(3):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            for _ in range(3):
                detector(None, "warning", _response_entry("/api/voice/models", 503))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert len(emits) == 2
            paths = {e.kwargs["anomaly.path"] for e in emits}
            assert paths == {"/api/voice/status", "/api/voice/models"}

    def test_storm_on_path_a_does_not_arm_path_b(self) -> None:
        """Threshold is per-path; storm on A does not affect B's count."""
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=5))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(10):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            # Single 500 on a different path — must NOT emit (only 1
            # in the path-B bucket).
            detector(None, "warning", _response_entry("/api/voice/models", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            paths = [e.kwargs["anomaly.path"] for e in emits]
            assert paths == ["/api/voice/status"]


class TestHttpErrorRateSpikeBoundedCardinality:
    """Anti-pattern #15: dict-of-deques never grows unbounded."""

    def test_path_cap_evicts_oldest_entry(self) -> None:
        """At ``path_cap + 1`` unique paths, oldest entry evicts.

        Storms on the FIRST path after eviction restart from
        count=1 — desirable: alerting on a path that was silent
        for a long time gets a fresh accounting window.
        """
        detector = AnomalyDetector(
            _tuning(http_error_rate_spike_count=2, http_error_rate_spike_path_cap=4)
        )
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            # Seed 4 paths with a single 5xx each.
            for i in range(4):
                detector(None, "warning", _response_entry(f"/api/p{i}", 500))
            # Add a 5th path — forces eviction of /api/p0.
            detector(None, "warning", _response_entry("/api/p4", 500))
            # Now /api/p0 should be evicted. A storm on /api/p0
            # starts from count=1, threshold=2 → 1 more emit
            # needed to trigger.
            detector(None, "warning", _response_entry("/api/p0", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            # Single 500 on each path: nothing crosses threshold=2 yet.
            assert emits == []
            # Second 500 on /api/p0 (now re-seeded) triggers.
            detector(None, "warning", _response_entry("/api/p0", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert len(emits) == 1
            assert emits[0].kwargs["anomaly.path"] == "/api/p0"


class TestHttpErrorRateSpikeDisabledKnob:
    """Default-off knob: when disabled, no observation work happens."""

    def test_disabled_produces_zero_emits(self) -> None:
        """``http_error_rate_spike_enabled=False`` short-circuits."""
        detector = AnomalyDetector(
            _tuning(http_error_rate_spike_enabled=False, http_error_rate_spike_count=2)
        )
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(50):
                detector(None, "warning", _response_entry("/api/voice/status", 500))
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []


class TestHttpErrorRateSpikeSelfRecursionGuard:
    """The detector's own emit MUST NOT be re-observed (anti-pattern recursion)."""

    def test_anomaly_http_error_rate_spike_event_self_skips(self) -> None:
        """A synthetic ``anomaly.http_error_rate_spike`` entry hits the guard.

        Documents the ``_DETECTOR_EVENTS`` membership of the new
        event name — without it the detector's own emit could
        re-enter ``__call__`` and recursively trigger itself.
        """
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=1))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            # Synthetic detector-event entry — must be skipped at the
            # top-of-__call__ guard.
            detector(
                None,
                "warning",
                {
                    "event": "anomaly.http_error_rate_spike",
                    "level": "warning",
                    "net.status_code": 500,
                    "net.path": "/api/voice/status",
                },
            )
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []


class TestHttpErrorRateSpikeMalformedEntries:
    """Detector MUST be silent on malformed entries (never raise)."""

    def test_missing_path_does_not_emit(self) -> None:
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=1))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            detector(
                None,
                "warning",
                {"event": "net.http.response", "net.status_code": 500},
            )
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []

    def test_status_code_not_int_does_not_emit(self) -> None:
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=1))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            detector(
                None,
                "warning",
                {
                    "event": "net.http.response",
                    "net.status_code": "500",  # str, not int
                    "net.path": "/api/voice/status",
                },
            )
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []

    def test_path_empty_string_does_not_emit(self) -> None:
        detector = AnomalyDetector(_tuning(http_error_rate_spike_count=1))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            detector(
                None,
                "warning",
                {
                    "event": "net.http.response",
                    "net.status_code": 500,
                    "net.path": "",
                },
            )
            emits = [
                c for c in mock_warn.call_args_list if c.args[0] == "anomaly.http_error_rate_spike"
            ]
            assert emits == []
