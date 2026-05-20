"""Hypothesis property tests for Mission H4 `_resource_registry` invariants.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§10.3 (≥ 8 invariants).

Property classes:

1. ``ResourceRegistry.snapshot_fields()`` always returns a dict whose
   keys are a subset of :data:`_HEALTH_SNAPSHOT_FIELDS`.
2. ``register_onnx_session`` is idempotent on label collision
   (same label twice does not double-count).
3. ``register_lock_dict`` is idempotent on owner_id collision.
4. ``record_to_thread_dispatch`` is monotonic on total counter
   (never decreases across calls).
5. ``record_to_thread_dispatch`` per-label count tracks the number of
   calls with that label (modulo ``_MAX_TRACKED_LABELS`` overflow).
6. ``record_exception_cohort`` deduplicates group_ids while
   accumulating retained_bytes.
7. ``snapshot_fields().lock_dict.total_cardinality`` equals the sum
   of all per-owner lengths.
8. Resetting the singleton produces a fresh registry whose
   counters are all zero.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.observability._resource_registry import (
    _HEALTH_SNAPSHOT_FIELDS,
    _MAX_TRACKED_LABELS,
    ResourceRegistry,
    get_default_resource_registry,
    reset_default_resource_registry,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


class _FakeSession:
    """Stand-in for ``onnxruntime.InferenceSession`` (supports weakref)."""


class _FakeLockDict:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return max(0, self._size)


# ── Property 1: snapshot_fields keys are a subset of SSoT ──


@given(num_onnx=st.integers(min_value=0, max_value=10))
@settings(max_examples=20, deadline=None)
def test_snapshot_keys_subset_of_ssot(num_onnx: int) -> None:
    reg = ResourceRegistry()
    sessions: list[Any] = [_FakeSession() for _ in range(num_onnx)]
    for i, s in enumerate(sessions):
        reg.register_onnx_session(label=f"label-{i}", session=s)
    fields = reg.snapshot_fields()
    for key in fields:
        assert key in _HEALTH_SNAPSHOT_FIELDS, (
            f"snapshot key {key!r} missing from _HEALTH_SNAPSHOT_FIELDS SSoT"
        )


# ── Property 2: ONNX register idempotent on label collision ──


@given(num_registrations=st.integers(min_value=1, max_value=15))
@settings(max_examples=20, deadline=None)
def test_onnx_register_idempotent_on_label_collision(num_registrations: int) -> None:
    reg = ResourceRegistry()
    sessions: list[Any] = []
    for _ in range(num_registrations):
        s = _FakeSession()
        sessions.append(s)
        reg.register_onnx_session(label="same-label", session=s)
    fields = reg.snapshot_fields()
    # WeakValueDictionary semantics: only the LAST registration is held;
    # session_count reflects that.
    assert fields["onnx.session_count"] == 1
    assert fields["onnx.session_labels"] == ["same-label"]


# ── Property 3: LRULockDict register idempotent on owner_id collision ──


@given(
    num_registrations=st.integers(min_value=1, max_value=15),
    last_size=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=20, deadline=None)
def test_lockdict_register_idempotent_on_owner_id(num_registrations: int, last_size: int) -> None:
    reg = ResourceRegistry()
    dicts: list[_FakeLockDict] = []
    for i in range(num_registrations):
        size = last_size if i == num_registrations - 1 else 99
        d = _FakeLockDict(size)
        dicts.append(d)
        reg.register_lock_dict(owner_id="same-owner", dict_ref=d)
    fields = reg.snapshot_fields()
    # Only the LAST registration is held — single owner_id.
    assert fields["lock_dict.instance_count"] == 1
    assert fields["lock_dict.per_owner"]["same-owner"] == last_size


# ── Property 4: to_thread total counter is monotonic ──


@given(
    dispatches=st.lists(
        st.tuples(
            st.text(min_size=1, max_size=20),
            st.integers(min_value=0, max_value=100),
            st.integers(min_value=0, max_value=100),
            st.integers(min_value=1, max_value=64),
        ),
        min_size=0,
        max_size=50,
    )
)
@settings(max_examples=20, deadline=None)
def test_to_thread_total_count_monotonic(
    dispatches: list[tuple[str, int, int, int]],
) -> None:
    reg = ResourceRegistry()
    last_total = 0
    for label, workers, queue, max_workers in dispatches:
        reg.record_to_thread_dispatch(
            label=label,
            worker_count_at_dispatch=workers,
            queue_depth=queue,
            max_workers=max_workers,
        )
        current = reg.snapshot_fields()["to_thread.dispatch_count_total"]
        assert current >= last_total, "dispatch counter must never decrease"
        last_total = current


# ── Property 5: per-label count tracks calls (modulo overflow) ──


@given(label_repeats=st.integers(min_value=1, max_value=30))
@settings(max_examples=20, deadline=None)
def test_to_thread_per_label_tracks_call_count(label_repeats: int) -> None:
    reg = ResourceRegistry()
    label = "test.label"
    for _ in range(label_repeats):
        reg.record_to_thread_dispatch(
            label=label, worker_count_at_dispatch=0, queue_depth=0, max_workers=0
        )
    fields = reg.snapshot_fields()
    per_label = fields["to_thread.dispatch_count_per_label"]
    assert per_label[label] == label_repeats
    assert fields["to_thread.dispatch_count_total"] == label_repeats


# ── Property 6: exception cohort dedup + accumulation ──


@given(
    observations=st.lists(
        st.tuples(
            st.text(min_size=1, max_size=10),
            st.integers(min_value=1, max_value=1024),
        ),
        min_size=1,
        max_size=30,
    )
)
@settings(max_examples=20, deadline=None)
def test_exception_cohort_dedup_and_accumulation(
    observations: list[tuple[str, int]],
) -> None:
    reg = ResourceRegistry()
    expected_bytes = 0
    distinct_groups: set[str] = set()
    for group_id, retained in observations:
        reg.record_exception_cohort(
            group_id=group_id,
            sub_exception_count=1,
            retained_bytes_estimate=retained,
        )
        expected_bytes += retained
        distinct_groups.add(group_id)
    fields = reg.snapshot_fields()
    # MISSION-A.1.P2 (F-002+F-003): cumulative fields are the canonical
    # lifetime accumulators; legacy keys are LENIENT shims emitted by the
    # snapshotter, not by snapshot_fields() itself.
    assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == expected_bytes
    assert fields["exception_cohort.cumulative_distinct_group_id_count"] == len(distinct_groups)


# ── Property 7: lock_dict.total_cardinality == sum(per_owner) ──


@given(
    owners=st.lists(
        st.tuples(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=["L", "N"], whitelist_characters="._-"
                ),
                min_size=1,
                max_size=15,
            ).filter(lambda s: s.strip()),
            st.integers(min_value=0, max_value=200),
        ),
        min_size=0,
        max_size=20,
        unique_by=lambda x: x[0],
    )
)
@settings(max_examples=20, deadline=None)
def test_lock_dict_total_equals_sum_per_owner(owners: list[tuple[str, int]]) -> None:
    reg = ResourceRegistry()
    dicts: list[_FakeLockDict] = []
    for owner_id, size in owners:
        d = _FakeLockDict(size)
        dicts.append(d)
        reg.register_lock_dict(owner_id=owner_id, dict_ref=d)
    fields = reg.snapshot_fields()
    total = fields["lock_dict.total_cardinality"]
    sum_per_owner = sum(fields["lock_dict.per_owner"].values())
    assert total == sum_per_owner, (
        f"total_cardinality ({total}) must equal sum of per_owner "
        f"({sum_per_owner}) — registry invariant"
    )


# ── Property 8: reset produces a fresh registry ──


@given(
    num_onnx=st.integers(min_value=0, max_value=5),
    num_dispatches=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=10, deadline=None)
def test_reset_produces_fresh_registry(num_onnx: int, num_dispatches: int) -> None:
    sessions: list[Any] = []
    for i in range(num_onnx):
        s = _FakeSession()
        sessions.append(s)
        get_default_resource_registry().register_onnx_session(label=f"label-{i}", session=s)
    for _ in range(num_dispatches):
        get_default_resource_registry().record_to_thread_dispatch(
            label="x", worker_count_at_dispatch=0, queue_depth=0, max_workers=0
        )
    reset_default_resource_registry()
    fresh = get_default_resource_registry()
    fields = fresh.snapshot_fields()
    assert fields["onnx.session_count"] == 0
    assert fields["to_thread.dispatch_count_total"] == 0
    assert fields["lock_dict.instance_count"] == 0
    assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 0


# ── Property 9 (bonus): overflow coalescing for to_thread labels ──


@given(extra_labels=st.integers(min_value=1, max_value=20))
@settings(max_examples=10, deadline=None)
def test_to_thread_label_overflow_coalesces(extra_labels: int) -> None:
    """Beyond _MAX_TRACKED_LABELS, new labels coalesce into _overflow_."""
    reg = ResourceRegistry()
    # Fill the cap with distinct labels.
    for i in range(_MAX_TRACKED_LABELS):
        reg.record_to_thread_dispatch(
            label=f"label-{i}",
            worker_count_at_dispatch=0,
            queue_depth=0,
            max_workers=0,
        )
    # Now overflow with new labels.
    for i in range(extra_labels):
        reg.record_to_thread_dispatch(
            label=f"overflow-{i}",
            worker_count_at_dispatch=0,
            queue_depth=0,
            max_workers=0,
        )
    fields = reg.snapshot_fields()
    per_label = fields["to_thread.dispatch_count_per_label"]
    assert "_overflow_" in per_label
    assert per_label["_overflow_"] == extra_labels
    # Total count tracks ALL dispatches (including overflow).
    assert fields["to_thread.dispatch_count_total"] == _MAX_TRACKED_LABELS + extra_labels
