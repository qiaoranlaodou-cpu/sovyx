"""Mission H3 §T2.8 — :class:`QuarantineEntryModel` boundary round-trip.

Pairs with the producer-side write in
``src/sovyx/voice/health/_quarantine.py::EndpointQuarantine.add`` which
populates :attr:`QuarantineEntry.resolved_reason`. The pydantic model
at ``src/sovyx/dashboard/routes/voice_health.py::QuarantineEntryModel``
MUST accept the producer's runtime-bound output across every
:class:`QuarantineReason` member without raising.

Discipline matches CLAUDE.md anti-pattern #40 + Mission C2 Quality Gate
8 — typed boundaries drift silently when the producer evolves without a
paired round-trip test. The Mission C2 mechanism applies to
``routes/voice.py``; the H3 surface lives in ``routes/voice_health.py``
which is not covered by Gate 8 today. This test ships H3's equivalent
discipline manually until a future cohort widens Gate 8's scan-root.

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T2.8 + §10.2.
"""

from __future__ import annotations

import time

import pytest

from sovyx.dashboard.routes.voice_health import QuarantineEntryModel
from sovyx.voice.health._quarantine import EndpointQuarantine, QuarantineEntry
from sovyx.voice.health._quarantine_reasons import (
    QuarantineReason,
    resolve_reason_from_verdict,
)
from sovyx.voice.health.contract import IntegrityVerdict


class TestQuarantineEntryModelRoundTrip:
    """Every QuarantineReason value round-trips through the boundary."""

    @pytest.mark.parametrize("reason", list(QuarantineReason))
    def test_round_trip_every_member(self, reason: QuarantineReason) -> None:
        """``QuarantineEntryModel.from_domain`` accepts every reason value."""
        entry = QuarantineEntry(
            endpoint_guid="{test-endpoint}",
            device_friendly_name="Test Mic",
            device_interface_name="",
            host_api="WASAPI",
            added_at_monotonic=0.0,
            expires_at_monotonic=60.0,
            reason="apo_degraded",  # legacy LENIENT default
            physical_device_id="",
            derived_reason=reason.value,
            resolved_reason=reason.value,
        )
        model = QuarantineEntryModel.from_domain(entry, now_monotonic=0.0, quarantine_s=60.0)
        assert model.resolved_reason == reason.value
        assert model.derived_reason == reason.value
        assert model.reason == "apo_degraded"

    @pytest.mark.parametrize("reason", list(QuarantineReason))
    def test_effective_reason_returns_resolved_when_set(self, reason: QuarantineReason) -> None:
        """``effective_reason`` returns the canonical SSoT value."""
        entry = QuarantineEntry(
            endpoint_guid="{x}",
            device_friendly_name="",
            device_interface_name="",
            host_api="",
            added_at_monotonic=0.0,
            expires_at_monotonic=60.0,
            reason="legacy_default",
            resolved_reason=reason.value,
        )
        model = QuarantineEntryModel.from_domain(entry, now_monotonic=0.0, quarantine_s=60.0)
        assert model.effective_reason == reason.value


class TestQuarantineEntryModelFieldChainFallback:
    """The ``effective_reason`` computed property is the SSoT read path."""

    def test_effective_reason_falls_through_to_derived(self) -> None:
        entry = QuarantineEntry(
            endpoint_guid="{x}",
            device_friendly_name="",
            device_interface_name="",
            host_api="",
            added_at_monotonic=0.0,
            expires_at_monotonic=60.0,
            reason="apo_degraded",
            derived_reason="capture_dead",
            resolved_reason="",  # empty — fall through to derived
        )
        model = QuarantineEntryModel.from_domain(entry, now_monotonic=0.0, quarantine_s=60.0)
        assert model.effective_reason == "capture_dead"

    def test_effective_reason_falls_through_to_legacy(self) -> None:
        entry = QuarantineEntry(
            endpoint_guid="{x}",
            device_friendly_name="",
            device_interface_name="",
            host_api="",
            added_at_monotonic=0.0,
            expires_at_monotonic=60.0,
            reason="probe",  # lifecycle tag
            derived_reason="",
            resolved_reason="",
        )
        model = QuarantineEntryModel.from_domain(entry, now_monotonic=0.0, quarantine_s=60.0)
        assert model.effective_reason == "probe"


class TestExtraAllow:
    """``model_config = ConfigDict(extra="allow")`` per anti-pattern #40."""

    def test_extra_fields_accepted(self) -> None:
        """Forward-additive: producers can attach extra fields without
        breaking validation."""
        entry_dict = {
            "endpoint_guid": "{x}",
            "device_friendly_name": "Test",
            "device_interface_name": "",
            "host_api": "WASAPI",
            "added_at_monotonic": 0.0,
            "expires_at_monotonic": 60.0,
            "seconds_until_expiry": 60.0,
            "reason": "apo_degraded",
            "derived_reason": "vad_frontend_dead",
            "resolved_reason": "vad_frontend_dead",
            # Hypothetical future H3-sibling fields:
            "voice.platform": "linux",
            "voice.bypass_family": "alsa_capture_chain",
        }
        model = QuarantineEntryModel.model_validate(entry_dict)
        assert model.resolved_reason == "vad_frontend_dead"


class TestProducerRoundTripWithResolverHelper:
    """End-to-end producer→boundary round-trip using the SSoT resolver."""

    @pytest.mark.parametrize(
        "verdict",
        [
            IntegrityVerdict.APO_DEGRADED,
            IntegrityVerdict.VAD_FRONTEND_DEAD,
            IntegrityVerdict.FORMAT_MISMATCH,
            IntegrityVerdict.DRIVER_SILENT,
        ],
    )
    def test_resolver_to_model_round_trip(self, verdict: IntegrityVerdict) -> None:
        """``resolve_reason_from_verdict`` → ``EndpointQuarantine.add`` →
        ``QuarantineEntryModel.from_domain`` accepts every terminal verdict."""
        resolved = resolve_reason_from_verdict(verdict)
        quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=4)
        entry = quarantine.add(
            endpoint_guid=f"{{test-{verdict.value}}}",
            device_friendly_name=f"Mic ({verdict.value})",
            host_api="WASAPI",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=resolved.value,
            resolved_reason=resolved.value,
        )
        model = QuarantineEntryModel.from_domain(
            entry, now_monotonic=time.monotonic(), quarantine_s=60.0
        )
        assert model.effective_reason == resolved.value
