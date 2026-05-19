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
        assert any(e.reason == "engine_resources.thread_count" for e in engine_axis_entries)

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
