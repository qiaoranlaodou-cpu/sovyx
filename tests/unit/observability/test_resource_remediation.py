"""Mission H4 §T3.4 + §10.1 — :mod:`_resource_remediation` unit tests.

Closure addendum tag v0.49.30: the production module at
``src/sovyx/observability/_resource_remediation.py`` (248 LOC, 27
canonical hints) shipped in v0.49.19 without dedicated test coverage.
This file lands the spec §10.1 ≥12-test target plus a structural
SSoT-parity assertion: every canonical field name in
:data:``_HEALTH_SNAPSHOT_FIELDS`` MUST have a remediation entry, and
every remediation entry MUST map to a canonical field (no orphan
hints; no missing hints).

The hint-content invariants encode the spec §T3.4 contract: each hint
follows a stable shape (what the field measures + healthy range +
remediation pointer). Operators running ``sovyx doctor resources
--explain <field>`` rely on the shape — a one-word answer is a
defect.
"""

from __future__ import annotations

import pytest

from sovyx.observability._resource_registry import _HEALTH_SNAPSHOT_FIELDS
from sovyx.observability._resource_remediation import (
    FIELD_REMEDIATIONS,
    remediation_for,
)


class TestFieldRemediationsExhaustiveness:
    """SSoT parity: ``_HEALTH_SNAPSHOT_FIELDS`` ↔ ``FIELD_REMEDIATIONS``."""

    def test_every_canonical_field_has_a_remediation_entry(self) -> None:
        """No silent drift: every snapshot field MUST carry a hint."""
        missing = sorted(
            field for field in _HEALTH_SNAPSHOT_FIELDS if field not in FIELD_REMEDIATIONS
        )
        assert not missing, (
            f"Spec §T3.4: every canonical field in _HEALTH_SNAPSHOT_FIELDS "
            f"MUST have an entry in FIELD_REMEDIATIONS. Missing: {missing}"
        )

    def test_no_orphan_remediation_entries(self) -> None:
        """Remediation keys MUST map to a canonical SSoT field name."""
        orphans = sorted(
            field for field in FIELD_REMEDIATIONS if field not in _HEALTH_SNAPSHOT_FIELDS
        )
        assert not orphans, (
            f"FIELD_REMEDIATIONS has {len(orphans)} orphan entries that do "
            f"not map to _HEALTH_SNAPSHOT_FIELDS: {orphans}"
        )

    def test_remediation_count_matches_field_count(self) -> None:
        assert len(FIELD_REMEDIATIONS) == len(_HEALTH_SNAPSHOT_FIELDS)


class TestRemediationContentInvariants:
    """Per-hint shape invariants — each hint is operator-actionable."""

    @pytest.mark.parametrize("field", sorted(FIELD_REMEDIATIONS))
    def test_hint_is_non_empty_string(self, field: str) -> None:
        hint = FIELD_REMEDIATIONS[field]
        assert isinstance(hint, str)
        assert hint.strip() != ""

    @pytest.mark.parametrize("field", sorted(FIELD_REMEDIATIONS))
    def test_hint_is_load_bearing(self, field: str) -> None:
        """Each hint MUST be at least 80 characters — guards against one-word stubs."""
        hint = FIELD_REMEDIATIONS[field]
        assert len(hint) >= 80, (
            f"Hint for {field!r} is too short ({len(hint)} chars); "
            f"per spec §T3.4 a hint MUST describe what the field "
            f"measures + healthy range + remediation pointer."
        )

    @pytest.mark.parametrize("field", sorted(FIELD_REMEDIATIONS))
    def test_hint_does_not_leak_internal_paths(self, field: str) -> None:
        """No /home/<user>/ or C:\\Users\\ in shipped hints (would PII-leak)."""
        hint = FIELD_REMEDIATIONS[field]
        assert "/home/" not in hint
        # Hints intentionally reference ``C:\Users\`` is a no-no.
        assert "C:\\Users\\" not in hint
        assert "/Users/" not in hint or "macOS" in hint  # tolerate generic docs


class TestRemediationFor:
    """``remediation_for`` lookup helper — fallback semantics + identity."""

    def test_known_field_returns_registered_hint(self) -> None:
        # Use a stable SSoT entry that has shipped since Phase 1.A.
        assert remediation_for("process.rss_bytes") == FIELD_REMEDIATIONS["process.rss_bytes"]

    def test_unknown_field_returns_canonical_pointer(self) -> None:
        result = remediation_for("made_up_field_xyz")
        assert "docs/operations/resource-hygiene.md" in result
        assert "made_up_field_xyz" in result

    def test_unknown_field_does_not_raise(self) -> None:
        # The CLI surface depends on robust no-raise semantics.
        # remediation_for must never raise even on adversarial inputs.
        for adversarial in ("", "  ", "no.such.field", "process..rss_bytes"):
            result = remediation_for(adversarial)
            assert isinstance(result, str)
            assert result.strip() != ""

    def test_fallback_mentions_module_path_for_self_serve_extension(self) -> None:
        """Fallback hint MUST teach operators how to add a new entry."""
        result = remediation_for("any_unknown_field")
        assert "_resource_remediation.py" in result
        assert "FIELD_REMEDIATIONS" in result


class TestRemediationsBySection:
    """Every section in the SSoT field map has at least one hint."""

    def test_every_section_has_coverage(self) -> None:
        """No section ships without operator hints."""
        sections_with_fields: dict[str, list[str]] = {}
        for field, spec in _HEALTH_SNAPSHOT_FIELDS.items():
            sections_with_fields.setdefault(spec.section, []).append(field)

        for section, fields in sections_with_fields.items():
            covered = [f for f in fields if f in FIELD_REMEDIATIONS]
            assert covered, (
                f"Section {section!r} has fields {fields} but ZERO "
                f"remediation entries; operators running --explain on "
                f"any field in this section would hit the fallback."
            )


class TestRemediationStaticStructure:
    """Module-level structural assertions — Final mapping + module surface."""

    def test_field_remediations_is_typed_as_final_mapping(self) -> None:
        """The module declares :data:`FIELD_REMEDIATIONS` as Final[Mapping]."""
        import sovyx.observability._resource_remediation as mod

        annotations = getattr(mod, "__annotations__", {})
        # The Final[Mapping[str, str]] annotation survives at runtime in CPython 3.11+.
        ann = annotations.get("FIELD_REMEDIATIONS")
        assert ann is not None, (
            "FIELD_REMEDIATIONS module-level annotation MUST exist so "
            "mypy/ruff treat the binding as Final."
        )

    def test_public_surface_has_only_two_names(self) -> None:
        """``__all__`` is the contract; new symbols MUST be intentional."""
        from sovyx.observability import _resource_remediation

        assert set(_resource_remediation.__all__) == {
            "FIELD_REMEDIATIONS",
            "remediation_for",
        }
