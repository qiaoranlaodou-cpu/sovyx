"""F2 — Mission H4 §T2.1 ResourceSnapshotter extension verification.

Verifies that the snapshot payload emitted by ``_emit_snapshot`` after
the Phase 1.B wire-up carries:

1. All 22 new H4 fields (per-cohort registry metrics block).
2. The dual-emit of ``system.rss_bytes`` alongside ``process.rss_bytes``
   during the LENIENT calibration window (drops at v0.54.0 STRICT).
3. The legacy 10 pre-mission fields preserved.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T2.1 + §3 F2.

Approach: patch the snapshotter's logger.info attribute directly and
capture kwargs. structlog's pipeline routes through its own logger
which doesn't always surface via pytest's caplog without extra config;
capturing the logger.info call directly is more deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import sovyx.observability.resources as resources_mod
from sovyx.observability._resource_registry import (
    register_lock_dict,
    register_onnx_session,
    reset_default_resource_registry,
)
from sovyx.observability.resources import ResourceSnapshotter

_H4_NEW_FIELDS: frozenset[str] = frozenset(
    {
        # Spec §3 F2 canonical 22-field list (v0.49.31 closure adds the
        # final 5 fields that v0.49.14..v0.49.30 had silently missed).
        # MISSION-A.1.P3.b (F-007, ADR-D16): canonicals renamed to
        # ``_at_last_dispatch``; legacy keys LENIENT-emitted by the
        # snapshotter (sunset v0.55.0).
        "to_thread.pool_size_at_last_dispatch",
        "to_thread.queue_depth_at_last_dispatch",
        "to_thread.max_workers_at_last_dispatch",
        "to_thread.pool_size",  # LENIENT legacy shim, sunset v0.55.0
        "to_thread.queue_depth",  # LENIENT legacy shim, sunset v0.55.0
        "to_thread.max_workers",  # LENIENT legacy shim, sunset v0.55.0
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
        # MISSION-A.1.P2 (F-002+F-003): cumulative-vs-window split.
        # Legacy keys remain LENIENT-emitted by the snapshotter for
        # dashboards keyed on the old labels (sunset v0.55.0, ADR-D14,
        # anti-pattern #49).
        "exception_cohort.cumulative_retained_bytes_since_start",
        "exception_cohort.cumulative_distinct_group_id_count",
        "exception_cohort.window_retained_bytes",
        "exception_cohort.window_distinct_group_id_count",
        "exception_cohort.last_observation_monotonic",
        "exception_cohort.retained_bytes_estimate",  # LENIENT legacy shim
        "exception_cohort.distinct_group_id_count",  # LENIENT legacy shim
        # MISSION-A.1.P3 (F-005+F-006, ADR-D15, anti-patterns #48 + #50):
        # ``to_thread.active_workers`` and ``asyncio.current_running_task_name``
        # were semantic lies — both removed from SSoT canonical entries.
        # The legacy keys remain LENIENT-emitted by the snapshotter as
        # shims (sunset v0.55.0). ``asyncio.all_task_names`` replaces
        # current_running_task_name with an operator-correct list.
        "asyncio.all_task_names",
        # v0.49.31 — Spec §0 item 4 + §T2.1 + §3 F2 extension fields.
        "process.memory_percent",
        "process.cpu_times_user_s",
        "process.cpu_times_system_s",
        "asyncio.default_executor_state",
        # MISSION-A.2.P4 F-012: triple-None disambiguation.
        "process.open_files_status",
        "process.connections_status",
    },
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


@pytest.fixture()
def snapshotter() -> ResourceSnapshotter:
    config = MagicMock()
    config.sampling.perf_hotpath_interval_seconds = 60
    return ResourceSnapshotter(config)


class _FakeSession:
    """ONNX-session stand-in supporting weakref."""


class _FakeLockDict:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


def _capture_emit(snapshotter: ResourceSnapshotter) -> dict[str, object]:
    """Trigger _emit_snapshot and return the logger.info kwargs payload."""
    with patch.object(resources_mod.logger, "info") as info, patch("psutil.Process"):
        snapshotter._emit_snapshot(final=False)
    # Find the self.health.snapshot call (other info calls may have fired).
    snapshot_calls = [
        c for c in info.call_args_list if c.args and c.args[0] == "self.health.snapshot"
    ]
    assert snapshot_calls, "logger.info('self.health.snapshot', ...) MUST be called"
    return snapshot_calls[-1].kwargs


class TestSnapshotPayloadH4Extension:
    """The post-Phase-1.B payload carries every H4 field."""

    def test_h4_new_fields_present(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        for field in _H4_NEW_FIELDS:
            assert field in payload, (
                f"Mission H4 §F2: snapshot payload MUST carry H4 new field "
                f"'{field}'; got payload keys {sorted(payload)}"
            )

    def test_legacy_system_rss_bytes_alias_dual_emitted(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        """ADR-D9 dual-emit: ``system.rss_bytes`` carries the same int as ``process.rss_bytes``."""
        with (
            patch.object(resources_mod.logger, "info") as info,
            patch("sovyx.observability.resources._capture_psutil_metrics") as cap,
        ):
            cap.return_value = {
                "process.rss_bytes": 1_234_567_890,
                "process.vms_bytes": None,
                "process.cpu_percent": 0.0,
                "process.num_threads": 4,
                "process.num_handles_or_fds": 50,
                "process.open_files_count": 10,
                "process.connections_count": 5,
                # v0.49.31 — psutil extension fields per spec §0 item 4.
                "process.memory_percent": 12.5,
                "process.cpu_times_user_s": 1.23,
                "process.cpu_times_system_s": 0.45,
            }
            snapshotter._emit_snapshot(final=False)
        snapshot_calls = [
            c for c in info.call_args_list if c.args and c.args[0] == "self.health.snapshot"
        ]
        assert snapshot_calls
        payload = snapshot_calls[-1].kwargs
        assert payload.get("process.rss_bytes") == 1_234_567_890
        assert payload.get("system.rss_bytes") == 1_234_567_890

    def test_onnx_session_count_reflects_registered(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        s1 = _FakeSession()
        s2 = _FakeSession()
        register_onnx_session(label="x", session=s1)
        register_onnx_session(label="y", session=s2)
        payload = _capture_emit(snapshotter)
        assert payload["onnx.session_count"] == 2
        assert set(payload["onnx.session_labels"]) == {"x", "y"}

    def test_lock_dict_cardinality_reflects_registered(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        d = _FakeLockDict(size=7)
        register_lock_dict(owner_id="abc", dict_ref=d)
        payload = _capture_emit(snapshotter)
        assert payload["lock_dict.total_cardinality"] == 7
        assert payload["lock_dict.per_owner"] == {"abc": 7}
        assert payload["lock_dict.instance_count"] == 1


class TestSpecF2CanonicalFieldList:
    """v0.49.31 — Mission H4 §3 F2 canonical 22-H4-field list closure.

    The 12th `feedback_no_inventing_specs` audit-discipline cycle
    (2026-05-20) caught that 5 fields canonically listed in §3 F2 were
    NOT emitted by v0.49.14..v0.49.30. This class asserts each of the 5
    appears in the snapshot payload at HEAD, preventing future regression.
    """

    def test_process_memory_percent_emitted(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        assert "process.memory_percent" in payload, (
            "Mission H4 §3 F2 + §0 item 4: snapshot MUST carry process.memory_percent"
        )

    def test_process_cpu_times_user_s_emitted(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        assert "process.cpu_times_user_s" in payload

    def test_process_cpu_times_system_s_emitted(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        assert "process.cpu_times_system_s" in payload

    def test_asyncio_current_running_task_name_emitted(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        payload = _capture_emit(snapshotter)
        assert "asyncio.current_running_task_name" in payload

    def test_asyncio_default_executor_state_is_a_dict_with_three_keys(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        payload = _capture_emit(snapshotter)
        state = payload.get("asyncio.default_executor_state")
        assert isinstance(state, dict), (
            f"asyncio.default_executor_state MUST be a dict; got {type(state).__name__}"
        )
        assert set(state) == {"pool_size", "queue_depth", "max_workers"}

    def test_to_thread_active_workers_lenient_shim_emitted(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        """MISSION-A.1.P3 F-006 (ADR-D15): legacy LENIENT shim is emitted.

        Pre-A.1.P3 the alias-trap test asserted
        ``payload["active_workers"] == payload["pool_size"]`` as a
        contract — encoding the semantic lie ("active workers really
        equals pool size") into the test suite. A future fix that
        distinguishes "active" from "pool" would have FAILED THE TEST
        rather than fixing the lie (anti-pattern #48 — falsifiability
        gates verify literal-string parity, not semantic correctness).
        This commit drops the equality assertion. The legacy key
        remains LENIENT-emitted by the snapshotter for one minor cycle
        for backward compat (sunset v0.55.0); the testing surface no
        longer encodes the alias as truth.
        """
        payload = _capture_emit(snapshotter)
        assert "to_thread.active_workers" in payload, (
            "LENIENT shim MUST emit the legacy key during the dual-emit "
            "window; sunset v0.55.0 will remove this assertion."
        )

    def test_v0_49_31_ssot_count_matches_spec_target(self) -> None:
        """SSoT count tracks the canonical field set.

        Pre-MISSION-A.1 breakdown (34):
        * 10 pre-mission fields (7 process.* + 3 asyncio.*)
        * 18 H4 v0.49.14..v0.49.30 fields (to_thread × 5 incl. queue_depth +
          lock_dict × 3 + onnx × 2 + gc × 2 + tracemalloc × 3 + exception_cohort × 3)
        * 6 v0.49.31 closure fields per spec §3 F2 + §0 item 4:
          process.memory_percent + process.cpu_times_user_s +
          process.cpu_times_system_s + asyncio.current_running_task_name +
          asyncio.default_executor_state + to_thread.active_workers

        MISSION-A.1.P2 delta (F-002+F-003): the pre-fix
        ``exception_cohort.retained_bytes_estimate`` and
        ``exception_cohort.distinct_group_id_count`` SSoT entries split
        into FOUR explicit fields — ``cumulative_*`` (lifetime
        accumulators) + ``window_*`` (rolling-window values). The two
        legacy keys remain LENIENT-emitted by the snapshotter as legacy
        shims (sunset v0.55.0, ADR-D14) but are NOT separate SSoT entries
        — they appear as ``legacy_alias=`` attributes on the cumulative
        FieldSpecs (precedent: ``system.rss_bytes`` is the legacy alias
        of ``process.rss_bytes`` without its own SSoT entry).

        Net delta: 34 + 2 (4 new − 2 old) = 36.

        MISSION-A.1.P3 delta (F-005+F-006): two semantic-lie fields
        removed from SSoT canonical entries — ``to_thread.active_workers``
        (literal alias of pool_size) and ``asyncio.current_running_task_name``
        (observation paradox — always returned the snapshotter task name).
        Replacement: ``asyncio.all_task_names`` (operator-correct list of
        not-done task names, capped at 64). Legacy keys remain
        LENIENT-emitted by the snapshotter and declared as
        ``legacy_alias=`` on ``to_thread.pool_size`` and
        ``asyncio.all_task_names`` respectively (ADR-D15).

        Net delta: 36 + 1 (1 new − 2 old) = 35.

        MISSION-A.1.P3.b delta (F-007+F-014): five canonical renames.
        F-007 renames the three twin-named stale ``to_thread.*`` fields
        to ``_at_last_dispatch`` (anti-pattern #51). F-014 renames the
        two ``asyncio.{running,pending}_count`` fields to
        ``not_done_count`` / ``awaiting_count`` (anti-pattern #51 same
        class). All 5 legacy keys LENIENT-emitted by the snapshotter
        and declared as ``legacy_alias=`` on the new canonicals
        (ADR-D16).

        Net delta: 35 + 0 (5 new − 5 demoted to shim) = 35.

        MISSION-A.2.P4 delta (F-012): triple-None disambiguation adds
        ``process.open_files_status`` + ``process.connections_status``
        as two new canonical typed-status fields. Net delta: +2 → 37.
        """
        from sovyx.observability._resource_registry import _HEALTH_SNAPSHOT_FIELDS

        assert len(_HEALTH_SNAPSHOT_FIELDS) == 37
