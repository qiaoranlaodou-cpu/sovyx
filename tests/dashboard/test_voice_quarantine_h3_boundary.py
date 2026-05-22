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

Mission C.1 §C.1-a extends this file with:

* :class:`TestQuarantineEntryModelUnionNarrowing` — pydantic smart-mode
  coerces ``"apo_degraded"`` etc. to :class:`QuarantineReason` instances
  via the new ``QuarantineReason | str`` Union field type.
* :class:`TestQuarantineEntryModelLifecycleTags` — H3 lifecycle tags
  (``"probe_pinned"`` / ``"probe_store"`` / …) pass without WARN.
* :class:`TestQuarantineEntryModelDriftDetection` — unknown values emit
  a structured WARN in LENIENT (default) and raise
  :class:`pydantic.ValidationError` in STRICT
  (``SOVYX_TRANSPORT__QUARANTINE_REASON_STRICT=true``).
* :class:`TestQuarantineEntryModelJsonRoundTrip` — serialised JSON wire
  shape is unchanged (StrEnum value, not enum repr).

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T2.8 + §10.2.

Mission C.1 anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.1.
"""

from __future__ import annotations

import time

import pytest
import structlog.testing
from pydantic import ValidationError

from sovyx.dashboard.routes.voice_health import (
    _QUARANTINE_REASON_STRICT_ENV,
    QuarantineEntryModel,
)
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


# ── Mission C.1 §C.1-a — transport-binding tests ─────────────────────────


_BASE_ENTRY_DICT: dict[str, object] = {
    "endpoint_guid": "{c1-a}",
    "device_friendly_name": "Mic (C.1)",
    "device_interface_name": "",
    "host_api": "WASAPI",
    "added_at_monotonic": 0.0,
    "expires_at_monotonic": 60.0,
    "seconds_until_expiry": 60.0,
    "reason": "apo_degraded",
}


class TestQuarantineEntryModelUnionNarrowing:
    """Mission C.1 §C.1-a — ``QuarantineReason | str`` Union smart-mode.

    Pydantic v2 smart-mode picks the most specific Union arm. Every
    :class:`QuarantineReason` member's string value coerces to the enum
    instance on validation so typed consumers (OpenAPI codegen,
    pydantic-typed Python clients) see real enum members instead of
    raw strings.
    """

    @pytest.mark.parametrize("member", list(QuarantineReason))
    def test_reason_field_coerces_enum_value_to_member(self, member: QuarantineReason) -> None:
        payload = {**_BASE_ENTRY_DICT, "reason": member.value}
        model = QuarantineEntryModel.model_validate(payload)
        # Union arm narrows to QuarantineReason instance — StrEnum so the
        # `==` compares against the literal value AND `isinstance` is True.
        assert model.reason == member.value
        assert isinstance(model.reason, QuarantineReason)

    @pytest.mark.parametrize("member", list(QuarantineReason))
    def test_derived_reason_field_coerces(self, member: QuarantineReason) -> None:
        payload = {**_BASE_ENTRY_DICT, "derived_reason": member.value}
        model = QuarantineEntryModel.model_validate(payload)
        assert isinstance(model.derived_reason, QuarantineReason)
        assert model.derived_reason == member.value

    @pytest.mark.parametrize("member", list(QuarantineReason))
    def test_resolved_reason_field_coerces(self, member: QuarantineReason) -> None:
        payload = {**_BASE_ENTRY_DICT, "resolved_reason": member.value}
        model = QuarantineEntryModel.model_validate(payload)
        assert isinstance(model.resolved_reason, QuarantineReason)
        assert model.resolved_reason == member.value


class TestQuarantineEntryModelLifecycleTags:
    """Mission C.1 §C.1-a — H3 lifecycle tags pass the boundary cleanly.

    Lifecycle-tag literals (``"probe_pinned"`` / ``"probe_store"`` / …)
    are legitimate ``reason`` values during the H3 LENIENT window and
    MUST round-trip without WARN. Gate 14 STRICT v0.53.0 closes the
    producer side; the boundary continues to accept lifecycle tags via
    the ``QuarantineReason | str`` Union ``str`` arm.
    """

    @pytest.mark.parametrize(
        "lifecycle_tag",
        [
            "probe",
            "probe_pinned",
            "probe_store",
            "probe_cascade",
            "factory_integration",
            "kernel_invalidated_recheck",
            "watchdog_recheck",
        ],
    )
    def test_lifecycle_tag_passes_without_warn(
        self,
        lifecycle_tag: str,
    ) -> None:
        payload = {**_BASE_ENTRY_DICT, "reason": lifecycle_tag}
        with structlog.testing.capture_logs() as cap:
            model = QuarantineEntryModel.model_validate(payload)
        # ``watchdog_recheck`` doubles as a QuarantineReason member so
        # the BeforeValidator coerces it to the enum; the other lifecycle
        # tags stay on the ``str`` Union arm.
        if lifecycle_tag == QuarantineReason.WATCHDOG_RECHECK.value:
            assert isinstance(model.reason, QuarantineReason)
        else:
            assert isinstance(model.reason, str)
            assert not isinstance(model.reason, QuarantineReason)
        assert str(model.reason) == lifecycle_tag
        drift_warns = [e for e in cap if e.get("event") == "voice_quarantine_reason_unrecognized"]
        assert drift_warns == [], f"lifecycle tag must not emit WARN, got: {drift_warns}"


class TestQuarantineEntryModelDriftDetection:
    """Mission C.1 §C.1-a — drift surfaces as WARN (LENIENT) or error (STRICT)."""

    _DRIFT_VALUE = "totally_not_a_real_quarantine_reason"

    def test_unknown_value_warns_in_lenient_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_QUARANTINE_REASON_STRICT_ENV, raising=False)
        payload = {**_BASE_ENTRY_DICT, "reason": self._DRIFT_VALUE}
        with structlog.testing.capture_logs() as cap:
            model = QuarantineEntryModel.model_validate(payload)
        # LENIENT mode: validator returns the unknown value unchanged.
        assert model.reason == self._DRIFT_VALUE
        drift_warns = [e for e in cap if e.get("event") == "voice_quarantine_reason_unrecognized"]
        assert len(drift_warns) == 1
        entry = drift_warns[0]
        assert entry["reason"] == self._DRIFT_VALUE
        assert entry["strict"] is False
        assert entry["mission"] == "C.1"

    @pytest.mark.parametrize("strict_value", ["true", "TRUE", "1", "yes"])
    def test_unknown_value_raises_in_strict_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
        strict_value: str,
    ) -> None:
        monkeypatch.setenv(_QUARANTINE_REASON_STRICT_ENV, strict_value)
        payload = {**_BASE_ENTRY_DICT, "reason": self._DRIFT_VALUE}
        with pytest.raises(ValidationError) as exc_info:
            QuarantineEntryModel.model_validate(payload)
        # xdist-safe (anti-pattern #8) — match on substring not type.
        assert "QuarantineReason member" in str(exc_info.value)
        assert _QUARANTINE_REASON_STRICT_ENV in str(exc_info.value)

    def test_strict_mode_accepts_enum_and_lifecycle_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_QUARANTINE_REASON_STRICT_ENV, "true")
        # Enum-valid value:
        payload_enum = {**_BASE_ENTRY_DICT, "reason": QuarantineReason.CAPTURE_DEAD.value}
        model_enum = QuarantineEntryModel.model_validate(payload_enum)
        assert isinstance(model_enum.reason, QuarantineReason)
        # Lifecycle-tag value:
        payload_lifecycle = {**_BASE_ENTRY_DICT, "reason": "probe_pinned"}
        model_lifecycle = QuarantineEntryModel.model_validate(payload_lifecycle)
        assert model_lifecycle.reason == "probe_pinned"

    @pytest.mark.parametrize("strict_value", ["", "0", "false", "no", "off", "anything"])
    def test_knob_is_false_unless_explicit_truthy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        strict_value: str,
    ) -> None:
        if strict_value == "":
            monkeypatch.delenv(_QUARANTINE_REASON_STRICT_ENV, raising=False)
        else:
            monkeypatch.setenv(_QUARANTINE_REASON_STRICT_ENV, strict_value)
        payload = {**_BASE_ENTRY_DICT, "reason": self._DRIFT_VALUE}
        # MUST NOT raise — only WARN.
        model = QuarantineEntryModel.model_validate(payload)
        assert model.reason == self._DRIFT_VALUE


class TestQuarantineEntryModelJsonRoundTrip:
    """Mission C.1 §C.1-a — wire-shape JSON serialization is unchanged."""

    @pytest.mark.parametrize("member", list(QuarantineReason))
    def test_enum_value_serializes_to_string_value(self, member: QuarantineReason) -> None:
        payload = {
            **_BASE_ENTRY_DICT,
            "reason": member.value,
            "derived_reason": member.value,
            "resolved_reason": member.value,
        }
        model = QuarantineEntryModel.model_validate(payload)
        dumped = model.model_dump(mode="json")
        # JSON wire shape MUST be the raw enum value string, not the
        # ``QuarantineReason.X`` repr — the latter would break every
        # existing dashboard consumer that reads the field.
        assert dumped["reason"] == member.value
        assert dumped["derived_reason"] == member.value
        assert dumped["resolved_reason"] == member.value
        assert dumped["effective_reason"] == member.value

    def test_lifecycle_tag_serializes_unchanged(self) -> None:
        payload = {
            **_BASE_ENTRY_DICT,
            "reason": "probe_pinned",
            "derived_reason": "",
            "resolved_reason": "",
        }
        model = QuarantineEntryModel.model_validate(payload)
        dumped = model.model_dump(mode="json")
        assert dumped["reason"] == "probe_pinned"
        assert dumped["derived_reason"] == ""
        assert dumped["resolved_reason"] == ""
        assert dumped["effective_reason"] == "probe_pinned"

    def test_field_chain_fallback_with_enum_resolved(self) -> None:
        """``effective_reason`` returns enum value through the field-chain."""
        payload = {
            **_BASE_ENTRY_DICT,
            "reason": "probe_pinned",  # lifecycle tag fallback
            "derived_reason": "",
            "resolved_reason": QuarantineReason.CAPTURE_DEAD.value,
        }
        model = QuarantineEntryModel.model_validate(payload)
        assert model.effective_reason == "capture_dead"
