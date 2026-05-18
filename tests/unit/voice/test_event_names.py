"""Unit tests for :mod:`sovyx.voice._event_names` (Mission H2 §T1.6).

Verifies the StrEnum membership shape, the LEGACY_TWIN_MAP completeness +
immutability, and the public re-export surface from
:mod:`sovyx.voice.__init__`.
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from sovyx.voice._event_names import (
    CAPTURE_INTEGRITY_EVENT_NAMES,
    LEGACY_EVENT_NAMES,
    LEGACY_TWIN_MAP,
    CaptureIntegrityEvent,
)


class TestCaptureIntegrityEventEnum:
    """The neutral event names + their StrEnum invariants."""

    def test_is_str_enum(self) -> None:
        """Anti-pattern #9: must inherit from StrEnum, not plain Enum.

        Guarantees value-based comparison + xdist namespace safety per
        the wider codebase convention.
        """
        assert issubclass(CaptureIntegrityEvent, StrEnum)

    def test_has_five_members(self) -> None:
        """The mission spec §0 mandates exactly five event names."""
        assert len(list(CaptureIntegrityEvent)) == 5

    def test_member_values(self) -> None:
        """Every member's value is the canonical neutral name string."""
        assert CaptureIntegrityEvent.BYPASS.value == "voice.capture_integrity.bypass"
        assert CaptureIntegrityEvent.BYPASSED.value == "voice.capture_integrity.bypassed"
        assert (
            CaptureIntegrityEvent.BYPASS_ACTIVATED.value
            == "voice.capture_integrity.bypass_activated"
        )
        assert (
            CaptureIntegrityEvent.BYPASS_INEFFECTIVE.value
            == "voice.capture_integrity.bypass_ineffective"
        )
        assert CaptureIntegrityEvent.BYPASS_FAILED.value == "voice.capture_integrity.bypass_failed"

    def test_str_coercion(self) -> None:
        """StrEnum members coerce to their value via ``str()``."""
        assert str(CaptureIntegrityEvent.BYPASSED) == "voice.capture_integrity.bypassed"

    def test_value_based_equality(self) -> None:
        """StrEnum values compare equal to bare strings."""
        assert CaptureIntegrityEvent.BYPASSED == "voice.capture_integrity.bypassed"

    def test_all_names_dotted_namespace(self) -> None:
        """Every neutral name uses the ``voice.capture_integrity.*`` prefix."""
        for member in CaptureIntegrityEvent:
            assert member.value.startswith("voice.capture_integrity."), (
                f"Member {member.name} value {member.value!r} is not in the neutral namespace"
            )


class TestLegacyTwinMap:
    """The LEGACY_TWIN_MAP must cover every member of the StrEnum."""

    def test_every_member_keyed(self) -> None:
        """No member is missing from the twin map."""
        for member in CaptureIntegrityEvent:
            assert member in LEGACY_TWIN_MAP, f"Missing legacy twin for {member.name}"

    def test_map_size_matches_enum(self) -> None:
        """Map length equals enum length."""
        assert len(LEGACY_TWIN_MAP) == len(list(CaptureIntegrityEvent))

    def test_legacy_twins(self) -> None:
        """The legacy event names match the forensic-audit anchors."""
        assert LEGACY_TWIN_MAP[CaptureIntegrityEvent.BYPASS] == "voice.apo.bypass"
        assert LEGACY_TWIN_MAP[CaptureIntegrityEvent.BYPASSED] == "audio.apo.bypassed"
        assert (
            LEGACY_TWIN_MAP[CaptureIntegrityEvent.BYPASS_ACTIVATED] == "voice_apo_bypass_activated"
        )
        assert (
            LEGACY_TWIN_MAP[CaptureIntegrityEvent.BYPASS_INEFFECTIVE]
            == "voice_apo_bypass_ineffective"
        )
        assert LEGACY_TWIN_MAP[CaptureIntegrityEvent.BYPASS_FAILED] == "voice_apo_bypass_failed"

    def test_no_neutral_name_collides_with_legacy(self) -> None:
        """Neutral and legacy names are disjoint sets."""
        neutral = {e.value for e in CaptureIntegrityEvent}
        legacy = set(LEGACY_TWIN_MAP.values())
        assert neutral.isdisjoint(legacy)

    def test_frozenset_caches_match_map_state(self) -> None:
        """Cached frozensets equal the dynamically-derived sets."""
        assert {e.value for e in CaptureIntegrityEvent} == CAPTURE_INTEGRITY_EVENT_NAMES
        assert set(LEGACY_TWIN_MAP.values()) == LEGACY_EVENT_NAMES


class TestPublicReExport:
    """The voice subpackage re-exports the H2 public surface."""

    def test_event_enum_reexported(self) -> None:
        from sovyx.voice import CaptureIntegrityEvent as ReExported

        assert ReExported is CaptureIntegrityEvent

    def test_legacy_twin_map_reexported(self) -> None:
        from sovyx import voice as voice_pkg

        assert voice_pkg.LEGACY_TWIN_MAP is LEGACY_TWIN_MAP

    def test_legacy_event_names_reexported(self) -> None:
        from sovyx import voice as voice_pkg

        assert voice_pkg.LEGACY_EVENT_NAMES is LEGACY_EVENT_NAMES


class TestForensicAnchorAlignment:
    """The legacy event names must match the v0.43.1 forensic audit §H2."""

    @pytest.mark.parametrize(
        "legacy",
        [
            "audio.apo.bypassed",
            "voice_apo_bypass_activated",
            "voice_apo_bypass_ineffective",
            "voice_apo_bypass_failed",
        ],
    )
    def test_legacy_names_appear_in_forensic_anchor(self, legacy: str) -> None:
        """Forensic anchors L1065/L1067 reference these four legacy names."""
        assert legacy in LEGACY_EVENT_NAMES
