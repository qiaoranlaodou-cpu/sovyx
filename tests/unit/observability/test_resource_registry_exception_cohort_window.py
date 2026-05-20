"""F-002 + F-003 regression — MISSION-A.1.P2 exception_cohort window/lifetime split.

Mission anchor:
``docs-internal/missions/MISSION-A1-runtime-truth-remediation-2026-05-20.md``
§T2.1..T2.10.

Pre-fix the registry emitted ``exception_cohort.retained_bytes_estimate`` as
a monotonic ``+=`` accumulator under a field name that implied current-window
semantics, and ``exception_cohort.distinct_group_id_count`` as the size of an
unbounded ``set``. The cohort governor compared the lifetime sum against a
real-time cap, so the cohort verdict became permanently breached after a
single storm — no decay, no reset, no recovery absent process restart.

Post-fix the registry splits each into:

* ``exception_cohort.cumulative_*`` — pre-fix monotonic semantics, explicit
  about lifetime accumulation.
* ``exception_cohort.window_*`` — rolling window sized by
  ``tuning.exception_cohort_window_s``; decays as observations age out of
  the bounded ``observations`` deque. The governor reads the WINDOW field.

The legacy keys (``retained_bytes_estimate``, ``distinct_group_id_count``)
remain LENIENT shims aliased to the cumulative values for one minor cycle;
STRICT-flip at v0.55.0 drops them (ADR-D14, anti-pattern #49).
"""

from __future__ import annotations

import time

import pytest

from sovyx.observability._resource_registry import (
    ResourceRegistry,
    reset_default_resource_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Isolate each test from the module-level singleton."""
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


class TestExceptionCohortWindowAndCumulative:
    """F-002 + F-003 — cumulative vs window split."""

    def test_cumulative_accumulates_monotonically(self) -> None:
        """``cumulative_retained_bytes_since_start`` is ``+=`` since start."""
        reg = ResourceRegistry()
        reg.record_exception_cohort(
            group_id="g1", sub_exception_count=1, retained_bytes_estimate=4 * 1024 * 1024
        )
        reg.record_exception_cohort(
            group_id="g2", sub_exception_count=1, retained_bytes_estimate=8 * 1024 * 1024
        )
        reg.record_exception_cohort(
            group_id="g3", sub_exception_count=1, retained_bytes_estimate=16 * 1024 * 1024
        )
        fields = reg.snapshot_fields(exception_cohort_window_s=60.0)
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 28 * 1024 * 1024
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 3

    def test_window_includes_only_recent_observations(self) -> None:
        """``window_retained_bytes`` sums only observations within window_s.

        Inject observations spanning > window_s; assert window field reads only
        those still inside the window. Uses a long window_s + brief sleeps so
        the test is robust to coarse Windows monotonic-clock ticks.
        """
        reg = ResourceRegistry()
        # Observation A — far back in deque (will be outside the window).
        reg.record_exception_cohort(
            group_id="old_a", sub_exception_count=1, retained_bytes_estimate=100_000_000
        )
        old_ts = reg._exception_cohort.observations[-1][0]
        # Synthesize a much-older deque entry by mutating the deque directly so
        # we don't need long sleeps; the public API still drives the lifetime path.
        reg._exception_cohort.observations[-1] = (old_ts - 600.0, "old_a", 100_000_000)
        # Observation B — fresh, inside the window.
        reg.record_exception_cohort(
            group_id="new_b", sub_exception_count=1, retained_bytes_estimate=50_000_000
        )
        # window_s = 60s; only the fresh observation should count.
        fields = reg.snapshot_fields(exception_cohort_window_s=60.0)
        assert fields["exception_cohort.window_retained_bytes"] == 50_000_000
        # Cumulative still counts both.
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 150_000_000

    def test_window_distinct_count_decays(self) -> None:
        """``window_distinct_group_id_count`` decays as observations age out.

        Same setup as above — old group_id falls outside the window, fresh one
        is inside.
        """
        reg = ResourceRegistry()
        reg.record_exception_cohort(
            group_id="old_a", sub_exception_count=1, retained_bytes_estimate=1
        )
        old_ts = reg._exception_cohort.observations[-1][0]
        reg._exception_cohort.observations[-1] = (old_ts - 600.0, "old_a", 1)
        reg.record_exception_cohort(
            group_id="new_b", sub_exception_count=1, retained_bytes_estimate=1
        )
        reg.record_exception_cohort(
            group_id="new_c", sub_exception_count=1, retained_bytes_estimate=1
        )
        fields = reg.snapshot_fields(exception_cohort_window_s=60.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 2  # new_b + new_c
        # Cumulative remains 3 — set never decays.
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 3

    def test_legacy_alias_declared_in_ssot(self) -> None:
        """SSoT FieldSpecs declare the LENIENT legacy aliases.

        The ``cumulative_*`` canonical entries each carry
        ``legacy_alias="<pre-fix-key>"`` so Gate 15 + future
        STRICT-flip tooling can mechanically locate the keys to drop at
        v0.55.0. The snapshotter (NOT ``snapshot_fields()``) emits the
        legacy keys alongside the cumulative ones during LENIENT,
        matching the ``system.rss_bytes`` precedent.
        """
        from sovyx.observability._resource_registry import _HEALTH_SNAPSHOT_FIELDS

        cumulative_retained = _HEALTH_SNAPSHOT_FIELDS[
            "exception_cohort.cumulative_retained_bytes_since_start"
        ]
        assert cumulative_retained.legacy_alias == "exception_cohort.retained_bytes_estimate"

        cumulative_distinct = _HEALTH_SNAPSHOT_FIELDS[
            "exception_cohort.cumulative_distinct_group_id_count"
        ]
        assert cumulative_distinct.legacy_alias == "exception_cohort.distinct_group_id_count"

    def test_window_s_none_falls_back_to_cumulative(self) -> None:
        """No window_s → window fields default to cumulative (leaf-callable safety)."""
        reg = ResourceRegistry()
        reg.record_exception_cohort(
            group_id="g1", sub_exception_count=1, retained_bytes_estimate=10_000
        )
        fields = reg.snapshot_fields()  # no window_s
        assert (
            fields["exception_cohort.window_retained_bytes"]
            == fields["exception_cohort.cumulative_retained_bytes_since_start"]
        )

    def test_duplicate_group_id_within_window_counted_once(self) -> None:
        """Same group_id observed N times within window counts as 1 distinct.

        ``window_distinct_group_id_count`` is the SET cardinality within window.
        ``window_retained_bytes`` and the cumulative bytes still sum all
        observations — bytes are additive even if the group_id repeats.
        """
        reg = ResourceRegistry()
        for _ in range(5):
            reg.record_exception_cohort(
                group_id="same", sub_exception_count=1, retained_bytes_estimate=1000
            )
        fields = reg.snapshot_fields(exception_cohort_window_s=60.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 1
        assert fields["exception_cohort.window_retained_bytes"] == 5000
        # Cumulative bytes accumulate 5×; distinct_group_ids set holds {"same"}.
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 5000
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 1

    def test_last_observation_monotonic_tracks_latest_observation(self) -> None:
        """``last_observation_monotonic`` ≈ ``time.monotonic()`` at record time."""
        reg = ResourceRegistry()
        before = time.monotonic()
        reg.record_exception_cohort(
            group_id="g1", sub_exception_count=1, retained_bytes_estimate=1
        )
        after = time.monotonic()
        fields = reg.snapshot_fields(exception_cohort_window_s=60.0)
        last_obs = fields["exception_cohort.last_observation_monotonic"]
        assert isinstance(last_obs, float)
        assert before <= last_obs <= after


class TestGovernorReadsWindowedField:
    """F-002 + F-003 — governor evaluates against windowed retention."""

    def test_governor_reads_window_field(self) -> None:
        """The governor's exception_cohort evaluator uses the windowed key.

        Construct a synthetic snapshot with a tiny ``window_retained_bytes``
        and a huge legacy ``retained_bytes_estimate``; the governor must
        verdict HEALTHY (reading the window field), NOT BUDGET_EXCEEDED.
        """
        from sovyx.engine.config import ObservabilityTuningConfig
        from sovyx.observability._resource_cohort_governor import (
            ResourceCohortGovernor,
            _budgets_from_tuning,
        )
        from sovyx.observability._resource_registry import CohortAxis

        tuning = ObservabilityTuningConfig()
        # cap is 16 MiB by default; pick a synthetic window_retained = 1 MiB
        # but legacy cumulative = 100 MiB. Governor MUST verdict HEALTHY.
        snapshot = {
            "exception_cohort.window_retained_bytes": 1 * 1024 * 1024,
            "exception_cohort.retained_bytes_estimate": 100 * 1024 * 1024,  # legacy
        }
        budgets = list(_budgets_from_tuning(tuning))
        gov = ResourceCohortGovernor(budgets=budgets)
        evaluations = gov.evaluate_snapshot(snapshot)
        exc_eval = next(e for e in evaluations if e.axis is CohortAxis.EXCEPTION_COHORT)
        assert exc_eval.verdict.name == "HEALTHY", exc_eval

    def test_governor_lenient_fallback_to_legacy_key(self) -> None:
        """When window key absent, governor falls back to legacy cumulative key.

        Snapshot omits ``window_retained_bytes`` (simulates an old log forwarder
        replay or a pre-A.1.P2 producer); governor reads the legacy key and
        produces a verdict. This preserves the LENIENT migration contract.
        """
        from sovyx.engine.config import ObservabilityTuningConfig
        from sovyx.observability._resource_cohort_governor import (
            ResourceCohortGovernor,
            _budgets_from_tuning,
        )
        from sovyx.observability._resource_registry import CohortAxis

        tuning = ObservabilityTuningConfig()
        snapshot = {
            # window key absent
            "exception_cohort.retained_bytes_estimate": 100 * 1024 * 1024,  # exceeds cap
        }
        budgets = list(_budgets_from_tuning(tuning))
        gov = ResourceCohortGovernor(budgets=budgets)
        evaluations = gov.evaluate_snapshot(snapshot)
        exc_eval = next(e for e in evaluations if e.axis is CohortAxis.EXCEPTION_COHORT)
        assert exc_eval.verdict.name == "BUDGET_EXCEEDED", exc_eval

    def test_governor_recovers_when_window_decays(self) -> None:
        """After a storm, governor verdict recovers as window observations age.

        First snapshot has a storm (window field = 64 MiB > cap 16 MiB) →
        BUDGET_EXCEEDED. Second snapshot — same registry, but the window has
        moved on so window field decayed to 0 → HEALTHY. This is the core
        operator-trust property that the pre-fix design violated.
        """
        from sovyx.engine.config import ObservabilityTuningConfig
        from sovyx.observability._resource_cohort_governor import (
            ResourceCohortGovernor,
            _budgets_from_tuning,
        )
        from sovyx.observability._resource_registry import CohortAxis

        tuning = ObservabilityTuningConfig()
        budgets = list(_budgets_from_tuning(tuning))
        gov = ResourceCohortGovernor(budgets=budgets)

        # Storm in-window.
        storm_snapshot = {
            "exception_cohort.window_retained_bytes": 64 * 1024 * 1024,
        }
        evaluations = gov.evaluate_snapshot(storm_snapshot)
        exc_eval = next(e for e in evaluations if e.axis is CohortAxis.EXCEPTION_COHORT)
        assert exc_eval.verdict.name == "BUDGET_EXCEEDED"

        # Same governor, later tick — window decayed.
        recovered_snapshot = {
            "exception_cohort.window_retained_bytes": 0,
        }
        evaluations = gov.evaluate_snapshot(recovered_snapshot)
        exc_eval = next(e for e in evaluations if e.axis is CohortAxis.EXCEPTION_COHORT)
        assert exc_eval.verdict.name == "HEALTHY"
