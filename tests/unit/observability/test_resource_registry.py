"""Unit tests for Mission H4 §T1.1 — ``_resource_registry`` SSoT.

Coverage targets:

* :class:`ResourceRegistry` lifetime (singleton, reset, thread-safety).
* ONNX session registration via weakref tracking + GC reclamation.
* :class:`LRULockDict` registration via weakref + reaping.
* ``record_to_thread_dispatch`` per-label counter + overflow handling.
* ``record_exception_cohort`` retention estimate + deduplication.
* ``snapshot_fields`` shape parity with :data:`_HEALTH_SNAPSHOT_FIELDS`.
* :data:`_HEALTH_SNAPSHOT_FIELDS` invariants (FieldSpec, legacy alias,
  exhaustiveness, type constraints).
* :class:`CohortAxis` :class:`StrEnum` compliance (anti-pattern #9).
"""

from __future__ import annotations

import gc
import threading
import tracemalloc

import pytest

from sovyx.observability._resource_registry import (
    _HEALTH_SNAPSHOT_FIELDS,
    CohortAxis,
    FieldSpec,
    ResourceRegistry,
    get_default_resource_registry,
    record_exception_cohort,
    record_to_thread_dispatch,
    register_lock_dict,
    register_onnx_session,
    reset_default_resource_registry,
)

# ── Fixtures ──


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Each test starts with a fresh singleton."""
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


# ── _HEALTH_SNAPSHOT_FIELDS invariants ──


class TestHealthSnapshotFieldsSSoT:
    """The SSoT mapping is the single source of truth — invariants must hold."""

    def test_ssot_is_non_empty(self) -> None:
        assert len(_HEALTH_SNAPSHOT_FIELDS) > 0

    def test_every_value_is_field_spec(self) -> None:
        for spec in _HEALTH_SNAPSHOT_FIELDS.values():
            assert isinstance(spec, FieldSpec)

    def test_canonical_key_matches_dict_key(self) -> None:
        for key, spec in _HEALTH_SNAPSHOT_FIELDS.items():
            assert key == spec.canonical_key

    def test_process_rss_bytes_has_legacy_alias(self) -> None:
        spec = _HEALTH_SNAPSHOT_FIELDS["process.rss_bytes"]
        assert spec.legacy_alias == "system.rss_bytes"

    def test_h4_new_fields_present(self) -> None:
        # MISSION-A.1.P2 (F-002+F-003): the legacy
        # ``exception_cohort.retained_bytes_estimate`` and
        # ``distinct_group_id_count`` are no longer SSoT canonical keys —
        # they're declared as ``legacy_alias`` of the new ``cumulative_*``
        # fields (precedent: ``system.rss_bytes`` is the legacy alias of
        # ``process.rss_bytes``). The snapshotter LENIENT-emits both pairs
        # until v0.55.0 STRICT-flip.
        expected_h4 = {
            # MISSION-A.1.P3.b (F-007, ADR-D16, anti-pattern #51): three
            # twin-named stale fields renamed to ``_at_last_dispatch``.
            "to_thread.pool_size_at_last_dispatch",
            "to_thread.queue_depth_at_last_dispatch",
            "to_thread.max_workers_at_last_dispatch",
            "to_thread.dispatch_count_total",
            "to_thread.dispatch_count_per_label",
            "lock_dict.total_cardinality",
            "lock_dict.per_owner",
            "lock_dict.instance_count",
            "onnx.session_count",
            "onnx.session_labels",
            "gc.collections_by_gen",
            "gc.objects_count",
            "tracemalloc.is_tracing",
            "tracemalloc.current_kb",
            "tracemalloc.peak_kb",
            "exception_cohort.cumulative_retained_bytes_since_start",
            "exception_cohort.cumulative_distinct_group_id_count",
            "exception_cohort.window_retained_bytes",
            "exception_cohort.window_distinct_group_id_count",
            "exception_cohort.last_observation_monotonic",
            # MISSION-A.1.P3 (F-005, ADR-D15, anti-pattern #50):
            # ``asyncio.all_task_names`` replaces the observation-paradox
            # field ``asyncio.current_running_task_name`` (always returned
            # the snapshotter task name).
            "asyncio.all_task_names",
        }
        assert expected_h4.issubset(_HEALTH_SNAPSHOT_FIELDS.keys())
        # MISSION-A.1.P3 (F-006, ADR-D15, anti-pattern #48):
        # ``to_thread.active_workers`` was a literal alias of pool_size
        # ("passes the falsifiability gate literally"). Removed from
        # canonical SSoT; declared as ``legacy_alias=`` on
        # ``to_thread.pool_size``. LENIENT-emitted by the snapshotter
        # only; sunset v0.55.0.
        assert "to_thread.active_workers" not in _HEALTH_SNAPSHOT_FIELDS
        assert "asyncio.current_running_task_name" not in _HEALTH_SNAPSHOT_FIELDS
        assert (
            _HEALTH_SNAPSHOT_FIELDS["asyncio.all_task_names"].legacy_alias
            == "asyncio.current_running_task_name"
        )
        # MISSION-A.1.P3.b (F-007, ADR-D16, anti-pattern #51): twin-name
        # stale fields renamed. The legacy ``to_thread.{pool_size,
        # max_workers, queue_depth}`` keys are removed from canonical
        # SSoT; declared as ``legacy_alias=`` on the
        # ``_at_last_dispatch`` canonicals. Note: ``to_thread.active_workers``
        # (F-006) is a shim-of-a-shim — active_workers → pool_size →
        # pool_size_at_last_dispatch — the SSoT only tracks one hop per
        # canonical; ADR-D16 documents the chain.
        assert "to_thread.pool_size" not in _HEALTH_SNAPSHOT_FIELDS
        assert "to_thread.queue_depth" not in _HEALTH_SNAPSHOT_FIELDS
        assert "to_thread.max_workers" not in _HEALTH_SNAPSHOT_FIELDS
        assert (
            _HEALTH_SNAPSHOT_FIELDS["to_thread.pool_size_at_last_dispatch"].legacy_alias
            == "to_thread.pool_size"
        )
        assert (
            _HEALTH_SNAPSHOT_FIELDS["to_thread.queue_depth_at_last_dispatch"].legacy_alias
            == "to_thread.queue_depth"
        )
        assert (
            _HEALTH_SNAPSHOT_FIELDS["to_thread.max_workers_at_last_dispatch"].legacy_alias
            == "to_thread.max_workers"
        )
        # MISSION-A.1.P3.b (F-014, ADR-D16, anti-pattern #51): math-vs-name
        # rename. ``running_count`` and ``pending_count`` removed from
        # canonical SSoT; declared as ``legacy_alias=`` on
        # ``not_done_count`` / ``awaiting_count``.
        assert "asyncio.running_count" not in _HEALTH_SNAPSHOT_FIELDS
        assert "asyncio.pending_count" not in _HEALTH_SNAPSHOT_FIELDS
        assert (
            _HEALTH_SNAPSHOT_FIELDS["asyncio.not_done_count"].legacy_alias
            == "asyncio.running_count"
        )
        assert (
            _HEALTH_SNAPSHOT_FIELDS["asyncio.awaiting_count"].legacy_alias
            == "asyncio.pending_count"
        )

    def test_anomaly_consumer_includes_process_rss_bytes(self) -> None:
        spec = _HEALTH_SNAPSHOT_FIELDS["process.rss_bytes"]
        assert "sovyx.observability.anomaly" in spec.consumer_modules

    def test_section_values_are_known(self) -> None:
        known = {
            "process",
            "asyncio",
            "to_thread",
            "lock_dict",
            "onnx",
            "gc",
            "tracemalloc",
            "exception_cohort",
        }
        for spec in _HEALTH_SNAPSHOT_FIELDS.values():
            assert spec.section in known

    def test_canonical_keys_are_unique(self) -> None:
        """Mission H4 §11 ADR-D2 — SSoT uniqueness.

        Each field MUST have a globally-unique ``canonical_key`` so the
        producer-side ``register_*`` calls never silently shadow each
        other. A duplicate would invalidate the Gate 15 invariant that
        producer↔consumer parity is detectable at AST scan time.
        """
        canonical_keys = [spec.canonical_key for spec in _HEALTH_SNAPSHOT_FIELDS.values()]
        assert len(canonical_keys) == len(set(canonical_keys)), (
            "Duplicate canonical_key detected in _HEALTH_SNAPSHOT_FIELDS — "
            "Gate 15 invariant violated. Inspect FieldSpec entries for the "
            "duplicate and either rename or merge consumers/producers."
        )
        # Dict-key parity invariant tested in test_canonical_key_matches_dict_key
        # above; this test asserts the stronger property that no two entries
        # share the canonical_key value even if their dict keys differ.


class TestCohortAxis:
    """anti-pattern #9 — StrEnum compliance."""

    def test_str_enum_members(self) -> None:
        assert CohortAxis.RSS_GROWTH == "rss_growth"
        assert CohortAxis.THREAD_COUNT == "thread_count"
        assert CohortAxis.LOCK_DICT_CARDINALITY == "lock_dict_cardinality"
        assert CohortAxis.ONNX_SESSION == "onnx_session"
        assert CohortAxis.EXCEPTION_COHORT == "exception_cohort"

    def test_is_str_subclass(self) -> None:
        assert isinstance(CohortAxis.RSS_GROWTH, str)

    def test_value_comparison(self) -> None:
        # StrEnum comparison via .value or via implicit str coercion.
        assert CohortAxis.RSS_GROWTH.value == "rss_growth"
        assert str(CohortAxis.RSS_GROWTH) == "rss_growth"


# ── Singleton lifecycle ──


class TestSingletonLifecycle:
    def test_default_returns_same_instance(self) -> None:
        a = get_default_resource_registry()
        b = get_default_resource_registry()
        assert a is b

    def test_reset_yields_fresh_instance(self) -> None:
        a = get_default_resource_registry()
        reset_default_resource_registry()
        b = get_default_resource_registry()
        assert a is not b

    def test_singleton_is_thread_safe(self) -> None:
        """Concurrent first-access from many threads yields one instance."""
        reset_default_resource_registry()
        instances: list[ResourceRegistry] = []
        lock = threading.Lock()

        def collect() -> None:
            inst = get_default_resource_registry()
            with lock:
                instances.append(inst)

        threads = [threading.Thread(target=collect) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(instances) == 16
        assert all(inst is instances[0] for inst in instances)


# ── ONNX session registration ──


class _FakeOnnxSession:
    """Stand-in for ``onnxruntime.InferenceSession`` (supports weakref)."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name


class TestOnnxSessionRegistration:
    def test_register_increments_count(self) -> None:
        reg = ResourceRegistry()
        s = _FakeOnnxSession("vad")
        reg.register_onnx_session(label="voice.vad", session=s)
        fields = reg.snapshot_fields()
        assert fields["onnx.session_count"] == 1
        assert fields["onnx.session_labels"] == ["voice.vad"]

    def test_register_two_sessions(self) -> None:
        reg = ResourceRegistry()
        s1 = _FakeOnnxSession("a")
        s2 = _FakeOnnxSession("b")
        reg.register_onnx_session(label="voice.vad", session=s1)
        reg.register_onnx_session(label="brain.embedding", session=s2)
        fields = reg.snapshot_fields()
        assert fields["onnx.session_count"] == 2
        assert set(fields["onnx.session_labels"]) == {"voice.vad", "brain.embedding"}

    def test_weakref_reclaim_on_gc(self) -> None:
        reg = ResourceRegistry()
        s = _FakeOnnxSession("ephemeral")
        reg.register_onnx_session(label="ephemeral", session=s)
        assert reg.snapshot_fields()["onnx.session_count"] == 1
        del s
        gc.collect()
        # After GC the weakref entry vanishes.
        assert reg.snapshot_fields()["onnx.session_count"] == 0
        assert reg.snapshot_fields()["onnx.session_labels"] == []

    def test_register_unsupported_weakref_does_not_raise(self) -> None:
        """Some C-extension sessions cannot be weak-referenced — must degrade."""
        reg = ResourceRegistry()
        # An int cannot be weak-referenced.
        reg.register_onnx_session(label="bad", session=42)
        # Count stays 0 (weakref failed silently per design).
        assert reg.snapshot_fields()["onnx.session_count"] == 0


# ── LRULockDict registration ──


class _FakeLockDict:
    """Stand-in for ``LRULockDict``; supports ``len()`` + weakref."""

    def __init__(self, size: int = 0) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class TestLockDictRegistration:
    def test_register_exposes_per_owner_cardinality(self) -> None:
        reg = ResourceRegistry()
        d = _FakeLockDict(size=7)
        reg.register_lock_dict(owner_id="bridge.conv_locks", dict_ref=d)
        fields = reg.snapshot_fields()
        assert fields["lock_dict.per_owner"] == {"bridge.conv_locks": 7}
        assert fields["lock_dict.total_cardinality"] == 7
        assert fields["lock_dict.instance_count"] == 1

    def test_two_owners_sum_correctly(self) -> None:
        reg = ResourceRegistry()
        d1 = _FakeLockDict(size=5)
        d2 = _FakeLockDict(size=12)
        reg.register_lock_dict(owner_id="a", dict_ref=d1)
        reg.register_lock_dict(owner_id="b", dict_ref=d2)
        fields = reg.snapshot_fields()
        assert fields["lock_dict.total_cardinality"] == 17
        assert fields["lock_dict.instance_count"] == 2

    def test_weakref_reap_on_gc(self) -> None:
        reg = ResourceRegistry()
        d = _FakeLockDict(size=3)
        reg.register_lock_dict(owner_id="ephemeral", dict_ref=d)
        assert reg.snapshot_fields()["lock_dict.instance_count"] == 1
        del d
        gc.collect()
        fields = reg.snapshot_fields()
        assert fields["lock_dict.instance_count"] == 0
        assert fields["lock_dict.total_cardinality"] == 0


# ── to_thread dispatch counter ──


class TestToThreadDispatch:
    def test_records_total_count(self) -> None:
        reg = ResourceRegistry()
        for _ in range(5):
            reg.record_to_thread_dispatch(
                label="voice.vad.infer",
                worker_count_at_dispatch=4,
                queue_depth=1,
                max_workers=32,
            )
        # MISSION-A.1.P3.b (F-007, ADR-D16): canonical keys renamed to
        # ``_at_last_dispatch`` to make staleness explicit. Legacy keys
        # are LENIENT-emitted by the snapshotter, NOT by snapshot_fields().
        fields = reg.snapshot_fields()
        assert fields["to_thread.dispatch_count_total"] == 5
        assert fields["to_thread.dispatch_count_per_label"] == {"voice.vad.infer": 5}
        assert fields["to_thread.pool_size_at_last_dispatch"] == 4
        assert fields["to_thread.queue_depth_at_last_dispatch"] == 1
        assert fields["to_thread.max_workers_at_last_dispatch"] == 32

    def test_per_label_split(self) -> None:
        reg = ResourceRegistry()
        reg.record_to_thread_dispatch(
            label="a", worker_count_at_dispatch=0, queue_depth=0, max_workers=0
        )
        reg.record_to_thread_dispatch(
            label="b", worker_count_at_dispatch=0, queue_depth=0, max_workers=0
        )
        reg.record_to_thread_dispatch(
            label="a", worker_count_at_dispatch=0, queue_depth=0, max_workers=0
        )
        fields = reg.snapshot_fields()
        assert fields["to_thread.dispatch_count_per_label"] == {"a": 2, "b": 1}

    def test_label_cardinality_overflow(self) -> None:
        """Beyond _MAX_TRACKED_LABELS, new labels coalesce into _overflow_."""
        from sovyx.observability._resource_registry import _MAX_TRACKED_LABELS

        reg = ResourceRegistry()
        for i in range(_MAX_TRACKED_LABELS + 5):
            reg.record_to_thread_dispatch(
                label=f"label-{i}",
                worker_count_at_dispatch=0,
                queue_depth=0,
                max_workers=0,
            )
        fields = reg.snapshot_fields()
        per_label = fields["to_thread.dispatch_count_per_label"]
        assert "_overflow_" in per_label
        # Exactly 5 labels coalesced into overflow.
        assert per_label["_overflow_"] == 5


# ── Exception cohort counter ──


class TestExceptionCohort:
    def test_record_accumulates_bytes(self) -> None:
        reg = ResourceRegistry()
        reg.record_exception_cohort(
            group_id="g1", sub_exception_count=3, retained_bytes_estimate=1024
        )
        reg.record_exception_cohort(
            group_id="g2", sub_exception_count=1, retained_bytes_estimate=512
        )
        # MISSION-A.1.P2: snapshot_fields() emits the cumulative canonical
        # fields; the legacy ``retained_bytes_estimate`` /
        # ``distinct_group_id_count`` keys are LENIENT shims added by the
        # snapshotter (`resources.py::_emit_snapshot`), matching the
        # ``system.rss_bytes`` precedent.
        fields = reg.snapshot_fields()
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 1536
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 2

    def test_distinct_group_ids_dedup(self) -> None:
        reg = ResourceRegistry()
        for _ in range(5):
            reg.record_exception_cohort(
                group_id="same",
                sub_exception_count=1,
                retained_bytes_estimate=100,
            )
        fields = reg.snapshot_fields()
        # group_id set has one entry; retained_bytes accumulates regardless.
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 1
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 500


# ── snapshot_fields shape parity ──


class TestSnapshotFieldsShape:
    """Every key returned MUST appear in _HEALTH_SNAPSHOT_FIELDS."""

    def test_all_returned_keys_in_ssot(self) -> None:
        reg = ResourceRegistry()
        fields = reg.snapshot_fields()
        for key in fields:
            assert key in _HEALTH_SNAPSHOT_FIELDS, (
                f"{key} emitted but missing from _HEALTH_SNAPSHOT_FIELDS"
            )

    def test_to_thread_block_present_by_default(self) -> None:
        reg = ResourceRegistry()
        fields = reg.snapshot_fields()
        # MISSION-A.1.P3.b (F-007, ADR-D16): canonical is now
        # ``pool_size_at_last_dispatch``.
        assert "to_thread.pool_size_at_last_dispatch" in fields
        assert "to_thread.dispatch_count_total" in fields
        assert fields["to_thread.dispatch_count_total"] == 0

    def test_gc_collections_is_3_tuple_list(self) -> None:
        reg = ResourceRegistry()
        fields = reg.snapshot_fields()
        assert isinstance(fields["gc.collections_by_gen"], list)
        assert len(fields["gc.collections_by_gen"]) == 3

    def test_tracemalloc_is_tracing_is_bool(self) -> None:
        reg = ResourceRegistry()
        fields = reg.snapshot_fields()
        assert isinstance(fields["tracemalloc.is_tracing"], bool)

    def test_tracemalloc_returns_kb_when_tracing(self) -> None:
        reg = ResourceRegistry()
        try:
            tracemalloc.start(5)
            # Allocate some memory so peak is non-zero.
            _ = [object() for _ in range(1000)]
            fields = reg.snapshot_fields()
            assert fields["tracemalloc.is_tracing"] is True
            assert isinstance(fields["tracemalloc.current_kb"], int)
            assert isinstance(fields["tracemalloc.peak_kb"], int)
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()


# ── Module-level helper wrappers ──


class TestModuleHelpers:
    def test_register_onnx_session_module_helper(self) -> None:
        s = _FakeOnnxSession("via-helper")
        register_onnx_session(label="helper.test", session=s)
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["onnx.session_count"] == 1
        assert "helper.test" in fields["onnx.session_labels"]

    def test_register_lock_dict_module_helper(self) -> None:
        d = _FakeLockDict(size=4)
        register_lock_dict(owner_id="helper.lock", dict_ref=d)
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["lock_dict.total_cardinality"] == 4

    def test_record_to_thread_dispatch_module_helper(self) -> None:
        record_to_thread_dispatch(
            label="helper.dispatch",
            worker_count_at_dispatch=2,
            queue_depth=0,
            max_workers=8,
        )
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["to_thread.dispatch_count_total"] == 1

    def test_record_exception_cohort_module_helper(self) -> None:
        record_exception_cohort(
            group_id="helper-group",
            sub_exception_count=2,
            retained_bytes_estimate=2048,
        )
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["exception_cohort.cumulative_retained_bytes_since_start"] == 2048
        assert fields["exception_cohort.cumulative_distinct_group_id_count"] == 1
