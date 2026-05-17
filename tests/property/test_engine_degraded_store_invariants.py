"""Hypothesis property tests for EngineDegradedStore invariants.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§9.3.

Property: for any sequence of ``record / clear_axis / clear_reason``
operations within reasonable bounds, the store's invariants hold:

1. Cardinality NEVER exceeds ``_MAX_ENTRIES`` (32) — eviction-on-overflow
   is deterministic + bounded.
2. ``clear_axis(X)`` removes EXACTLY the entries with ``axis == X``
   (never more, never less).
3. ``snapshot()`` returns a defensive copy — mutating it must not
   leak back into the store.
4. ``distinct_axes()`` is sorted + de-duplicated even under heavy
   churn.
5. ``occurrence_count`` is monotonic per ``reason`` — never goes
   backwards on an upsert.
6. ``first_observed_monotonic`` is preserved across upserts; only
   ``last_observed_monotonic`` is updated.
"""

from __future__ import annotations

import time

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.engine._degraded_store import (
    DegradedEntry,
    EngineDegradedStore,
)

_AXES = st.sampled_from(["voice", "llm", "stt", "brain", "bridges", "plugins"])
_REASONS = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
    min_size=1,
    max_size=24,
)
_SEVERITIES = st.sampled_from(["warn", "error", "critical"])


def _build_entry(axis: str, reason: str, severity: str, ts: float) -> DegradedEntry:
    return DegradedEntry(
        axis=axis,
        reason=reason,
        severity=severity,
        title_token=f"degraded.{axis}.title",
        body_token=f"degraded.{axis}.body",
        action_chips=(),
        metadata={},
        first_observed_monotonic=ts,
        last_observed_monotonic=ts,
        occurrence_count=1,
    )


_ENTRIES = st.builds(
    _build_entry,
    axis=_AXES,
    reason=_REASONS,
    severity=_SEVERITIES,
    ts=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
)


class TestEngineDegradedStoreInvariants:
    @given(entries=st.lists(_ENTRIES, min_size=1, max_size=80))
    @settings(max_examples=40, deadline=None)
    def test_cardinality_never_exceeds_max_entries(
        self,
        entries: list[DegradedEntry],
    ) -> None:
        store = EngineDegradedStore()
        for e in entries:
            store.record(e)
        assert len(store) <= EngineDegradedStore._MAX_ENTRIES

    @given(
        axis_a=_AXES,
        axis_b=_AXES,
        reasons=st.lists(_REASONS, min_size=2, max_size=10, unique=True),
    )
    @settings(max_examples=30, deadline=None)
    def test_clear_axis_removes_exactly_matching_axis(
        self,
        axis_a: str,
        axis_b: str,
        reasons: list[str],
    ) -> None:
        store = EngineDegradedStore()
        for i, r in enumerate(reasons):
            ax = axis_a if i % 2 == 0 else axis_b
            store.record(_build_entry(ax, r, "warn", float(i)))
        # Establish ground-truth counts
        snap = store.snapshot()
        expected_a = sum(1 for e in snap if e.axis == axis_a)
        expected_b = sum(1 for e in snap if e.axis == axis_b)

        removed = store.clear_axis(axis_a)
        assert removed == expected_a

        after = store.snapshot()
        # Only axis_b entries remain (when axis_a != axis_b)
        if axis_a != axis_b:
            assert all(e.axis != axis_a for e in after)
            assert len(after) == expected_b
        else:
            # If both axes are the same name, clear removed everything
            assert len(after) == 0

    @given(entries=st.lists(_ENTRIES, min_size=0, max_size=20))
    @settings(max_examples=20, deadline=None)
    def test_snapshot_returns_defensive_copy(
        self,
        entries: list[DegradedEntry],
    ) -> None:
        store = EngineDegradedStore()
        for e in entries:
            store.record(e)
        snap1 = store.snapshot()
        snap2 = store.snapshot()
        # Two snapshots are independent lists
        assert snap1 is not snap2
        # Mutating snap1 does NOT bleed back
        snap1.clear()
        assert len(store.snapshot()) == len({e.reason for e in entries})

    @given(entries=st.lists(_ENTRIES, min_size=0, max_size=20))
    @settings(max_examples=20, deadline=None)
    def test_distinct_axes_sorted_unique(
        self,
        entries: list[DegradedEntry],
    ) -> None:
        store = EngineDegradedStore()
        for e in entries:
            store.record(e)
        axes = store.distinct_axes()
        assert axes == sorted(set(axes))
        assert len(axes) == len(set(axes))

    @given(
        axis=_AXES,
        reason=_REASONS,
        upsert_count=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=20, deadline=None)
    def test_occurrence_count_monotonic_per_reason(
        self,
        axis: str,
        reason: str,
        upsert_count: int,
    ) -> None:
        store = EngineDegradedStore()
        for i in range(upsert_count):
            store.record(_build_entry(axis, reason, "warn", float(i)))
        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0].occurrence_count == upsert_count

    @given(
        axis=_AXES,
        reason=_REASONS,
        first_ts=st.floats(min_value=0.0, max_value=1e3, allow_nan=False),
        later_ts=st.floats(min_value=2e3, max_value=1e6, allow_nan=False),
    )
    @settings(max_examples=20, deadline=None)
    def test_first_observed_preserved_across_upsert(
        self,
        axis: str,
        reason: str,
        first_ts: float,
        later_ts: float,
    ) -> None:
        store = EngineDegradedStore()
        store.record(_build_entry(axis, reason, "warn", first_ts))
        store.record(_build_entry(axis, reason, "error", later_ts))
        snap = store.snapshot()
        assert snap[0].first_observed_monotonic == first_ts
        assert snap[0].last_observed_monotonic == later_ts
        # Severity wins on latest
        assert snap[0].severity == "error"

    def test_real_monotonic_clock_invariants(self) -> None:
        """Smoke check that the store works under real time.monotonic()
        values (Hypothesis often generates floats far outside that
        range). Pins the contract under realistic operator-session
        wall-clock numbers."""
        store = EngineDegradedStore()
        for i in range(10):
            store.record(_build_entry("voice", f"r{i}", "warn", time.monotonic()))
        assert 1 <= len(store) <= 10
        store.clear_axis("voice")
        assert len(store) == 0
