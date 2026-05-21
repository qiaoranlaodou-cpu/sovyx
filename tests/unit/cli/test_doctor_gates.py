"""F-020 regression — MISSION-A.2.P3 `sovyx doctor gates` CLI.

Mission anchor:
``docs-internal/missions/MISSION-A2-operator-trust-remediation-2026-05-20.md``
§T3.1..T3.4.

Pre-fix operators on v0.49.x had no surface to discover Quality Gate
STRICT/LENIENT state, the STRICT-flip target tag, or the validation
gate (V-* in OPERATOR-VALIDATION-BACKLOG-2026.md) that unblocks the
flip. Operators relied on memory or grep through CLAUDE.md.

Post-fix ``sovyx doctor gates`` prints the single-source-of-truth
registry. This test file mechanically anchors the registry shape so a
future commit adding a gate to ``scripts/verify_gates.sh`` MUST also
add a row to ``_QUALITY_GATES`` in the same commit.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from sovyx.cli.commands.doctor import _QUALITY_GATES, doctor_app

runner = CliRunner()


class TestDoctorGatesRegistry:
    """F-020 — registry shape + STRICT-flip discipline."""

    def test_all_15_gates_present(self) -> None:
        """Registry must enumerate gates 1–15 inclusive (current CLAUDE.md count)."""
        numbers = [g.number for g in _QUALITY_GATES]
        assert numbers == list(range(1, 16)), (
            f"_QUALITY_GATES MUST cover gates 1–15 inclusive; got {numbers}. "
            "A new gate added to scripts/verify_gates.sh MUST add a row to "
            "_QUALITY_GATES in the same commit (F-020 discipline)."
        )

    def test_every_gate_has_status_strict_or_lenient(self) -> None:
        for gate in _QUALITY_GATES:
            assert gate.status in {"STRICT", "LENIENT"}, (
                f"Gate {gate.number} ({gate.name}) status={gate.status!r} — "
                "MUST be 'STRICT' or 'LENIENT' (operator-facing surface "
                "depends on this enum)."
            )

    def test_lenient_gates_have_strict_target_and_validation_gate(self) -> None:
        """LENIENT gates MUST cite their STRICT-flip target + V-* gate."""
        for gate in _QUALITY_GATES:
            if gate.status == "LENIENT":
                assert gate.strict_target.startswith("v"), (
                    f"Gate {gate.number} LENIENT but strict_target="
                    f"{gate.strict_target!r} — MUST cite a version tag "
                    "(e.g. v0.54.0)."
                )
                assert gate.validation_gate.startswith("V-"), (
                    f"Gate {gate.number} LENIENT but validation_gate="
                    f"{gate.validation_gate!r} — MUST cite a V-* ID "
                    "(operator validation backlog anchor)."
                )

    def test_strict_gates_have_em_dash_placeholders(self) -> None:
        """STRICT gates have no pending target or validation gate."""
        for gate in _QUALITY_GATES:
            if gate.status == "STRICT":
                assert gate.strict_target == "—", (
                    f"Gate {gate.number} STRICT but strict_target="
                    f"{gate.strict_target!r} — STRICT gates have no pending target."
                )


class TestDoctorGatesCli:
    """F-020 — CLI rendering invariants."""

    def test_default_invocation_renders_table(self) -> None:
        result = runner.invoke(doctor_app, ["gates"])
        assert result.exit_code == 0, result.output
        # Header text appears (rich Table renders title).
        assert "Quality Gates" in result.output
        # Every gate's name appears in the rendered output.
        for gate in _QUALITY_GATES:
            assert str(gate.number) in result.output

    def test_json_invocation_emits_valid_json(self) -> None:
        result = runner.invoke(doctor_app, ["gates", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert len(payload) == len(_QUALITY_GATES)
        assert all(
            set(row.keys())
            == {
                "number",
                "name",
                "status",
                "strict_target",
                "validation_gate",
            }
            for row in payload
        )

    def test_json_row_matches_registry(self) -> None:
        """JSON output is a faithful render of the in-memory registry."""
        result = runner.invoke(doctor_app, ["gates", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        for json_row, registry_row in zip(payload, _QUALITY_GATES, strict=True):
            assert json_row["number"] == registry_row.number
            assert json_row["name"] == registry_row.name
            assert json_row["status"] == registry_row.status
            assert json_row["strict_target"] == registry_row.strict_target
            assert json_row["validation_gate"] == registry_row.validation_gate

    def test_lenient_footer_appears_when_any_lenient(self) -> None:
        """When ≥1 gate is LENIENT, the footer pointing to the validation backlog appears."""
        result = runner.invoke(doctor_app, ["gates"])
        assert result.exit_code == 0
        # Footer references the operator-validation backlog explicitly.
        assert "OPERATOR-VALIDATION-BACKLOG-2026.md" in result.output


class TestGateExpectations:
    """F-020 — operator-trust contract for specific gates."""

    def test_gate_15_h4_lenient_with_v054_target(self) -> None:
        """Gate 15 (H4 resource hygiene) is LENIENT through V-H4-13 / v0.54.0."""
        gate = next(g for g in _QUALITY_GATES if g.number == 15)
        assert gate.status == "LENIENT"
        assert gate.strict_target == "v0.54.0"
        assert gate.validation_gate == "V-H4-13"

    def test_gates_1_to_7_all_strict(self) -> None:
        """Baseline gates (ruff/mypy/bandit/pytest/tsc/vitest) are always STRICT."""
        for gate in _QUALITY_GATES[:7]:
            assert gate.status == "STRICT", (
                f"Gate {gate.number} ({gate.name}) should be STRICT but got {gate.status!r}"
            )
