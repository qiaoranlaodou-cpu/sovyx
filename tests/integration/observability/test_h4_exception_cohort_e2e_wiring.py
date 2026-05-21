"""Mission B B-P0-2 — F-022 production wire end-to-end regression.

Forensic context. From Mission H4 v0.49.14 through Mission A.1 v0.49.36
the `record_exception_cohort` function was DEFINED in the registry but
NEVER CALLED from production code. Grep of `src/sovyx/` returned only
definitions + re-exports + the `_resource_remediation.py:369` docstring.
The EXCEPTION_COHORT cohort verdict was therefore permanently HEALTHY by
construction; the chip for `exception_cohort_retention_high` reason
(added by Mission A.2 F-019) was dead code.

Mission A.3 spec §A.3.P3 scheduled the wire-up for v0.50.1 (proposing
3 hook sites: 5xx handler / structlog processor / bridge). Mission B
investigation found:
* No `@app.exception_handler` decorators exist in src/sovyx/dashboard/.
* `ExceptionTreeProcessor` at `_exception_serializer.py:179-249` is
  already wired at `logging.py:369` and captures all 59 production
  `logger.exception(...)` sites via the existing pipeline.

Mission B B.1.P2 (this test) transfers F-022 closure from A.3 to B.1
and wires the producer at the single chokepoint. Feature flag default
False at v0.49.37; default-flip True at v0.49.38.

This file pins seven invariants:

1. Recording-enabled producer→consumer single-tick: raise → log → flush
   → snapshot shows `window_distinct_group_id_count == 1`.
2. 1-second monotonic-rounded group_id dedup: same exception class
   logged 5× within <1s → distinct count == 1.
3. Distinct exception classes → distinct counts.
4. Window decay: observation outside `exception_cohort_window_s` →
   window decays to 0; cumulative persists.
5. Governor pipeline: storm produces non-zero `window_retained_bytes`;
   governor evaluation produces a verdict aligned with budget.
6. Feature-flag-off no-op: recording flag False → window stays 0
   (v0.49.36 behavior preserved).
7. Bytes dedup within 1s: same group_id appended twice within the
   dedup window → bytes count once (closes B-P2-12).

Mission anchor: `docs-internal/MISSION-B-FINDINGS-REGISTER-2026-05-21.md`
§1 B-P0-2 + `docs-internal/MISSION-B-REMEDIATION-PLAN-2026-05-21.md`
§5 B.1.P2.
"""

from __future__ import annotations

import time

import pytest

import sovyx.observability._resource_registry as _registry_mod
from sovyx.observability._exception_cohort_record_helper import (
    record_from_exception,
)
from sovyx.observability._exception_serializer import ExceptionTreeProcessor
from sovyx.observability._resource_cohort_governor import (
    CohortVerdict,
    ResourceCohortGovernor,
    _budgets_from_tuning,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import (
    CohortAxis,
    ResourceRegistry,
    record_exception_cohort,
    reset_default_resource_registry,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_default_resource_registry()
    reset_default_resource_cohort_governor()
    yield
    reset_default_resource_registry()
    reset_default_resource_cohort_governor()


def _install_registry(*, observations_maxlen: int = 128) -> ResourceRegistry:
    """Force the registry singleton to a fresh instance with explicit maxlen."""
    registry = ResourceRegistry(exception_cohort_observations_maxlen=observations_maxlen)
    with _registry_mod._SINGLETON_LOCK:  # noqa: SLF001
        _registry_mod._SINGLETON = registry  # noqa: SLF001
    return registry


def _drive_one_exception(processor: ExceptionTreeProcessor, exc: BaseException) -> None:
    """Send one exception through the processor as structlog would."""
    event_dict = {"event": "test.exception", "exc_info": exc}
    processor(logger=None, method_name="error", event_dict=event_dict)


class TestProducerWireSingleTick:
    """Mission B B-P0-2 — minimum producer→consumer chain."""

    def test_recording_enabled_processor_populates_window(self) -> None:
        """Raise → log → snapshot. ``window_distinct_group_id_count`` MUST be 1.

        Falsifiability: pre-fix the processor never called
        ``record_exception_cohort`` — the snapshot field was 0 forever.
        """
        registry = _install_registry()
        processor = ExceptionTreeProcessor(record_cohort=True)
        try:
            raise ValueError("e2e")
        except ValueError as exc:
            _drive_one_exception(processor, exc)
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 1, (
            "B-P0-2 regression: producer wire did not deposit an observation."
        )
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] > 0


class TestGroupIdDedupWithin1Second:
    """The helper's group_id collapses identical exception types within 1s."""

    def test_same_class_5x_collapses_to_one_distinct(self) -> None:
        """Falsifiability: if dedup is wrong, distinct count would be 5."""
        registry = _install_registry()
        processor = ExceptionTreeProcessor(record_cohort=True)
        for _ in range(5):
            try:
                raise RuntimeError("same class")
            except RuntimeError as exc:
                _drive_one_exception(processor, exc)
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 1, (
            "B-P0-2 regression: 5× same-class raises did not collapse via "
            "1s monotonic-rounded group_id."
        )


class TestDistinctClassesYieldDistinctCounts:
    """Three different exception types in the same second → 3 distinct."""

    def test_three_classes_three_distinct(self) -> None:
        registry = _install_registry()
        processor = ExceptionTreeProcessor(record_cohort=True)
        for exc_cls in (ValueError, RuntimeError, KeyError):
            try:
                raise exc_cls("distinct")
            except exc_cls as exc:
                _drive_one_exception(processor, exc)
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 3


class TestWindowDecay:
    """Past `window_s` an observation must decay out of the windowed count."""

    def test_observation_outside_window_decays(self) -> None:
        """Backdate an observation; snapshot with a short window; verify decay.

        We mutate the deque directly to simulate aging — this matches the
        pattern used by the Mission A.1 window-decay tests (test
        infrastructure speed). The dedup-window 1s logic is independent
        of this aging mechanism.
        """
        registry = _install_registry()
        record_exception_cohort(
            group_id="ValueError@1",
            sub_exception_count=1,
            retained_bytes_estimate=100_000,
        )
        # Backdate the observation by 600s so it ages out of the 300s window.
        cohort = registry._exception_cohort  # noqa: SLF001
        old_ts, gid, bytes_ = cohort.observations[-1]
        cohort.observations[-1] = (old_ts - 600.0, gid, bytes_)

        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.window_retained_bytes"] == 0
        assert fields["exception_cohort.window_distinct_group_id_count"] == 0
        # Cumulative persists.
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 100_000


class TestGovernorPipeline:
    """Storm → governor evaluation produces a verdict consistent with budget."""

    def test_storm_drives_window_above_cap_and_governor_fires(self) -> None:
        registry = _install_registry()
        # Drive observations that exceed the 16 MiB default cap.
        for i in range(20):
            # Distinct group_ids so dedup doesn't collapse — simulate 20
            # different exception classes each retaining ~1 MiB.
            record_exception_cohort(
                group_id=f"ExcClass_{i}@{int(time.monotonic())}",
                sub_exception_count=1,
                retained_bytes_estimate=1_500_000,
            )
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)

        # Build a governor with default budgets so we can assert the verdict.
        from sovyx.engine.config import ObservabilityTuningConfig

        tuning = ObservabilityTuningConfig()
        governor = ResourceCohortGovernor(budgets=_budgets_from_tuning(tuning))
        results = governor.evaluate_snapshot(fields)
        exc_verdict = next(r for r in results if r.axis == CohortAxis.EXCEPTION_COHORT)
        assert exc_verdict.verdict == CohortVerdict.BUDGET_EXCEEDED, (
            f"Storm with {20 * 1_500_000} bytes window retention should "
            f"exceed default 16 MiB cap; got {exc_verdict.verdict} "
            f"observed={exc_verdict.observed}"
        )


class TestFeatureFlagOff:
    """Recording flag False preserves v0.49.36 behavior — producer dark."""

    def test_recording_disabled_processor_does_not_populate(self) -> None:
        """Falsifiability: this assertion would pass unchanged at v0.49.36."""
        registry = _install_registry()
        processor = ExceptionTreeProcessor(record_cohort=False)
        try:
            raise ValueError("not recorded")
        except ValueError as exc:
            _drive_one_exception(processor, exc)
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.window_distinct_group_id_count"] == 0
        assert fields["exception_cohort.window_retained_bytes"] == 0


class TestBytesDedupWithin1Second:
    """B-P2-12 closure: same group_id within 1s adds bytes ONCE."""

    def test_same_group_id_twice_within_dedup_window_bytes_once(self) -> None:
        """Falsifiability: pre-B.1.P2 the docstring claimed dedup but the
        bytes accumulator always added; this test would have failed with
        cumulative == 2 × bytes.
        """
        registry = _install_registry()
        gid = f"TestExc@{int(time.monotonic())}"
        record_exception_cohort(
            group_id=gid, sub_exception_count=1, retained_bytes_estimate=50_000
        )
        record_exception_cohort(
            group_id=gid, sub_exception_count=1, retained_bytes_estimate=50_000
        )
        fields = registry.snapshot_fields(exception_cohort_window_s=300.0)
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 50_000, (
            "B-P2-12 regression: same group_id within 1s dedup window must "
            "NOT double-count bytes (chain-walk duplicate)."
        )
        assert fields["exception_cohort.window_distinct_group_id_count"] == 1


class TestHelperHandlesNoneAndDefensively:
    """The helper MUST NOT crash the caller's logger.exception() call."""

    def test_helper_with_none_returns_silently(self) -> None:
        record_from_exception(None)  # type: ignore[arg-type] — defensive

    def test_helper_swallows_internal_errors(self) -> None:
        """Force an internal error by passing a non-exception object.

        The helper is called from the structlog hot path; observability
        rule §27.4 requires it must absorb its own failures.
        """
        # type: ignore — we intentionally pass a non-exception.
        record_from_exception("not an exception")  # type: ignore[arg-type]
