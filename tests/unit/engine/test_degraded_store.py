"""Unit tests for :class:`sovyx.engine._degraded_store.EngineDegradedStore`.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.1 + §T1.13.

Coverage targets per §9.6: ≥95% line coverage on ``_degraded_store.py``
(threading.Lock semantics + cardinality eviction + upsert merge logic
+ axis/reason clear + singleton lifecycle).
"""

from __future__ import annotations

import threading
import time

import pytest

from sovyx.engine._degraded_store import (
    ActionChip,
    DegradedEntry,
    EngineDegradedStore,
    get_default_degraded_store,
    make_action_chip,
    reset_default_degraded_store,
)


def _make_entry(
    *,
    axis: str = "voice",
    reason: str = "failover_ladder_exhausted",
    severity: str = "error",
    monotonic_ts: float | None = None,
    metadata: dict[str, object] | None = None,
) -> DegradedEntry:
    ts = monotonic_ts if monotonic_ts is not None else time.monotonic()
    return DegradedEntry(
        axis=axis,
        reason=reason,
        severity=severity,
        title_token=f"degraded.{axis}.title",
        body_token=f"degraded.{axis}.body",
        action_chips=(make_action_chip("test.label", "navigate", "/test"),),
        metadata=metadata or {},
        first_observed_monotonic=ts,
        last_observed_monotonic=ts,
        occurrence_count=1,
    )


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Each test gets a fresh singleton — Mission C3 _failover_history pattern."""
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


class TestEngineDegradedStoreRecord:
    """``record()`` upsert + cardinality semantics."""

    def test_record_inserts_new_entry(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry())
        assert len(store) == 1

    def test_record_upsert_preserves_first_observed(self) -> None:
        """Upsert MUST preserve ``first_observed_monotonic`` so the
        operator can see how long the condition has been live."""
        store = EngineDegradedStore()
        first = _make_entry(monotonic_ts=10.0)
        store.record(first)
        second = _make_entry(monotonic_ts=20.0)
        store.record(second)
        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0].first_observed_monotonic == 10.0
        assert snap[0].last_observed_monotonic == 20.0
        assert snap[0].occurrence_count == 2

    def test_record_upsert_takes_latest_severity(self) -> None:
        """Latest severity wins on upsert — operator sees current state."""
        store = EngineDegradedStore()
        store.record(_make_entry(severity="warn"))
        store.record(_make_entry(severity="critical"))
        snap = store.snapshot()
        assert snap[0].severity == "critical"

    def test_record_distinct_reasons_keep_separate_entries(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(reason="a"))
        store.record(_make_entry(reason="b"))
        store.record(_make_entry(reason="c"))
        assert len(store) == 3

    def test_record_evicts_oldest_at_max_capacity(self) -> None:
        """Cardinality safeguard per anti-pattern #15. Should never
        trigger on real hardware but the test confirms determinism."""
        store = EngineDegradedStore()
        # Fill to exactly _MAX_ENTRIES
        for i in range(EngineDegradedStore._MAX_ENTRIES):
            store.record(_make_entry(reason=f"r{i}", monotonic_ts=float(i)))
        assert len(store) == EngineDegradedStore._MAX_ENTRIES
        # One more — evicts oldest (r0)
        store.record(_make_entry(reason="overflow", monotonic_ts=999.0))
        snap_reasons = {e.reason for e in store.snapshot()}
        assert "r0" not in snap_reasons
        assert "overflow" in snap_reasons
        assert len(store) == EngineDegradedStore._MAX_ENTRIES


class TestEngineDegradedStoreClear:
    """``clear_axis()`` + ``clear_reason()`` semantics."""

    def test_clear_axis_removes_all_entries_on_axis(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(axis="voice", reason="a"))
        store.record(_make_entry(axis="voice", reason="b"))
        store.record(_make_entry(axis="llm", reason="c"))
        removed = store.clear_axis("voice")
        assert removed == 2
        assert len(store) == 1
        assert store.snapshot()[0].axis == "llm"

    def test_clear_axis_returns_zero_when_axis_absent(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(axis="llm"))
        assert store.clear_axis("voice") == 0
        assert len(store) == 1

    def test_clear_reason_removes_single_entry(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(reason="a"))
        store.record(_make_entry(reason="b"))
        assert store.clear_reason("a") is True
        assert len(store) == 1

    def test_clear_reason_returns_false_when_absent(self) -> None:
        store = EngineDegradedStore()
        assert store.clear_reason("nonexistent") is False


class TestEngineDegradedStoreReadAccessors:
    """``snapshot()`` + ``distinct_axes()`` + ``__len__()``."""

    def test_snapshot_is_independent_copy(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(reason="a"))
        snap = store.snapshot()
        store.record(_make_entry(reason="b"))
        # First snapshot should NOT mutate
        assert len(snap) == 1
        # Fresh snapshot sees both
        assert len(store.snapshot()) == 2

    def test_distinct_axes_sorted_unique(self) -> None:
        store = EngineDegradedStore()
        store.record(_make_entry(axis="voice", reason="a"))
        store.record(_make_entry(axis="llm", reason="b"))
        store.record(_make_entry(axis="stt", reason="c"))
        store.record(_make_entry(axis="voice", reason="d"))
        assert store.distinct_axes() == ["llm", "stt", "voice"]

    def test_distinct_axes_empty_when_store_empty(self) -> None:
        assert EngineDegradedStore().distinct_axes() == []


class TestEngineDegradedStoreSingleton:
    """Module-level lazy singleton mirroring C3 _failover_history."""

    def test_get_default_returns_same_instance(self) -> None:
        a = get_default_degraded_store()
        b = get_default_degraded_store()
        assert a is b

    def test_reset_default_drops_singleton(self) -> None:
        a = get_default_degraded_store()
        reset_default_degraded_store()
        b = get_default_degraded_store()
        assert a is not b

    def test_singleton_records_visible_across_callers(self) -> None:
        get_default_degraded_store().record(_make_entry(reason="cross_caller"))
        snap = get_default_degraded_store().snapshot()
        assert any(e.reason == "cross_caller" for e in snap)


class TestEngineDegradedStoreThreadSafety:
    """Mission C4 §16 — concurrent writes MUST NOT corrupt state."""

    def test_concurrent_records_dont_lose_entries(self) -> None:
        store = EngineDegradedStore()
        n_threads = 4
        n_per_thread = 5

        def worker(tid: int) -> None:
            for i in range(n_per_thread):
                store.record(_make_entry(reason=f"t{tid}_r{i}"))

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Distinct reasons — no merge, all should be present.
        assert len(store) == n_threads * n_per_thread


class TestActionChipFactory:
    """``make_action_chip`` shim — kept tiny but covered."""

    def test_default_style(self) -> None:
        chip = make_action_chip("label.key", "navigate", "/route")
        assert chip.style == "default"
        assert chip.label_token == "label.key"
        assert chip.action == "navigate"
        assert chip.target == "/route"

    def test_explicit_style(self) -> None:
        chip = make_action_chip(
            "label.key",
            "external_link",
            "https://example.com",
            style="primary",
        )
        assert chip.style == "primary"

    def test_action_chip_is_frozen(self) -> None:
        chip = make_action_chip("k", "navigate", "/")
        # Frozen dataclass — attribute assignment should raise.
        with pytest.raises(Exception) as exc_info:
            chip.label_token = "mutated"  # type: ignore[misc]
        # FrozenInstanceError — xdist-safe per anti-pattern #8.
        assert type(exc_info.value).__name__ in {
            "FrozenInstanceError",
            "AttributeError",
        }


class TestActionChipPositional:
    """Constructor invariants."""

    def test_action_chip_round_trip_via_constructor(self) -> None:
        chip = ActionChip(
            label_token="x",
            action="dispatch",
            target="/api/x",
            style="danger",
        )
        assert chip.label_token == "x"
        assert chip.action == "dispatch"
        assert chip.target == "/api/x"
        assert chip.style == "danger"
