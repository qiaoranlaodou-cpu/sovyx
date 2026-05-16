"""Tests for ``sovyx.voice.health._failover_history``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.9.

Pin the ring-buffer invariants:

* Bounded FIFO via ``deque(maxlen=capacity)`` — oldest entry evicted
  on overflow.
* ``record_ladder`` appends a new run; ``update_in_progress`` finalises
  a matching record by ``ladder_id``.
* ``entries()`` returns newest-first snapshot.
* Lazy singleton + ``reset_default_failover_history`` for test
  isolation.
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._failover_history import (
    FailoverCandidateRecord,
    FailoverHistoryRing,
    FailoverLadderRunRecord,
    get_default_failover_history,
    make_ladder_id,
    reset_default_failover_history,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_failover_history()


def _record(*, ladder_id: str = "id-1", started: float = 1.0) -> FailoverLadderRunRecord:
    return FailoverLadderRunRecord(
        ladder_id=ladder_id,
        started_monotonic=started,
        from_endpoint="razer",
        mind_id="jonny",
    )


class TestRingBasic:
    def test_default_capacity_is_32(self) -> None:
        ring = FailoverHistoryRing()
        assert ring.capacity == 32  # noqa: PLR2004

    def test_explicit_capacity_honored(self) -> None:
        ring = FailoverHistoryRing(capacity=5)
        assert ring.capacity == 5

    def test_capacity_floor_one(self) -> None:
        """``capacity <= 0`` falls back to 1 (defensive)."""
        ring = FailoverHistoryRing(capacity=0)
        assert ring.capacity == 1

    def test_record_and_len(self) -> None:
        ring = FailoverHistoryRing(capacity=3)
        ring.record_ladder(_record(ladder_id="a"))
        ring.record_ladder(_record(ladder_id="b"))
        assert len(ring) == 2

    def test_entries_newest_first(self) -> None:
        ring = FailoverHistoryRing(capacity=3)
        ring.record_ladder(_record(ladder_id="a", started=1.0))
        ring.record_ladder(_record(ladder_id="b", started=2.0))
        ring.record_ladder(_record(ladder_id="c", started=3.0))
        entries = ring.entries()
        assert [e.ladder_id for e in entries] == ["c", "b", "a"]


class TestRingCapacity:
    def test_overflow_evicts_oldest(self) -> None:
        ring = FailoverHistoryRing(capacity=3)
        ring.record_ladder(_record(ladder_id="a"))
        ring.record_ladder(_record(ladder_id="b"))
        ring.record_ladder(_record(ladder_id="c"))
        ring.record_ladder(_record(ladder_id="d"))
        entries = ring.entries()
        # 'a' was evicted; the most recent 3 remain.
        assert [e.ladder_id for e in entries] == ["d", "c", "b"]


class TestUpdateInProgress:
    def test_finalize_known_ladder_id(self) -> None:
        ring = FailoverHistoryRing(capacity=5)
        ring.record_ladder(_record(ladder_id="in-flight"))
        ok = ring.update_in_progress(
            "in-flight",
            verdict="succeeded",
            completed_monotonic=2.0,
            succeeded_index=0,
            candidates_tried=1,
            elapsed_ms=1000,
        )
        assert ok is True
        entry = ring.entries()[0]
        assert entry.verdict == "succeeded"
        assert entry.succeeded_index == 0
        assert entry.candidates_tried == 1
        assert entry.elapsed_ms == 1000  # noqa: PLR2004
        assert entry.completed_monotonic == 2.0  # noqa: PLR2004

    def test_finalize_unknown_ladder_id_returns_false(self) -> None:
        ring = FailoverHistoryRing(capacity=5)
        ring.record_ladder(_record(ladder_id="real"))
        ok = ring.update_in_progress(
            "nonexistent",
            verdict="succeeded",
            completed_monotonic=2.0,
            succeeded_index=0,
            candidates_tried=1,
            elapsed_ms=1000,
        )
        assert ok is False


class TestCandidateRecord:
    def test_add_candidate_appends(self) -> None:
        run = _record()
        run.add_candidate(
            FailoverCandidateRecord(
                index=0,
                target_endpoint="dev_a",
                verdict="failed",
                error_class="unopenable_this_boot",
            ),
        )
        run.add_candidate(
            FailoverCandidateRecord(
                index=1,
                target_endpoint="dev_b",
                verdict="succeeded",
            ),
        )
        assert len(run.candidates) == 2
        assert run.candidates[0].verdict == "failed"
        assert run.candidates[1].verdict == "succeeded"


class TestSingleton:
    def test_get_returns_same_instance(self) -> None:
        a = get_default_failover_history()
        b = get_default_failover_history()
        assert a is b

    def test_reset_drops_singleton(self) -> None:
        a = get_default_failover_history()
        reset_default_failover_history()
        b = get_default_failover_history()
        assert a is not b

    def test_make_ladder_id_returns_12_char_hex(self) -> None:
        id1 = make_ladder_id()
        id2 = make_ladder_id()
        assert len(id1) == 12  # noqa: PLR2004
        assert len(id2) == 12  # noqa: PLR2004
        assert id1 != id2
        # Hex-only.
        assert all(c in "0123456789abcdef" for c in id1)
