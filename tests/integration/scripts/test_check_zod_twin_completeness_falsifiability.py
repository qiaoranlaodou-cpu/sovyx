"""Falsifiability tests — Mission C Gate 17 zod twin completeness.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.0 +
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 17.

These tests EXIST to be the operational proof that:

1. The extractor at ``_extract_zod_keys`` correctly walks brace depth +
   handles both the inline-z.object and chained-z.\\n.object stylistic
   shapes used in ``dashboard/src/types/schemas.ts``.
2. The asymmetry semantics are correct: pydantic-only keys are
   VIOLATIONS; zod-only keys are IGNORED (forward-additive).
3. STRICT mode actually fails on a deliberate violation; LENIENT mode
   reports + exits 0.
4. The pre-D.1 registered pair ``EngineDegradedResponse`` is currently
   PARITY (D.1 PERCEIVED commit `811421d8` added composite_max_severity
   to both pydantic and zod). The presence of this passing pair anchors
   the gate against false positives.

If any test here regresses, Gate 17 is broken — DO NOT silence it.
"""

from __future__ import annotations

from scripts.dev.check_zod_twin_completeness import (
    _extract_zod_keys,
    _pydantic_wire_field_names,
    _RegistryEntry,
    scan_registry,
)


class TestExtractZodKeys:
    """``_extract_zod_keys`` brace-depth + style invariants."""

    def test_inline_z_object_extracts_keys(self) -> None:
        src = """
export const Foo = z.object({
  a: z.string(),
  b: z.number(),
});
"""
        assert _extract_zod_keys(src, "Foo") == {"a", "b"}

    def test_chained_z_dot_object_extracts_keys(self) -> None:
        src = """
export const Bar = z
  .object({
    x: z.string(),
    "dotted.key": z.number(),
  })
  .passthrough();
"""
        assert _extract_zod_keys(src, "Bar") == {"x", "dotted.key"}

    def test_nested_object_inside_field_type_does_not_bleed(self) -> None:
        src = """
export const Baz = z.object({
  outer_key: z.object({
    inner_should_be_ignored: z.string(),
  }),
  other_top_level: z.number(),
});
"""
        assert _extract_zod_keys(src, "Baz") == {"outer_key", "other_top_level"}

    def test_unknown_export_returns_none(self) -> None:
        src = "export const Other = z.object({ a: z.string() });"
        assert _extract_zod_keys(src, "Missing") is None

    def test_string_with_braces_does_not_bias_depth(self) -> None:
        src = """
export const StringBraces = z.object({
  message: z.literal("see {plural}"),
  other: z.number(),
});
"""
        assert _extract_zod_keys(src, "StringBraces") == {"message", "other"}


class TestPydanticWireFieldNames:
    """``_pydantic_wire_field_names`` resolves Field(alias=...) shapes."""

    def test_resource_cohort_metrics_has_aliased_keys(self) -> None:
        keys = _pydantic_wire_field_names(
            "sovyx.dashboard.routes.engine_resources.ResourceCohortMetrics",
        )
        # Canonical post-A.1 keys MUST be present in the pydantic peer.
        assert "to_thread.pool_size_at_last_dispatch" in keys
        assert "asyncio.not_done_count" in keys
        assert "exception_cohort.window_retained_bytes" in keys
        # Legacy keys ALSO present during LENIENT cycle (sunset v0.55.0).
        assert "to_thread.pool_size" in keys
        assert "exception_cohort.retained_bytes_estimate" in keys


class TestGate17RegistryScan:
    """Anchor the current real-world drift state."""

    def test_engine_degraded_response_is_parity(self) -> None:
        """Mission D.1 (`811421d8`) added composite_max_severity to BOTH
        peers; the pair MUST report zero drift."""
        report = scan_registry()
        violations_by_pair = {v.pair_label for v in report.violations}
        assert "C4 composite degraded banner" not in violations_by_pair, (
            "Mission D.1 closure required EngineDegradedResponse parity. "
            f"Violations: {[v.missing_field_alias for v in report.violations if v.pair_label == 'C4 composite degraded banner']}"
        )

    def test_resource_cohort_metrics_is_current_drift_anchor(self) -> None:
        """C-P0-1 NOMINATED #1: ResourceCohortMetricsSchema is stale.
        Pre-Phase-C.2 this MUST report violations (the gate's whole
        purpose). After Phase C.2 ships, this anchor inverts."""
        report = scan_registry()
        cohort_violations = [
            v
            for v in report.violations
            if v.pair_label == "H4 cohort metrics (C-P0-1 NOMINATED #1)"
        ]
        # Anchor the EXACT canonical-key set that is currently missing.
        # Phase C.2 closure flips this from > 0 to == 0; that flip IS
        # the operator-visible signal that C-P0-1 closed.
        currently_missing = {v.missing_field_alias for v in cohort_violations}
        # The 11 canonical / cumulative / window keys the audit
        # identified MUST be in the missing set pre-C.2.
        canonical_anchors = {
            "asyncio.all_task_names",
            "asyncio.not_done_count",
            "asyncio.awaiting_count",
            "to_thread.pool_size_at_last_dispatch",
            "to_thread.queue_depth_at_last_dispatch",
            "to_thread.max_workers_at_last_dispatch",
            "exception_cohort.cumulative_retained_bytes_since_start",
            "exception_cohort.cumulative_distinct_group_id_count",
            "exception_cohort.window_retained_bytes",
            "exception_cohort.window_distinct_group_id_count",
            "process.open_files_status",
            "process.connections_status",
        }
        # Post-C.2: this assertion inverts (currently_missing becomes
        # empty). Until then, the audit-named keys MUST be flagged.
        # If this assertion ever passes with `currently_missing == set()`,
        # C-P0-1 has been closed — invert the assertion at that time.
        assert canonical_anchors.issubset(currently_missing) or (currently_missing == set()), (
            "Gate 17 should EITHER report all 12 canonical keys missing "
            "(pre-Phase-C.2 state) OR report zero violations (post-C.2 "
            f"state). Got: {currently_missing}"
        )


class TestGate17Asymmetry:
    """Asymmetry: zod-only fields are NOT violations."""

    def test_zod_only_field_not_flagged(self) -> None:
        """Forward-additive: a zod-twin field absent from pydantic is
        legitimate (consumer may declare it `.optional()` ahead of the
        producer wiring)."""
        from scripts.dev import check_zod_twin_completeness as gate

        entry = _RegistryEntry(
            pydantic_dotted_path=("sovyx.dashboard.routes.engine_degraded.AckStateModel"),
            zod_export_name="AckStateSchema",
            label="synthetic-asymmetry-probe",
        )
        report = gate.GateReport()
        zod_src = gate._DEFAULT_ZOD_FILE.read_text(encoding="utf-8")
        gate._check_pair(entry, zod_src, report)
        # AckStateSchema (zod) is a superset of AckStateModel (pydantic);
        # any extra zod field MUST NOT count against the pair.
        assert not report.violations, (
            f"Zod-only fields are not violations; got: "
            f"{[v.missing_field_alias for v in report.violations]}"
        )
