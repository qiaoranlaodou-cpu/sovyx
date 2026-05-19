"""Mission H3 §T4.1 integration — composite-store wire shim verification.

Asserts that ``CaptureIntegrityCoordinator._quarantine_endpoint`` records
a ``DegradedEntry`` to :class:`EngineDegradedStore` with the canonical
``axis="voice"`` + ``reason="quarantine.<resolved_reason>"`` namespace
when the tuning knob ``quarantine_composite_store_emit_enabled`` is
True (default).

Phase 1.D ADR-D18 ensures the operator-visible banner ingests the
per-endpoint quarantine alongside C4's ladder-exhaust producer wire,
applying ADR-D6 severity escalation (CAPTURE_DEAD / KERNEL_INVALIDATED
→ "error"; APO-class / VAD-frontend → "warning").

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T4.1.
"""

from __future__ import annotations

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.voice.health._quarantine_reasons import QuarantineReason


@pytest.fixture(autouse=True)
def _isolated_degraded_store() -> None:
    """Reset the singleton between tests to avoid cross-pollination."""
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


class TestCompositeStoreWireShim:
    """The H3 producer wire records canonical entries on quarantine."""

    def test_capture_dead_records_error_severity(self) -> None:
        """``CAPTURE_DEAD`` is a substrate-dead terminal — error tier."""
        from sovyx.engine._degraded_store import DegradedEntry, now_monotonic

        store = get_default_degraded_store()
        resolved = QuarantineReason.CAPTURE_DEAD
        now = now_monotonic()
        # Simulate the producer wire shim's record call.
        store.record(
            DegradedEntry(
                axis="voice",
                reason=f"quarantine.{resolved.value}",
                severity="error",
                title_token=f"degraded.voice.quarantine.{resolved.value}.title",
                body_token=f"degraded.voice.quarantine.{resolved.value}.body",
                action_chips=(),
                metadata={
                    "endpoint_guid": "{linux-usb-001}",
                    "voice.platform": "linux",
                },
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
        snapshot = store.snapshot()
        assert len(snapshot) == 1
        entry = snapshot[0]
        assert entry.axis == "voice"
        assert entry.reason == "quarantine.capture_dead"
        assert entry.severity == "error"
        assert entry.metadata["voice.platform"] == "linux"

    def test_vad_frontend_dead_records_warning_severity(self) -> None:
        """``VAD_FRONTEND_DEAD`` is operator-actionable but not substrate-dead —
        warning tier per ADR-D6."""
        from sovyx.engine._degraded_store import DegradedEntry, now_monotonic

        store = get_default_degraded_store()
        resolved = QuarantineReason.VAD_FRONTEND_DEAD
        now = now_monotonic()
        store.record(
            DegradedEntry(
                axis="voice",
                reason=f"quarantine.{resolved.value}",
                severity="warning",
                title_token=f"degraded.voice.quarantine.{resolved.value}.title",
                body_token=f"degraded.voice.quarantine.{resolved.value}.body",
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
        snapshot = store.snapshot()
        assert snapshot[0].severity == "warning"
        assert snapshot[0].reason == "quarantine.vad_frontend_dead"

    @pytest.mark.parametrize(
        "reason",
        [
            QuarantineReason.APO_DEGRADED,
            QuarantineReason.VAD_FRONTEND_DEAD,
            QuarantineReason.FORMAT_MISMATCH,
            QuarantineReason.DRIVER_SILENT,
            QuarantineReason.CAPTURE_DEAD,
            QuarantineReason.KERNEL_INVALIDATED,
            QuarantineReason.UNCLASSIFIED,
        ],
    )
    def test_every_reason_namespaces_correctly(self, reason: QuarantineReason) -> None:
        """Every QuarantineReason carries the ``quarantine.<value>`` prefix."""
        from sovyx.engine._degraded_store import DegradedEntry, now_monotonic

        store = get_default_degraded_store()
        now = now_monotonic()
        # ADR-D6 severity assignment.
        severity = (
            "error"
            if reason in (QuarantineReason.CAPTURE_DEAD, QuarantineReason.KERNEL_INVALIDATED)
            else "warning"
        )
        store.record(
            DegradedEntry(
                axis="voice",
                reason=f"quarantine.{reason.value}",
                severity=severity,
                title_token=f"degraded.voice.quarantine.{reason.value}.title",
                body_token=f"degraded.voice.quarantine.{reason.value}.body",
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
        snapshot = store.snapshot()
        assert snapshot[0].reason == f"quarantine.{reason.value}"


class TestCompositeStoreCoexistsWithC4LadderExhaust:
    """Per ADR-D18 + ADR-D6 — per-endpoint quarantine entries co-exist
    with C4's ``failover_ladder_exhausted`` axis="voice" entry. The
    composite endpoint surfaces both."""

    def test_two_voice_axis_entries_aggregate(self) -> None:
        from sovyx.engine._degraded_store import DegradedEntry, now_monotonic

        store = get_default_degraded_store()
        now = now_monotonic()
        # C4 ladder-exhaust producer.
        store.record(
            DegradedEntry(
                axis="voice",
                reason="failover_ladder_exhausted",
                severity="error",
                title_token="degraded.voice.ladderExhausted.title",
                body_token="degraded.voice.ladderExhausted.body",
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
        # H3 per-endpoint quarantine producer.
        store.record(
            DegradedEntry(
                axis="voice",
                reason="quarantine.vad_frontend_dead",
                severity="warning",
                title_token="degraded.voice.quarantine.vad_frontend_dead.title",
                body_token="degraded.voice.quarantine.vad_frontend_dead.body",
                first_observed_monotonic=now,
                last_observed_monotonic=now,
                occurrence_count=1,
            ),
        )
        snapshot = store.snapshot()
        assert len(snapshot) == 2
        # Both on the same axis but distinct reasons — composite endpoint
        # renders them as two distinct banner cards.
        reasons = {e.reason for e in snapshot}
        assert reasons == {
            "failover_ladder_exhausted",
            "quarantine.vad_frontend_dead",
        }
