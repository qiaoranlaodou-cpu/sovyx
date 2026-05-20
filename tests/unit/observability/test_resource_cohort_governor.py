"""Unit tests for Mission H4 §T4.1 — ResourceCohortGovernor.

Verifies that each of the 5 cohort verdicts (RSS_GROWTH, THREAD_COUNT,
LOCK_DICT_CARDINALITY, ONNX_SESSION, EXCEPTION_COHORT) evaluates
correctly against synthetic snapshot payloads + emits the
``engine.resources.cohort_budget_exceeded`` WARN on breach + records
to the C4 EngineDegradedStore with ``axis="engine_resources"``.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T4.1 + §3 F4.
"""

from __future__ import annotations

from typing import Any

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.observability._resource_cohort_governor import (
    CohortBudget,
    CohortVerdict,
    ResourceCohortGovernor,
    emit_axis_entries,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import CohortAxis


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()
    yield
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()


def _baseline_snapshot() -> dict[str, Any]:
    """Healthy-state snapshot dict mirroring _HEALTH_SNAPSHOT_FIELDS."""
    return {
        "process.rss_bytes": 100_000_000,
        "process.num_threads": 20,
        "lock_dict.total_cardinality": 100,
        "onnx.session_count": 4,
        "exception_cohort.retained_bytes_estimate": 0,
    }


class TestRssGrowthCohort:
    """Δ-based cohort: RSS growth across rolling window."""

    def test_insufficient_data_on_first_tick(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot(_baseline_snapshot())
        rss_result = next(r for r in results if r.axis == CohortAxis.RSS_GROWTH)
        assert rss_result.verdict == CohortVerdict.INSUFFICIENT_DATA

    def test_healthy_on_flat_window(self) -> None:
        governor = ResourceCohortGovernor()
        governor.evaluate_snapshot({"process.rss_bytes": 100_000_000})
        results = governor.evaluate_snapshot({"process.rss_bytes": 100_000_000})
        rss_result = next(r for r in results if r.axis == CohortAxis.RSS_GROWTH)
        assert rss_result.verdict == CohortVerdict.HEALTHY

    def test_budget_exceeded_on_spike(self) -> None:
        """Forensic anchor §H4: +1.1 GB Δ MUST fire the cohort verdict."""
        governor = ResourceCohortGovernor()
        # Tick 1: baseline RSS.
        governor.evaluate_snapshot({"process.rss_bytes": 100_000_000})
        # Tick 2: 1.7 GB spike (> 512 MiB default budget).
        results = governor.evaluate_snapshot({"process.rss_bytes": 1_700_000_000})
        rss_result = next(r for r in results if r.axis == CohortAxis.RSS_GROWTH)
        assert rss_result.verdict == CohortVerdict.BUDGET_EXCEEDED
        assert rss_result.observed == 1_600_000_000

    def test_custom_budget_threshold_honoured(self) -> None:
        # Bump threshold so 100 MiB Δ doesn't trip.
        budgets = (
            CohortBudget(axis=CohortAxis.RSS_GROWTH, threshold=200 * 1024 * 1024, window_s=60),
        )
        governor = ResourceCohortGovernor(budgets=budgets)
        governor.evaluate_snapshot({"process.rss_bytes": 100_000_000})
        results = governor.evaluate_snapshot({"process.rss_bytes": 200_000_000})
        # 100 MiB Δ < 200 MiB threshold.
        rss_result = next(r for r in results if r.axis == CohortAxis.RSS_GROWTH)
        assert rss_result.verdict == CohortVerdict.HEALTHY


class TestThreadCountCohort:
    def test_budget_exceeded_on_thread_spike(self) -> None:
        """Forensic anchor §H4: 67→178 thread spike MUST fire."""
        governor = ResourceCohortGovernor()
        governor.evaluate_snapshot({"process.num_threads": 67})
        results = governor.evaluate_snapshot({"process.num_threads": 178})
        thread_result = next(r for r in results if r.axis == CohortAxis.THREAD_COUNT)
        assert thread_result.verdict == CohortVerdict.BUDGET_EXCEEDED
        assert thread_result.observed == 111

    def test_healthy_on_small_growth(self) -> None:
        governor = ResourceCohortGovernor()
        governor.evaluate_snapshot({"process.num_threads": 20})
        results = governor.evaluate_snapshot({"process.num_threads": 25})
        thread_result = next(r for r in results if r.axis == CohortAxis.THREAD_COUNT)
        assert thread_result.verdict == CohortVerdict.HEALTHY


class TestLockDictCohort:
    """Absolute-cap cohort: aggregate cardinality."""

    def test_healthy_below_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot({"lock_dict.total_cardinality": 5_000})
        ld_result = next(r for r in results if r.axis == CohortAxis.LOCK_DICT_CARDINALITY)
        assert ld_result.verdict == CohortVerdict.HEALTHY

    def test_budget_exceeded_above_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot({"lock_dict.total_cardinality": 7_500})
        ld_result = next(r for r in results if r.axis == CohortAxis.LOCK_DICT_CARDINALITY)
        assert ld_result.verdict == CohortVerdict.BUDGET_EXCEEDED


class TestOnnxCohort:
    def test_healthy_at_default_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot({"onnx.session_count": 5})
        onnx_result = next(r for r in results if r.axis == CohortAxis.ONNX_SESSION)
        assert onnx_result.verdict == CohortVerdict.HEALTHY

    def test_budget_exceeded_above_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot({"onnx.session_count": 12})
        onnx_result = next(r for r in results if r.axis == CohortAxis.ONNX_SESSION)
        assert onnx_result.verdict == CohortVerdict.BUDGET_EXCEEDED


class TestExceptionCohort:
    def test_healthy_below_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot(
            {"exception_cohort.retained_bytes_estimate": 1024 * 1024}  # 1 MiB
        )
        exc_result = next(r for r in results if r.axis == CohortAxis.EXCEPTION_COHORT)
        assert exc_result.verdict == CohortVerdict.HEALTHY

    def test_budget_exceeded_above_cap(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot(
            {"exception_cohort.retained_bytes_estimate": 20 * 1024 * 1024}  # 20 MiB
        )
        exc_result = next(r for r in results if r.axis == CohortAxis.EXCEPTION_COHORT)
        assert exc_result.verdict == CohortVerdict.BUDGET_EXCEEDED


class TestEmitAxisEntries:
    """Routing breach evaluations to the C4 composite store."""

    def test_breach_records_to_composite_store(self) -> None:
        governor = ResourceCohortGovernor()
        governor.evaluate_snapshot({"process.num_threads": 20})
        results = governor.evaluate_snapshot({"process.num_threads": 178})
        emitted = emit_axis_entries(results)
        assert emitted >= 1
        # The C4 store now has an axis="engine_resources" entry.
        snapshot = get_default_degraded_store().snapshot()
        engine_axis_entries = [e for e in snapshot if e.axis == "engine_resources"]
        assert engine_axis_entries
        # v0.49.24 — spec-literal reason name (was "engine_resources.thread_count").
        assert any(e.reason == "engine_resources.thread_count_spike" for e in engine_axis_entries)

    def test_healthy_does_not_record(self) -> None:
        governor = ResourceCohortGovernor()
        results = governor.evaluate_snapshot({"process.num_threads": 25})
        emitted = emit_axis_entries(results)
        # First tick is INSUFFICIENT_DATA — not BUDGET_EXCEEDED.
        assert emitted == 0
        snapshot = get_default_degraded_store().snapshot()
        engine_axis_entries = [e for e in snapshot if e.axis == "engine_resources"]
        assert engine_axis_entries == []

    def test_disabled_governor_returns_empty(self) -> None:
        governor = ResourceCohortGovernor(enabled=False)
        # Even with a clear spike, disabled governor returns empty.
        governor.evaluate_snapshot({"process.num_threads": 20})
        results = governor.evaluate_snapshot({"process.num_threads": 200})
        assert results == []


class TestGovernorSingleton:
    def test_default_returns_same_instance(self) -> None:
        from sovyx.observability._resource_cohort_governor import (
            get_default_resource_cohort_governor,
        )

        a = get_default_resource_cohort_governor()
        b = get_default_resource_cohort_governor()
        assert a is b

    def test_reset_yields_fresh_instance(self) -> None:
        from sovyx.observability._resource_cohort_governor import (
            get_default_resource_cohort_governor,
        )

        a = get_default_resource_cohort_governor()
        reset_default_resource_cohort_governor()
        b = get_default_resource_cohort_governor()
        assert a is not b


class TestSpecLiteralReasonNames:
    """Mission H4 §0 line 30 + v0.49.24 — spec-literal reason taxonomy.

    The 6 reason strings the spec lists at section §0 line 30 MUST
    match exactly what the governor emits — operators, alert rules,
    and i18n token keys all depend on this taxonomy.
    """

    def test_reason_for_axis_mapping_matches_spec_literal(self) -> None:
        from sovyx.observability._resource_cohort_governor import _REASON_FOR_AXIS

        # Spec §0 line 30 — 5 cohort-driven reasons. The full path is
        # ``engine_resources.<reason>`` so banner/dashboard see a fully
        # qualified namespace string.
        assert _REASON_FOR_AXIS[CohortAxis.RSS_GROWTH] == "engine_resources.rss_growth_spike"
        assert _REASON_FOR_AXIS[CohortAxis.THREAD_COUNT] == "engine_resources.thread_count_spike"
        assert (
            _REASON_FOR_AXIS[CohortAxis.LOCK_DICT_CARDINALITY]
            == "engine_resources.lock_dict_cardinality_saturated"
        )
        assert (
            _REASON_FOR_AXIS[CohortAxis.ONNX_SESSION]
            == "engine_resources.onnx_session_unexpected_count"
        )
        assert (
            _REASON_FOR_AXIS[CohortAxis.EXCEPTION_COHORT]
            == "engine_resources.exception_cohort_retention_high"
        )

    def test_heap_snapshot_triggered_reason_constant(self) -> None:
        from sovyx.observability._resource_cohort_governor import (
            _REASON_HEAP_SNAPSHOT_TRIGGERED,
        )

        # Spec §0 line 30 — 6th reason emitted by the heap-snapshot
        # capture success path (not a budget breach but a forensic-
        # artifact-persisted notification).
        assert _REASON_HEAP_SNAPSHOT_TRIGGERED == "engine_resources.heap_snapshot_triggered"

    def test_record_to_composite_store_uses_spec_literal_reason(self) -> None:
        """End-to-end: a BUDGET_EXCEEDED evaluation produces a
        DegradedEntry whose reason matches the spec literal.
        """
        from sovyx.observability._resource_cohort_governor import (
            CohortEvaluation,
            _record_to_composite_store,
        )

        for axis, expected_reason in [
            (CohortAxis.RSS_GROWTH, "engine_resources.rss_growth_spike"),
            (CohortAxis.THREAD_COUNT, "engine_resources.thread_count_spike"),
            (
                CohortAxis.LOCK_DICT_CARDINALITY,
                "engine_resources.lock_dict_cardinality_saturated",
            ),
            (CohortAxis.ONNX_SESSION, "engine_resources.onnx_session_unexpected_count"),
            (
                CohortAxis.EXCEPTION_COHORT,
                "engine_resources.exception_cohort_retention_high",
            ),
        ]:
            reset_default_degraded_store()
            evaluation = CohortEvaluation(
                axis=axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=999,
                budget=100,
                note="synthetic",
            )
            _record_to_composite_store(evaluation)
            entries = get_default_degraded_store().snapshot()
            assert len(entries) == 1
            entry = entries[0]
            assert entry.reason == expected_reason
            assert entry.axis == "engine_resources"
            # Title/body tokens MUST derive from the reason suffix so the
            # i18n keys at degraded.engine_resources.<reason>.title resolve.
            suffix = expected_reason.split(".", 1)[1]
            assert entry.title_token == f"degraded.engine_resources.{suffix}.title"
            assert entry.body_token == f"degraded.engine_resources.{suffix}.body"


class TestAdrD8ChipMapping:
    """Mission H4 §4.8 ADR-D8 + v0.49.25 — per-cohort-reason chip mapping.

    Validates that each reason produces 2 chips with cohort-specific
    target URLs (NOT the generic ``/engine/resources`` fallback). Closes
    the v0.49.24 audit-cycle finding that chips were 1-per-reason with
    a phantom ``/engine/resources`` target.
    """

    def test_rss_growth_chips_are_heap_snapshot_plus_doctor(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.rss_growth_spike", {})
        assert len(chips) == 2
        # Primary chip routes at a heap-snapshot deep-link (latest_ts
        # substituted to /engine/resources#heap when no file persisted).
        assert chips[0].label_token == "degraded.engine_resources.actions.viewHeapSnapshot"
        assert chips[0].action == "navigate"
        assert chips[0].target.startswith("/engine/resources")
        # Secondary chip is the doctor CLI hint.
        assert chips[1].label_token == "degraded.engine_resources.actions.openDoctor"
        assert chips[1].action == "command_hint"

    def test_thread_count_chips_are_thread_snapshot_plus_doctor(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.thread_count_spike", {})
        assert len(chips) == 2
        assert chips[0].label_token == "degraded.engine_resources.actions.viewThreadSnapshot"
        assert chips[0].target.startswith("/engine/resources")

    def test_lock_dict_chips_anchor_plus_docs(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.lock_dict_cardinality_saturated", {})
        assert len(chips) == 2
        assert chips[0].target == "/engine/resources#lock-dicts"
        assert chips[1].label_token == "degraded.engine_resources.actions.adjustLruDocs"
        assert chips[1].action == "external_link"

    def test_onnx_chips_anchor_plus_doctor(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.onnx_session_unexpected_count", {})
        assert len(chips) == 2
        assert chips[0].target == "/engine/resources#onnx"

    def test_exception_cohort_chips_anchor_plus_c2_link(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.exception_cohort_retention_high", {})
        assert len(chips) == 2
        assert chips[0].target == "/engine/resources#exception-cohort"
        assert chips[1].label_token == "degraded.engine_resources.actions.viewRecent500s"

    def test_heap_snapshot_triggered_chips_view_plus_ack(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason(
            "engine_resources.heap_snapshot_triggered",
            {"heap_snapshot_timestamp": 1716143280},
        )
        assert len(chips) == 2
        assert chips[0].target == "/engine/resources/heap-snapshot/1716143280"
        assert chips[1].label_token == "degraded.engine_resources.actions.ack"
        assert chips[1].action == "api_post"
        assert chips[1].target == "/api/engine/resources/cohort/ack"

    def test_unknown_reason_falls_back_to_generic_chip(self) -> None:
        from sovyx.observability._resource_cohort_governor import _chips_for_reason

        chips = _chips_for_reason("engine_resources.future_reason_v2", {})
        # Fallback is 1 generic chip (current behaviour — a new reason
        # added in a future minor MUST land with a paired chip mapping
        # entry; the fallback exists so the banner does not crash).
        assert len(chips) == 1
        assert chips[0].target == "/engine/resources"


class TestAdrD6SeverityEscalation:
    """Mission H4 §4.6 ADR-D6 + v0.49.29 — combined severity escalation.

    Verifies that ``_record_to_composite_store`` produces DegradedEntries
    whose severity escalates per ADR-D6:

    * Cross-cohort: 1 cohort = warn, 2 = error, 3+ = critical.
    * Temporal: 1st = warn, 2nd within 5 min = error, 3rd within 1 h = critical.
    * Combined: max of the two layers.
    """

    def test_single_cohort_first_breach_is_warning(self) -> None:
        """Baseline: 1 cohort, 1st breach in window → severity="warning"."""
        from sovyx.observability._resource_cohort_governor import (
            CohortEvaluation,
            _record_to_composite_store,
        )

        evaluation = CohortEvaluation(
            axis=CohortAxis.RSS_GROWTH,
            verdict=CohortVerdict.BUDGET_EXCEEDED,
            observed=1024,
            budget=512,
        )
        _record_to_composite_store(evaluation)
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].severity == "warning"

    def test_two_distinct_cohorts_escalates_to_error(self) -> None:
        """Cross-cohort: 2 different cohort axes in engine_resources → "error"."""
        from sovyx.observability._resource_cohort_governor import (
            CohortEvaluation,
            _record_to_composite_store,
        )

        # First cohort (RSS_GROWTH).
        _record_to_composite_store(
            CohortEvaluation(
                axis=CohortAxis.RSS_GROWTH,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=1024,
                budget=512,
            ),
        )
        # Second cohort (THREAD_COUNT) — co-occurrence → escalate.
        _record_to_composite_store(
            CohortEvaluation(
                axis=CohortAxis.THREAD_COUNT,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=64,
                budget=32,
            ),
        )
        entries = get_default_degraded_store().snapshot()
        # Both entries in store; the LATEST one was computed with 2
        # distinct cohorts → severity = error.
        engine_entries = [e for e in entries if e.axis == "engine_resources"]
        assert len(engine_entries) == 2
        # The thread_count entry (recorded second) sees both cohorts
        # in the store → severity = error per cross-cohort layer.
        thread_entry = next(e for e in engine_entries if "thread_count" in e.reason)
        assert thread_entry.severity == "error"

    def test_three_distinct_cohorts_escalates_to_critical(self) -> None:
        """Cross-cohort: 3 different cohort axes → "critical"."""
        from sovyx.observability._resource_cohort_governor import (
            CohortEvaluation,
            _record_to_composite_store,
        )

        for axis in [
            CohortAxis.RSS_GROWTH,
            CohortAxis.THREAD_COUNT,
            CohortAxis.LOCK_DICT_CARDINALITY,
        ]:
            _record_to_composite_store(
                CohortEvaluation(
                    axis=axis,
                    verdict=CohortVerdict.BUDGET_EXCEEDED,
                    observed=1024,
                    budget=512,
                ),
            )
        entries = get_default_degraded_store().snapshot()
        engine_entries = [e for e in entries if e.axis == "engine_resources"]
        # The 3rd entry (lock_dict) sees 3 distinct cohorts → critical.
        lock_entry = next(e for e in engine_entries if "lock_dict" in e.reason)
        assert lock_entry.severity == "critical"

    def test_temporal_second_breach_within_5min_escalates_to_error(self) -> None:
        """Temporal: same cohort, 2nd breach in <5 min → "error"."""
        from sovyx.observability._resource_cohort_governor import (
            _compute_engine_resources_severity,
            get_default_resource_cohort_governor,
        )

        governor = get_default_resource_cohort_governor()
        # Simulate one prior breach for RSS_GROWTH.
        governor.record_breach(CohortAxis.RSS_GROWTH)
        # Now record a second; severity should be "error".
        governor.record_breach(CohortAxis.RSS_GROWTH)
        severity = _compute_engine_resources_severity(CohortAxis.RSS_GROWTH)
        assert severity == "error"

    def test_temporal_third_breach_within_1h_escalates_to_critical(self) -> None:
        """Temporal: same cohort, 3rd breach in <1 h → "critical"."""
        from sovyx.observability._resource_cohort_governor import (
            _compute_engine_resources_severity,
            get_default_resource_cohort_governor,
        )

        governor = get_default_resource_cohort_governor()
        for _ in range(3):
            governor.record_breach(CohortAxis.RSS_GROWTH)
        severity = _compute_engine_resources_severity(CohortAxis.RSS_GROWTH)
        assert severity == "critical"

    def test_severity_combined_takes_max(self) -> None:
        """Cross=warning + temporal=error → final = "error" (max)."""
        from sovyx.observability._resource_cohort_governor import (
            _compute_engine_resources_severity,
            get_default_resource_cohort_governor,
        )

        governor = get_default_resource_cohort_governor()
        # Only 1 cohort recorded → cross=warning.
        # 2 temporal breaches → temporal=error.
        governor.record_breach(CohortAxis.RSS_GROWTH)
        governor.record_breach(CohortAxis.RSS_GROWTH)
        severity = _compute_engine_resources_severity(CohortAxis.RSS_GROWTH)
        # Layer max → "error" (from temporal layer).
        assert severity == "error"

    def test_emit_axis_entries_records_breach_before_severity(self) -> None:
        """Mission H4 v0.49.29 — emit_axis_entries reorders so the
        breach is recorded BEFORE _record_to_composite_store. This way
        the severity computation sees the current breach in the
        temporal window.
        """
        governor = ResourceCohortGovernor()
        # Force a BUDGET_EXCEEDED via repeated evaluate_snapshot calls.
        governor.evaluate_snapshot({"process.rss_bytes": 100_000_000})
        results = governor.evaluate_snapshot({"process.rss_bytes": 2_000_000_000})
        # ALSO trip the THREAD_COUNT cohort via evaluate.
        governor.evaluate_snapshot({"process.num_threads": 20})
        thread_results = governor.evaluate_snapshot({"process.num_threads": 200})
        emit_axis_entries(results + thread_results)
        entries = get_default_degraded_store().snapshot()
        engine_entries = [e for e in entries if e.axis == "engine_resources"]
        # 2 cohorts breached → at least one entry should have severity
        # "error" (cross-cohort layer).
        severities = {e.severity for e in engine_entries}
        assert "error" in severities, (
            f"expected at least one error-severity entry; got {severities}"
        )
