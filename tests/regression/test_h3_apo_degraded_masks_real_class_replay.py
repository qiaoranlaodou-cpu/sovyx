"""Mission H3 §T2.9 F3 forensic-replay regression.

Replays the operator session FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md
§H3: a ``VAD_FRONTEND_DEAD`` verdict reaching the
``CaptureIntegrityCoordinator._quarantine_endpoint`` site produced a
``QuarantineEntry`` whose legacy ``reason`` field was pinned to
``"apo_degraded"`` regardless of the verdict — dashboard and doctor
rendered "APO degraded" on a Linux box where no APO chain existed,
sending the operator down the wrong remediation path.

Post-H3 Phase 1.B v0.49.11: the SSoT resolver populates
:attr:`QuarantineEntry.resolved_reason` with the canonical verdict-
derived value (here ``"vad_frontend_dead"``) while the legacy
:attr:`QuarantineEntry.reason` stays ``"apo_degraded"`` for backward
compat during the triple-field LENIENT window. The
:attr:`QuarantineEntryModel.effective_reason` computed property is the
single read path that frontends + monitoring tooling consume; it
returns ``"vad_frontend_dead"`` for this case.

This regression MUST pass after Phase 1.B ships AND would have failed
on the pre-mission revision (the ``resolved_reason`` field did not
exist and ``derived_reason`` was the only canonical value).

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§3 F3 + §10.4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health._quarantine_reasons import (
    QuarantineReason,
    resolve_reason_from_verdict,
)
from sovyx.voice.health.contract import IntegrityVerdict

if TYPE_CHECKING:
    from collections.abc import Callable


def _build_quarantine(clock: Callable[[], float] | None = None) -> EndpointQuarantine:
    """Fresh EndpointQuarantine with a fast TTL for replay determinism."""
    return EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)


class TestL1011L1013Replay:
    """FORENSIC §H3 L1011/L1013 replay — VAD_FRONTEND_DEAD → resolved_reason."""

    def test_vad_frontend_dead_writes_resolved_reason_field(self) -> None:
        """The H3 canonical SSoT field captures the verdict-derived value."""
        quarantine = _build_quarantine()
        resolved = resolve_reason_from_verdict(IntegrityVerdict.VAD_FRONTEND_DEAD)
        entry = quarantine.add(
            endpoint_guid="{linux-usb-1532:0528-0-duplex}",
            device_friendly_name="Razer BlackShark V2 Pro",
            host_api="ALSA",
            # h3-allowlist: ADR-D10 legacy default — replay mirrors capture_integrity.py:1274
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=resolved.value,
            resolved_reason=resolved.value,
        )
        # Post-H3 — resolved_reason carries the canonical SSoT value.
        assert entry.resolved_reason == "vad_frontend_dead"
        # Mission C1 LENIENT alias preserved.
        assert entry.derived_reason == "vad_frontend_dead"
        # Legacy field intentionally pinned to literal default during
        # LENIENT — Phase 3 STRICT v0.53.0 drops the divergence.
        assert entry.reason == "apo_degraded"

    def test_apo_degraded_writes_resolved_reason_field(self) -> None:
        """Windows APO_DEGRADED path — the canonical case where legacy +
        resolved happen to agree."""
        quarantine = _build_quarantine()
        resolved = resolve_reason_from_verdict(IntegrityVerdict.APO_DEGRADED)
        entry = quarantine.add(
            endpoint_guid="{windows-wasapi-mic-001}",
            device_friendly_name="Logitech G Pro X",
            host_api="WASAPI",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=resolved.value,
            resolved_reason=resolved.value,
        )
        assert entry.resolved_reason == "apo_degraded"
        assert entry.derived_reason == "apo_degraded"
        assert entry.reason == "apo_degraded"

    def test_format_mismatch_writes_resolved_reason_field(self) -> None:
        """FORMAT_MISMATCH terminal verdict → resolved_reason field."""
        quarantine = _build_quarantine()
        resolved = resolve_reason_from_verdict(IntegrityVerdict.FORMAT_MISMATCH)
        entry = quarantine.add(
            endpoint_guid="{darwin-coreaudio-mic-002}",
            device_friendly_name="Apple Built-in",
            host_api="CoreAudio",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=resolved.value,
            resolved_reason=resolved.value,
        )
        assert entry.resolved_reason == "format_mismatch"
        assert entry.derived_reason == "format_mismatch"
        assert entry.reason == "apo_degraded"  # legacy default pinned

    def test_driver_silent_writes_resolved_reason_field(self) -> None:
        quarantine = _build_quarantine()
        resolved = resolve_reason_from_verdict(IntegrityVerdict.DRIVER_SILENT)
        entry = quarantine.add(
            endpoint_guid="{linux-pipewire-mic-003}",
            device_friendly_name="USB Mic",
            host_api="ALSA",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=resolved.value,
            resolved_reason=resolved.value,
        )
        assert entry.resolved_reason == "driver_silent"
        assert entry.derived_reason == "driver_silent"


class TestLifecycleReAddPreservesResolvedReason:
    """Mission H3 §T2.4 — watchdog lifecycle re-add inherits resolved_reason."""

    def test_watchdog_recheck_re_add_inherits_resolved_reason(self) -> None:
        """A re-add with reason=watchdog_recheck preserves the original
        verdict-derived classification on resolved_reason."""
        # Use injected monotonic clock so the re-add doesn't trigger the
        # rapid-requarantine warning (we want the deterministic path).
        ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])

        def clock() -> float:
            return next(ticks)

        quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)
        # Initial quarantine — VAD_FRONTEND_DEAD verdict.
        quarantine.add(
            endpoint_guid="{linux-usb-mic-001}",
            device_friendly_name="Razer",
            host_api="ALSA",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason=QuarantineReason.VAD_FRONTEND_DEAD.value,
            resolved_reason=QuarantineReason.VAD_FRONTEND_DEAD.value,
        )
        # Lifecycle re-add by the watchdog rechecker (omit derived/resolved
        # → inherit from prior).
        # h3-allowlist: lifecycle-tag (test replay)
        reentry = quarantine.add(
            endpoint_guid="{linux-usb-mic-001}",
            device_friendly_name="Razer",
            host_api="ALSA",
            reason="watchdog_recheck",
        )
        # The lifecycle tag goes on ``reason``; the verdict-derived
        # classification survives on both alias + resolved fields.
        assert reentry.reason == "watchdog_recheck"
        assert reentry.derived_reason == "vad_frontend_dead"
        assert reentry.resolved_reason == "vad_frontend_dead"


class TestResolverRejectsBenignVerdicts:
    """Mission H3 §4.3 ADR-D3 — fail-loudly on programming-error verdicts."""

    @pytest.mark.parametrize(
        "benign_verdict",
        [
            IntegrityVerdict.HEALTHY,
            IntegrityVerdict.VAD_MUTE,
            IntegrityVerdict.INCONCLUSIVE,
        ],
    )
    def test_benign_verdict_raises_value_error(self, benign_verdict: IntegrityVerdict) -> None:
        """HEALTHY / VAD_MUTE / INCONCLUSIVE MUST NOT reach the resolver.

        The resolver's ValueError is the canonical "programming error
        — coordinator's verdict-router must handle this earlier"
        signal per anti-pattern #46. Pre-mission these verdicts would
        have silently fallen through to ``"apo_degraded"`` default.
        """
        with pytest.raises(ValueError, match="must not reach _quarantine_endpoint"):
            resolve_reason_from_verdict(benign_verdict)


class TestFieldChainFallback:
    """Mission H3 ADR-D2 — consumer field-chain fallback semantics."""

    def test_resolved_reason_takes_precedence(self) -> None:
        """When all three are populated, resolved_reason wins."""
        ticks = iter([0.0, 0.1])

        def clock() -> float:
            return next(ticks)

        quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)
        entry = quarantine.add(
            endpoint_guid="x",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason="vad_frontend_dead",
            resolved_reason="capture_dead",
        )
        # Field-chain fallback: resolved > derived > reason.
        actual = entry.resolved_reason or entry.derived_reason or entry.reason
        assert actual == "capture_dead"

    def test_derived_falls_through_when_resolved_empty(self) -> None:
        ticks = iter([0.0])

        def clock() -> float:
            return next(ticks)

        quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)
        entry = quarantine.add(
            endpoint_guid="x",
            # h3-allowlist: ADR-D10 legacy default during LENIENT
            reason=QuarantineReason.APO_DEGRADED.value,
            derived_reason="format_mismatch",
            # resolved_reason omitted → defaults to "" via inheritance
        )
        actual = entry.resolved_reason or entry.derived_reason or entry.reason
        assert actual == "format_mismatch"

    def test_legacy_reason_falls_through_when_both_empty(self) -> None:
        ticks = iter([0.0])

        def clock() -> float:
            return next(ticks)

        quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)
        entry = quarantine.add(
            endpoint_guid="x",
            # h3-allowlist: lifecycle-tag — pre-mission entry
            reason="probe",
        )
        actual = entry.resolved_reason or entry.derived_reason or entry.reason
        assert actual == "probe"
