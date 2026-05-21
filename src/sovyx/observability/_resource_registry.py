"""Mission H4 SSoT — resource-cohort instrumentation registry.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T1.1-T1.3.

This module is the **single source of truth** for every per-cohort
cardinality field emitted on ``self.health.snapshot``. The producer
(``sovyx.observability.resources.ResourceSnapshotter``) consults
:func:`get_default_resource_registry()`; the consumer
(``sovyx.observability.anomaly.AnomalyProcessor`` + the future
``sovyx.observability._resource_cohort_governor``) keys on the canonical
field names declared in :data:`_HEALTH_SNAPSHOT_FIELDS`. Quality Gate 15
AST-scans both sides for name parity.

Owned objects:

* :data:`_HEALTH_SNAPSHOT_FIELDS` — SSoT mapping of every canonical
  ``self.health.snapshot`` field key → :class:`FieldSpec` carrying
  producer/consumer module paths, type constraint, legacy alias (for
  the ADR-D9 dual-emit window), operator-hint key, and dashboard
  section.
* :class:`ResourceRegistry` — process-local, lifetime-spanning registry
  of ONNX :class:`InferenceSession`s (weakref-tracked),
  :class:`LRULockDict` instances (weakref-tracked), and async
  ``to_thread`` dispatch counters.
* :class:`CohortAxis` — :class:`StrEnum` of the 5 cohort verdicts
  consumed by :class:`ResourceCohortGovernor` (Phase 1.D).
* :func:`get_default_resource_registry` — module-level lazy singleton.
* :func:`reset_default_resource_registry` — test-isolation reset.

Anti-pattern compliance:

* #9 — :class:`CohortAxis` is a :class:`StrEnum` (xdist-safe).
* #14 — :class:`ResourceRegistry`'s thread-safety boundary lets workers
  spawned via ``dispatch_to_thread`` record dispatch counts safely.
* #15 — every :class:`LRULockDict` construction site MUST call
  :func:`register_lock_dict` so cardinality is observable (Phase 1.B
  wires the 9 known sites; Quality Gate 15 enforces).
* #16 — leaf module; no internal contract dependencies.
* #20 — public surface re-exported via
  :mod:`sovyx.observability.__init__`; tests patch via
  ``patch.object(_resource_registry, "get_default_resource_registry")``.
* #34 — registry is lazy-initialized; bootstrap creates the singleton
  before :class:`ResourceSnapshotter`.
* #42 — composite-store consumer reads :meth:`snapshot_cohort_state`
  for ``axis="engine_resources"`` emission (Phase 1.D).

Public surface:

* :class:`FieldSpec`, :class:`CohortAxis`, :class:`ResourceRegistry`.
* :data:`_HEALTH_SNAPSHOT_FIELDS`.
* :func:`get_default_resource_registry`,
  :func:`reset_default_resource_registry`,
  :func:`register_onnx_session`, :func:`register_lock_dict`,
  :func:`record_to_thread_dispatch`, :func:`record_exception_cohort`.
"""

from __future__ import annotations

import gc
import threading
import time
import tracemalloc
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any, Final

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)


# ── Cohort axes (consumed by Phase 1.D ResourceCohortGovernor) ──


@unique
class CohortAxis(StrEnum):
    """Five canonical resource-cohort verdicts.

    The :class:`ResourceCohortGovernor` (Phase 1.D) evaluates each
    cohort independently against its budget; multiple cohorts MAY fire
    on the same tick (composite severity escalation handled by the
    C4 :class:`EngineDegradedStore` per ADR-D6).

    Members:
        RSS_GROWTH: process resident set size grew Δ > budget within
            ``cohort_window_s``. Cure: heap-snapshot trigger; restart
            daemon; disable a feature flag suspected of leaking.
        THREAD_COUNT: process thread count grew Δ > budget within the
            same window. Cure: thread-snapshot trigger; review
            ``dispatch_to_thread`` workload distribution.
        LOCK_DICT_CARDINALITY: aggregate ``LRULockDict`` cardinality
            crossed soft cap. Cure: bump ``maxsize`` on the saturated
            instance OR audit the eviction rate.
        ONNX_SESSION: count of registered ONNX :class:`InferenceSession`s
            exceeds the expected-by-feature-flags total. Cure: audit
            session lifetimes; expect one per
            ``{VAD,STT,wake_word,TTS,brain_embedding}`` × per-mind
            (single-mind GA expects 4-5 sessions total).
        EXCEPTION_COHORT: retained-bytes-estimate across recently
            observed :class:`ExceptionGroup`s crossed cap. Cure:
            review the recent 500 storm; reset coordinator state.
    """

    RSS_GROWTH = "rss_growth"
    THREAD_COUNT = "thread_count"
    LOCK_DICT_CARDINALITY = "lock_dict_cardinality"
    ONNX_SESSION = "onnx_session"
    EXCEPTION_COHORT = "exception_cohort"


# ── Field specs (SSoT for snapshot field-name parity) ──


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One snapshot field's producer/consumer contract.

    Attributes:
        canonical_key: The literal key emitted on ``self.health.snapshot``
            and read by every consumer. Quality Gate 15 AST-scans for
            literal-string matches against this set.
        type_constraint: Expected runtime type. ``int`` for counters,
            ``float`` for monotonic timestamps, ``str`` for labels,
            ``list`` for ordered sequences, ``dict`` for keyed maps.
        producer_module: Dotted module path of the canonical emitter.
            A producer outside this path emitting the field is a
            violation (e.g. `voice/foo.py` cannot emit
            ``"process.rss_bytes"`` — only
            ``sovyx.observability.resources`` may).
        consumer_modules: Dotted module paths permitted to read this
            field. Empty tuple means "field is exported but no
            in-tree consumer yet" (e.g. external Grafana dashboards).
        legacy_alias: Pre-mission key the producer dual-emits during the
            LENIENT calibration window. STRICT (Phase 3 v0.54.0) drops
            the alias. ``None`` for fields without legacy.
        operator_hint_key: Key into ``_REMEDIATION_BY_FIELD`` (Phase 1.C
            ``_resource_remediation.py``) for the ``sovyx doctor
            resources --explain <field>`` render. ``None`` for purely
            developer-informational fields.
        section: Dashboard ``<ResourceHealthSection>`` collapsible-row
            grouping. One of ``"process"`` / ``"asyncio"`` /
            ``"to_thread"`` / ``"lock_dict"`` / ``"onnx"`` / ``"gc"`` /
            ``"tracemalloc"`` / ``"exception_cohort"``.
    """

    canonical_key: str
    type_constraint: type | tuple[type, ...]
    producer_module: str
    consumer_modules: tuple[str, ...] = ()
    legacy_alias: str | None = None
    operator_hint_key: str | None = None
    section: str = "process"


# ── _HEALTH_SNAPSHOT_FIELDS — the canonical SSoT mapping ──
#
# Every field emitted by ``ResourceSnapshotter._emit_snapshot`` MUST be
# here. Every consumer reading a literal snapshot key (e.g.
# ``event_dict.get("process.rss_bytes")``) MUST reference a key in this
# map. Quality Gate 15 AST-scans both sides.

_HEALTH_SNAPSHOT_FIELDS: Final[Mapping[str, FieldSpec]] = {
    # ── psutil block (pre-H4, name-parity audited) ──
    "process.rss_bytes": FieldSpec(
        canonical_key="process.rss_bytes",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=(
            "sovyx.observability.anomaly",
            "sovyx.observability._resource_cohort_governor",
        ),
        legacy_alias="system.rss_bytes",
        operator_hint_key="process_rss_bytes",
        section="process",
    ),
    "process.vms_bytes": FieldSpec(
        canonical_key="process.vms_bytes",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_vms_bytes",
        section="process",
    ),
    "process.cpu_percent": FieldSpec(
        canonical_key="process.cpu_percent",
        type_constraint=(int, float),
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_cpu_percent",
        section="process",
    ),
    "process.num_threads": FieldSpec(
        canonical_key="process.num_threads",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=("sovyx.observability._resource_cohort_governor",),
        operator_hint_key="process_num_threads",
        section="process",
    ),
    "process.num_handles_or_fds": FieldSpec(
        canonical_key="process.num_handles_or_fds",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_num_handles_or_fds",
        section="process",
    ),
    "process.open_files_count": FieldSpec(
        canonical_key="process.open_files_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_open_files_count",
        section="process",
    ),
    # MISSION-A.2.P4 F-012: triple-None disambiguation. Pre-fix the
    # ``open_files_count`` / ``connections_count`` ``None`` value
    # collapsed three distinct conditions (shutdown-skip / permission-
    # denied / psutil-missing). The parallel ``_status`` field carries
    # one of ``ok`` / ``skipped_shutdown`` / ``denied`` / ``unsupported``
    # / ``psutil_missing`` so downstream consumers can disambiguate.
    "process.open_files_status": FieldSpec(
        canonical_key="process.open_files_status",
        type_constraint=str,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_open_files_status",
        section="process",
    ),
    "process.connections_count": FieldSpec(
        canonical_key="process.connections_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_connections_count",
        section="process",
    ),
    "process.connections_status": FieldSpec(
        canonical_key="process.connections_status",
        type_constraint=str,
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_connections_status",
        section="process",
    ),
    # ── H4 new fields: process block extension (Mission H4 §0 item 4 + §T2.1 + §3 F2). ──
    # ``_capture_psutil_metrics`` extends from 7 → 9 fields per spec.
    "process.memory_percent": FieldSpec(
        canonical_key="process.memory_percent",
        type_constraint=(int, float),
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_memory_percent",
        section="process",
    ),
    "process.cpu_times_user_s": FieldSpec(
        canonical_key="process.cpu_times_user_s",
        type_constraint=(int, float),
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_cpu_times_user_s",
        section="process",
    ),
    "process.cpu_times_system_s": FieldSpec(
        canonical_key="process.cpu_times_system_s",
        type_constraint=(int, float),
        producer_module="sovyx.observability.resources",
        operator_hint_key="process_cpu_times_system_s",
        section="process",
    ),
    # ── asyncio block (pre-H4) ──
    "asyncio.task_count": FieldSpec(
        canonical_key="asyncio.task_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="asyncio_task_count",
        section="asyncio",
    ),
    # MISSION-A.1.P3.b F-014 (anti-pattern #51, ADR-D16):
    # ``asyncio.running_count`` counted "not done" — including tasks blocked
    # on ``await asyncio.sleep`` or any await; NOT a measure of tasks actively
    # executing on the loop step. ``pending_count`` was ``running_count - 1``
    # (excluded the snapshotter task via ``_is_currently_running``). The field
    # names promised executor-step semantics; the math delivered "not done"
    # semantics. Renames make the math explicit. Legacy keys remain
    # LENIENT-emitted by the snapshotter (sunset v0.55.0).
    "asyncio.not_done_count": FieldSpec(
        canonical_key="asyncio.not_done_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="asyncio.running_count",
        operator_hint_key="asyncio_not_done_count",
        section="asyncio",
    ),
    "asyncio.awaiting_count": FieldSpec(
        canonical_key="asyncio.awaiting_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="asyncio.pending_count",
        operator_hint_key="asyncio_awaiting_count",
        section="asyncio",
    ),
    # ── H4 new fields: asyncio block extension (Mission H4 §0 item 4 + §T2.1 + §3 F2). ──
    # ``_capture_asyncio_metrics`` extends from 3 → 5 fields per spec.
    #
    # MISSION-A.1.P3 F-005 (anti-pattern #50, ADR-D15):
    # pre-fix ``asyncio.current_running_task_name`` always returned the
    # SNAPSHOTTER task name (observation paradox — from inside the
    # snapshotter coroutine, ``asyncio.current_task()`` IS the
    # snapshotter). The field's promised value ("correlate snapshot to
    # the running coroutine") was never delivered. ``asyncio.all_task_names``
    # replaces it with a list of every not-done task's name (capped at 64).
    # The legacy key remains LENIENT-emitted by the snapshotter (alongside
    # the new field) for one minor cycle; sunset v0.55.0.
    "asyncio.all_task_names": FieldSpec(
        canonical_key="asyncio.all_task_names",
        type_constraint=list,
        producer_module="sovyx.observability.resources",
        legacy_alias="asyncio.current_running_task_name",
        operator_hint_key="asyncio_all_task_names",
        section="asyncio",
    ),
    "asyncio.default_executor_state": FieldSpec(
        canonical_key="asyncio.default_executor_state",
        type_constraint=dict,
        producer_module="sovyx.observability.resources",
        operator_hint_key="asyncio_default_executor_state",
        section="asyncio",
    ),
    # ── H4 new fields: to_thread block ──
    #
    # MISSION-A.1.P3 F-006 (anti-pattern #48, ADR-D15 supersedes ADR-D4):
    # ``to_thread.active_workers`` was a literal alias of pool_size — see
    # ADR-D15 commit `2ebc1c13`. The legacy key is LENIENT-emitted by the
    # snapshotter; sunset v0.55.0.
    #
    # MISSION-A.1.P3.b F-007 (anti-pattern #51, ADR-D16): the three
    # ``to_thread.{pool_size, max_workers, queue_depth}`` fields are
    # STALE — recorded at last dispatch (``_to_thread.last_*``) rather
    # than read live at snapshot time. They twin-named the LIVE
    # ``asyncio.default_executor_state.{pool_size, queue_depth,
    # max_workers}`` fields without disclosing the freshness divergence.
    # Renamed to ``_at_last_dispatch`` to make staleness explicit.
    # Legacy keys (``pool_size``, ``max_workers``, ``queue_depth``)
    # remain LENIENT-emitted by the snapshotter (sunset v0.55.0).
    #
    # ``to_thread.active_workers`` (F-006 retirement) remains an SSoT-
    # orphan shim emitted by the snapshotter — its lineage was
    # active_workers → pool_size (P3 declaration) → pool_size_at_last_dispatch
    # (P3.b rename). The SSoT only tracks one legacy_alias hop per
    # canonical; the multi-hop chain is documented in ADR-D16.
    "to_thread.pool_size_at_last_dispatch": FieldSpec(
        canonical_key="to_thread.pool_size_at_last_dispatch",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=("sovyx.observability._resource_cohort_governor",),
        legacy_alias="to_thread.pool_size",
        operator_hint_key="to_thread_pool_size_at_last_dispatch",
        section="to_thread",
    ),
    "to_thread.max_workers_at_last_dispatch": FieldSpec(
        canonical_key="to_thread.max_workers_at_last_dispatch",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="to_thread.max_workers",
        operator_hint_key="to_thread_max_workers_at_last_dispatch",
        section="to_thread",
    ),
    "to_thread.queue_depth_at_last_dispatch": FieldSpec(
        canonical_key="to_thread.queue_depth_at_last_dispatch",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="to_thread.queue_depth",
        operator_hint_key="to_thread_queue_depth_at_last_dispatch",
        section="to_thread",
    ),
    "to_thread.dispatch_count_total": FieldSpec(
        canonical_key="to_thread.dispatch_count_total",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="to_thread_dispatch_count_total",
        section="to_thread",
    ),
    "to_thread.dispatch_count_per_label": FieldSpec(
        canonical_key="to_thread.dispatch_count_per_label",
        type_constraint=dict,
        producer_module="sovyx.observability.resources",
        operator_hint_key="to_thread_dispatch_count_per_label",
        section="to_thread",
    ),
    # ── H4 new fields: lock_dict block ──
    "lock_dict.total_cardinality": FieldSpec(
        canonical_key="lock_dict.total_cardinality",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=("sovyx.observability._resource_cohort_governor",),
        operator_hint_key="lock_dict_total_cardinality",
        section="lock_dict",
    ),
    "lock_dict.per_owner": FieldSpec(
        canonical_key="lock_dict.per_owner",
        type_constraint=dict,
        producer_module="sovyx.observability.resources",
        operator_hint_key="lock_dict_per_owner",
        section="lock_dict",
    ),
    "lock_dict.instance_count": FieldSpec(
        canonical_key="lock_dict.instance_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="lock_dict_instance_count",
        section="lock_dict",
    ),
    # ── H4 new fields: onnx block ──
    "onnx.session_count": FieldSpec(
        canonical_key="onnx.session_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=("sovyx.observability._resource_cohort_governor",),
        operator_hint_key="onnx_session_count",
        section="onnx",
    ),
    "onnx.session_labels": FieldSpec(
        canonical_key="onnx.session_labels",
        type_constraint=list,
        producer_module="sovyx.observability.resources",
        operator_hint_key="onnx_session_labels",
        section="onnx",
    ),
    # ── H4 new fields: gc block ──
    "gc.collections_by_gen": FieldSpec(
        canonical_key="gc.collections_by_gen",
        type_constraint=list,
        producer_module="sovyx.observability.resources",
        operator_hint_key="gc_collections_by_gen",
        section="gc",
    ),
    "gc.objects_count": FieldSpec(
        canonical_key="gc.objects_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="gc_objects_count",
        section="gc",
    ),
    # ── H4 new fields: tracemalloc block ──
    "tracemalloc.is_tracing": FieldSpec(
        canonical_key="tracemalloc.is_tracing",
        type_constraint=bool,
        producer_module="sovyx.observability.resources",
        operator_hint_key="tracemalloc_is_tracing",
        section="tracemalloc",
    ),
    "tracemalloc.current_kb": FieldSpec(
        canonical_key="tracemalloc.current_kb",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="tracemalloc_current_kb",
        section="tracemalloc",
    ),
    "tracemalloc.peak_kb": FieldSpec(
        canonical_key="tracemalloc.peak_kb",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="tracemalloc_peak_kb",
        section="tracemalloc",
    ),
    # ── H4 new fields: exception_cohort block ──
    #
    # MISSION-A.1 F-002+F-003 (anti-pattern #49): each of these split
    # into a CUMULATIVE (lifetime, since process start; never decays)
    # and a WINDOW (rolling, sized by ``tuning.exception_cohort_window_s``;
    # decays as observations age out of the deque) variant. The pre-fix
    # ``retained_bytes_estimate`` / ``distinct_group_id_count`` names
    # implied current-window semantics but the implementation was
    # monotonic ``+=``; the governor compared a lifetime sum against a
    # real-time cap and the cohort verdict became permanently breached
    # after a single storm. The legacy keys remain LENIENT-emitted with
    # ``legacy_alias`` pointing to the new cumulative canonical names so
    # operator dashboards keyed on the old labels keep functioning; the
    # STRICT-flip drops the legacy keys at v0.55.0 (V-A1-2). ADR-D14.
    "exception_cohort.cumulative_retained_bytes_since_start": FieldSpec(
        canonical_key="exception_cohort.cumulative_retained_bytes_since_start",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="exception_cohort.retained_bytes_estimate",
        operator_hint_key="exception_cohort_cumulative_retained_bytes_since_start",
        section="exception_cohort",
    ),
    "exception_cohort.cumulative_distinct_group_id_count": FieldSpec(
        canonical_key="exception_cohort.cumulative_distinct_group_id_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        legacy_alias="exception_cohort.distinct_group_id_count",
        operator_hint_key="exception_cohort_cumulative_distinct_group_id_count",
        section="exception_cohort",
    ),
    "exception_cohort.window_retained_bytes": FieldSpec(
        canonical_key="exception_cohort.window_retained_bytes",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        consumer_modules=("sovyx.observability._resource_cohort_governor",),
        operator_hint_key="exception_cohort_window_retained_bytes",
        section="exception_cohort",
    ),
    "exception_cohort.window_distinct_group_id_count": FieldSpec(
        canonical_key="exception_cohort.window_distinct_group_id_count",
        type_constraint=int,
        producer_module="sovyx.observability.resources",
        operator_hint_key="exception_cohort_window_distinct_group_id_count",
        section="exception_cohort",
    ),
    "exception_cohort.last_observation_monotonic": FieldSpec(
        canonical_key="exception_cohort.last_observation_monotonic",
        type_constraint=(int, float),
        producer_module="sovyx.observability.resources",
        operator_hint_key="exception_cohort_last_observation_monotonic",
        section="exception_cohort",
    ),
}


# ── Internal counter dataclasses ──


@dataclass(slots=True)
class _ToThreadCounter:
    """Mutable per-process counter for ``dispatch_to_thread`` calls.

    Cardinality bounded by ``_MAX_TRACKED_LABELS`` to prevent a
    runaway-label-generator from inflating the per-label map. When the
    cap is reached, new labels are coalesced into the synthetic
    ``"_overflow_"`` bucket and a one-time WARN fires.
    """

    dispatch_count_total: int = 0
    dispatch_count_per_label: dict[str, int] = field(default_factory=dict)
    last_worker_count: int = 0
    last_queue_depth: int = 0
    last_max_workers: int = 0
    _overflow_warned: bool = False


_MAX_TRACKED_LABELS: Final[int] = 128


_DEFAULT_EXCEPTION_COHORT_OBSERVATIONS_MAXLEN: Final[int] = 128

# Mission B B-P1-07 (B.1.P2 closure 2026-05-21) — `distinct_group_ids`
# bounded via LRU. The bound is a multiple of the observations deque
# maxlen because distinct group_ids are typically dwarfed by total
# observations during a storm (same class re-raised many times). 4×
# gives 512 distinct types at default maxlen=128, which covers the
# realistic upper bound for a single daemon's exception-class diversity.
_DISTINCT_GROUP_IDS_LRU_MULTIPLIER: Final[int] = 4

# Mission B B-P2-12 (B.1.P2 closure 2026-05-21) — dedup window for
# duplicate observations of the SAME group_id. With the helper's
# 1-second monotonic-rounded group_id construction (see
# `_exception_cohort_record_helper.py`), most chained-walk duplicates
# arrive within the same 1.0s. Within this window the registry treats
# repeat observations as the SAME exception being chained-walked
# multiple times (do not double-count bytes); outside the window the
# same exception_type firing N times is genuine recurrence (count
# each).
_EXCEPTION_COHORT_DEDUP_WINDOW_S: Final[float] = 1.0


@dataclass(slots=True)
class _ExceptionCohortCounter:
    """Mutable retained-bytes estimate for recent ExceptionGroups.

    Stores BOTH lifetime accumulators AND a bounded ``observations``
    deque of ``(monotonic_ts, group_id, retained_bytes)`` tuples used
    to derive WINDOWED values at snapshot time. The cumulative-vs-window
    distinction is the post-MISSION-A.1 fix for F-002 + F-003 — pre-fix
    only the lifetime accumulators existed, and field names
    ``retained_bytes_estimate`` / ``distinct_group_id_count``
    misleadingly implied current-window semantics. Anti-pattern #49.

    Mission B B-P0-2 + B-P1-07 (B.1.P2 closure 2026-05-21):

    * ``distinct_group_ids`` is now an :class:`OrderedDict` (LRU by
      last-seen monotonic) rather than the pre-fix unbounded ``set``.
      Bounded at ``observations_maxlen * _DISTINCT_GROUP_IDS_LRU_MULTIPLIER``.
      The ``cumulative_distinct_group_id_count`` snapshot field
      reflects ``len()`` and is therefore bounded — operators reading
      it understand it as "distinct group_ids in the most-recent
      LRU-bound", NOT lifetime-distinct (which was operationally
      unreachable anyway because the lifetime would OOM under storm).
    * ``record_exception_cohort`` now dedup-checks the group_id
      against ``_EXCEPTION_COHORT_DEDUP_WINDOW_S`` (1.0s). Within the
      window, repeat observations of the same group_id are treated as
      chain-walk duplicates (no bytes add, no observation append).
      Outside the window, a same-class re-raise is genuine recurrence.
    * Saturation telemetry — when the observations deque is full at
      record time, a one-shot WARN ``exception_cohort.deque_saturation``
      fires (debounced by the producer to ≤1/snapshot-tick) so
      operators know the window-counts are under-reporting an
      ongoing storm.
    """

    observations_maxlen: int = _DEFAULT_EXCEPTION_COHORT_OBSERVATIONS_MAXLEN
    retained_bytes_estimate: int = 0
    # Mission B B-P1-07: LRU-bounded by last_seen_monotonic. Mapping
    # is group_id -> last_seen_monotonic (float). OrderedDict so we
    # can move_to_end on touch + popitem(last=False) on eviction.
    distinct_group_ids: OrderedDict[str, float] = field(default_factory=OrderedDict)
    observations: deque[tuple[float, str, int]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        # Build the deque with the operator-tunable maxlen; pydantic
        # constructor knows nothing about the registry's tuning-knob
        # protocol, so we set it here at construction time.
        if self.observations.maxlen != self.observations_maxlen:
            self.observations = deque(maxlen=self.observations_maxlen)


# ── ResourceRegistry — the lifetime-spanning state holder ──


class ResourceRegistry:
    """Process-local registry + per-cohort snapshot-field producer.

    Thread-safe via a single :class:`threading.Lock`. The registry is
    populated lazily by construction-site callers; the snapshotter
    (``observability/resources.py``) calls :meth:`snapshot_fields()`
    once per tick to consume the per-cohort state.

    ONNX sessions are tracked via :class:`weakref.WeakValueDictionary`
    so the registry does not artificially extend their lifetime; the
    ``onnx.session_count`` field reflects the count of live sessions
    that have NOT yet been garbage-collected.

    :class:`LRULockDict` instances are tracked via :class:`weakref.ref`;
    cardinality is read on demand via ``len(dict_ref())``. A dropped
    weakref yields cardinality 0 and is reaped from the registry on
    the next :meth:`snapshot_fields` call.
    """

    def __init__(
        self,
        *,
        exception_cohort_observations_maxlen: int = _DEFAULT_EXCEPTION_COHORT_OBSERVATIONS_MAXLEN,
    ) -> None:
        """Build a registry.

        Mission B B-P0-2 (B.1.P2 closure 2026-05-21) —
        ``exception_cohort_observations_maxlen`` is the operator-tunable
        bound on the rolling observations deque. Bootstrap passes
        :attr:`ObservabilityTuningConfig.exception_cohort_observations_maxlen`;
        tests using bare ``ResourceRegistry()`` get 128 (the pre-fix
        constant). Storm-level traffic that exceeds the deque maxlen
        within the window emits ``exception_cohort.deque_saturation``
        DEBUG so operators see the under-count risk.
        """
        self._lock = threading.Lock()
        self._onnx_sessions: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
        # Ordered label list parallel to the weakref values so callers
        # can read a stable list even after partial GC.
        self._onnx_label_order: list[str] = []
        # Lock-dict entries are stored as zero-arg callables: either a
        # ``weakref.ref(dict_ref)`` (returns the live ref or ``None`` if
        # collected) OR a strong-ref fallback closure used when the
        # subclass disables ``__weakref__``. Both satisfy ``Callable[[],
        # Any | None]`` so call-site semantics are identical.
        self._lock_dicts: dict[str, Callable[[], Any | None]] = {}
        self._to_thread = _ToThreadCounter()
        self._exception_cohort = _ExceptionCohortCounter(
            observations_maxlen=exception_cohort_observations_maxlen,
        )

    # ── Mutators ──

    def register_onnx_session(self, *, label: str, session: Any) -> None:  # noqa: ANN401 — session is an opaque third-party type (onnxruntime.InferenceSession)
        """Track *session* under *label*. Idempotent on label collision.

        The session is held via weakref; when the constructor's local
        reference drops + GC runs, the entry vanishes from the count
        automatically.
        """
        with self._lock:
            try:
                self._onnx_sessions[label] = session
                if label not in self._onnx_label_order:
                    self._onnx_label_order.append(label)
            except TypeError:
                # The session object may not support weak references
                # (some C-extension types don't). Best-effort: log
                # the gap; do not crash the constructor.
                logger.debug(
                    "h4.resource_registry.onnx_weakref_unsupported",
                    label=label,
                )

    def register_lock_dict(self, *, owner_id: str, dict_ref: Any) -> None:  # noqa: ANN401 — dict_ref is an opaque mapping/lock-dict instance
        """Track *dict_ref* under *owner_id*. Idempotent on owner-id collision.

        Stores a :func:`weakref.ref` so the registry does not extend
        the lock-dict's lifetime past its owner.
        """
        with self._lock:
            try:
                self._lock_dicts[owner_id] = weakref.ref(dict_ref)
            except TypeError:
                # If the lock-dict subclass disables weak references,
                # fall back to a strong reference but log a one-time
                # WARN — this is a latent leak risk.
                logger.warning(
                    "h4.resource_registry.lock_dict_strong_ref",
                    owner_id=owner_id,
                    hint=(
                        "LRULockDict subclass cannot be weak-referenced; "
                        "the registry retains a strong reference. This "
                        "is a latent leak risk — audit the subclass for "
                        "__slots__ / __weakref__ definitions."
                    ),
                )
                # Strong-ref fallback — a tiny closure mirroring the
                # weakref.ref API (zero-arg, returns the object). Lifetime
                # extension is the trade-off we accept for visibility.
                strong = dict_ref

                def _strong_ref() -> Any:  # noqa: ANN401 — mirrors weakref.ref API
                    return strong

                self._lock_dicts[owner_id] = _strong_ref

    def record_to_thread_dispatch(
        self,
        *,
        label: str,
        worker_count_at_dispatch: int,
        queue_depth: int,
        max_workers: int,
    ) -> None:
        """Increment per-label + total dispatch counters.

        Called once per ``dispatch_to_thread`` invocation. Cheap +
        non-blocking; safe to call from worker threads.

        Cardinality is bounded by ``_MAX_TRACKED_LABELS``; new labels
        beyond the cap coalesce into the synthetic ``"_overflow_"``
        bucket and a one-time WARN fires per process so operators see
        the saturation event without per-call log spam.
        """
        emit_overflow_warning = False
        with self._lock:
            self._to_thread.dispatch_count_total += 1
            self._to_thread.last_worker_count = worker_count_at_dispatch
            self._to_thread.last_queue_depth = queue_depth
            self._to_thread.last_max_workers = max_workers
            per_label = self._to_thread.dispatch_count_per_label
            if label in per_label:
                per_label[label] += 1
            elif len(per_label) < _MAX_TRACKED_LABELS:
                per_label[label] = 1
            else:
                per_label["_overflow_"] = per_label.get("_overflow_", 0) + 1
                if not self._to_thread._overflow_warned:
                    self._to_thread._overflow_warned = True
                    emit_overflow_warning = True
        # Emit overflow warning outside the lock so the structlog
        # pipeline does not run under our registry mutex.
        if emit_overflow_warning:
            logger.warning(
                "h4.resource_registry.to_thread_label_cap_exhausted",
                cap=_MAX_TRACKED_LABELS,
                hint=(
                    "More than _MAX_TRACKED_LABELS distinct dispatch_to_thread "
                    "labels observed; new labels coalesce into the '_overflow_' "
                    "bucket. Audit call sites for dynamically-generated labels."
                ),
            )

    def record_exception_cohort(
        self,
        *,
        group_id: str,
        sub_exception_count: int,  # noqa: ARG002 — accepted for caller symmetry; reserved for future cardinality-vs-bytes correlation
        retained_bytes_estimate: int,
    ) -> None:
        """Accumulate :class:`ExceptionGroup` retention estimate.

        ``group_id`` is a process-local synthetic identifier built by
        :mod:`sovyx.observability._exception_cohort_record_helper` as
        ``f"{exception_type}@{int(time.monotonic())}"`` — the
        1-second rounding collapses repeat instances of the same
        class within 1 s into one cohort observation. The registry
        ENFORCES that collapse here: same-group-id within
        :data:`_EXCEPTION_COHORT_DEDUP_WINDOW_S` is a chain-walk
        duplicate (no bytes add, no observation append). Outside the
        window the same class firing again is genuine recurrence and
        DOES add.

        Mission B B-P0-2 + B-P2-12 (B.1.P2 closure 2026-05-21):
        previously the bytes accumulator always added even on
        duplicate group_id — over-counting under chain walks. The
        docstring claimed "the registry deduplicates so a single
        ExceptionGroup observed multiple times in the chain doesn't
        multiply the counter" but the dedup only applied to the
        ``distinct_group_ids`` set, not the bytes accumulator.

        Mission B B-P1-07: ``distinct_group_ids`` is now LRU-bounded
        — see :class:`_ExceptionCohortCounter` docstring. Touches the
        LRU on every observation (window or out-of-window).

        Mission B B-P1-06: emits ``exception_cohort.deque_saturation``
        DEBUG (≤1/sec via the producer-side debouncing) when the
        observations deque is at maxlen at record time — operators
        know the window-counts are under-reporting an ongoing storm.
        """
        with self._lock:
            now = time.monotonic()
            cohort = self._exception_cohort
            last_seen = cohort.distinct_group_ids.get(group_id)
            if last_seen is not None and (now - last_seen) < _EXCEPTION_COHORT_DEDUP_WINDOW_S:
                # Chain-walk duplicate — same exception being observed
                # again at a different chain depth. Touch LRU; do not
                # double-count bytes or observation.
                cohort.distinct_group_ids[group_id] = now
                cohort.distinct_group_ids.move_to_end(group_id)
                return

            # Saturation check BEFORE append: if the deque is at maxlen
            # we are about to evict the oldest in-window observation
            # silently. Surface a debug breadcrumb so operators see
            # under-counting risk. Sampling discipline (≤1/sec) lives
            # at the helper producer, not here — the lock is held so
            # we keep this site minimal.
            _maxlen = cohort.observations.maxlen
            if _maxlen and len(cohort.observations) >= _maxlen:
                logger.debug(
                    "exception_cohort.deque_saturation",
                    maxlen=_maxlen,
                    hint=(
                        "raise SOVYX_OBSERVABILITY__TUNING__"
                        "EXCEPTION_COHORT_OBSERVATIONS_MAXLEN if the "
                        "window-undercount is operationally material"
                    ),
                )

            # Update LRU + lifetime accumulators + windowed observation.
            cohort.distinct_group_ids[group_id] = now
            cohort.distinct_group_ids.move_to_end(group_id)
            lru_cap = cohort.observations_maxlen * _DISTINCT_GROUP_IDS_LRU_MULTIPLIER
            while len(cohort.distinct_group_ids) > lru_cap:
                cohort.distinct_group_ids.popitem(last=False)
            cohort.retained_bytes_estimate += retained_bytes_estimate
            # MISSION-A.1 F-002+F-003: triple now carries group_id so window
            # observers can compute distinct-in-window counts WITHOUT
            # consulting the bounded ``distinct_group_ids`` LRU.
            cohort.observations.append(
                (now, group_id, retained_bytes_estimate),
            )

    # ── Reader ──

    def snapshot_fields(
        self,
        *,
        exception_cohort_window_s: float | None = None,
    ) -> dict[str, object]:
        """Return the per-cohort snapshot block consumed by ResourceSnapshotter.

        Every key MUST appear in :data:`_HEALTH_SNAPSHOT_FIELDS`.
        Quality Gate 15 enforces.

        ``exception_cohort_window_s`` is the rolling window (seconds) used
        to compute ``exception_cohort.window_retained_bytes`` and
        ``window_distinct_group_id_count``. The snapshotter passes
        ``config.tuning.exception_cohort_window_s``; tests may pass an
        explicit value. When ``None`` the window-bytes / window-count
        fields fall back to the cumulative values (a defensive default
        that keeps the registry leaf-callable without config access,
        though every production call site MUST supply window_s).
        """
        with self._lock:
            # Reap dead lock-dict weakrefs.
            dead_owners = [oid for oid, ref in self._lock_dicts.items() if ref() is None]
            for oid in dead_owners:
                self._lock_dicts.pop(oid, None)
            lock_dict_per_owner: dict[str, int] = {}
            for owner_id, ref in self._lock_dicts.items():
                target = ref()
                if target is None:
                    continue
                try:
                    lock_dict_per_owner[owner_id] = len(target)
                except TypeError:
                    # Lock-dict subclass without __len__ — best-effort.
                    lock_dict_per_owner[owner_id] = 0
            total_lock_dict_cardinality = sum(lock_dict_per_owner.values())

            # Reap dead ONNX label-order entries.
            live_onnx_labels = [
                lbl for lbl in self._onnx_label_order if lbl in self._onnx_sessions
            ]
            self._onnx_label_order = live_onnx_labels
            onnx_session_count = len(self._onnx_sessions)

            to_thread_state = (
                self._to_thread.last_worker_count,
                self._to_thread.last_queue_depth,
                self._to_thread.last_max_workers,
                self._to_thread.dispatch_count_total,
                dict(self._to_thread.dispatch_count_per_label),
            )

            # MISSION-A.1 F-002+F-003: cumulative reads identical to pre-fix.
            cumulative_retained_bytes = self._exception_cohort.retained_bytes_estimate
            cumulative_distinct_group_id_count = len(self._exception_cohort.distinct_group_ids)
            last_observation_monotonic = (
                self._exception_cohort.observations[-1][0]
                if self._exception_cohort.observations
                else 0.0
            )
            # Window reads — derive from the bounded observations deque.
            # When no window_s is supplied (test fixture / leaf usage) the
            # window fields default to the cumulative values; production
            # ResourceSnapshotter ALWAYS supplies window_s from config.
            if exception_cohort_window_s is None:
                window_retained_bytes = cumulative_retained_bytes
                window_distinct_group_id_count = cumulative_distinct_group_id_count
            else:
                window_threshold = time.monotonic() - exception_cohort_window_s
                window_observations = [
                    (ts, gid, retained)
                    for (ts, gid, retained) in self._exception_cohort.observations
                    if ts >= window_threshold
                ]
                window_retained_bytes = sum(
                    retained for (_ts, _gid, retained) in window_observations
                )
                window_distinct_group_id_count = len(
                    {gid for (_ts, gid, _retained) in window_observations}
                )

        # Cheap stdlib reads (outside the lock).
        gc_collections = list(gc.get_count())  # (gen0, gen1, gen2) → list for JSON
        gc_objects_count = len(gc.get_objects())

        tm_is_tracing = tracemalloc.is_tracing()
        tm_current_kb = 0
        tm_peak_kb = 0
        if tm_is_tracing:
            try:
                cur, peak = tracemalloc.get_traced_memory()
                tm_current_kb = cur // 1024
                tm_peak_kb = peak // 1024
            except Exception:  # noqa: BLE001 — tracemalloc edge cases (started/stopped concurrently)
                pass

        return {
            # MISSION-A.1.P3 F-006 (ADR-D15): ``to_thread.active_workers``
            # is no longer emitted by snapshot_fields(); the snapshotter
            # LENIENT-emits it via the legacy-alias path for one minor
            # cycle (sunset v0.55.0).
            # MISSION-A.1.P3.b F-007 (ADR-D16): the three twin-named
            # stale fields renamed to ``_at_last_dispatch`` to make
            # staleness explicit vs the LIVE counterparts emitted by
            # ``_capture_asyncio_metrics::asyncio.default_executor_state``.
            # Legacy keys LENIENT-emitted by the snapshotter (sunset
            # v0.55.0).
            "to_thread.pool_size_at_last_dispatch": to_thread_state[0],
            "to_thread.queue_depth_at_last_dispatch": to_thread_state[1],
            "to_thread.max_workers_at_last_dispatch": to_thread_state[2],
            "to_thread.dispatch_count_total": to_thread_state[3],
            "to_thread.dispatch_count_per_label": to_thread_state[4],
            "lock_dict.total_cardinality": total_lock_dict_cardinality,
            "lock_dict.per_owner": lock_dict_per_owner,
            "lock_dict.instance_count": len(self._lock_dicts),
            "onnx.session_count": onnx_session_count,
            "onnx.session_labels": live_onnx_labels,
            "gc.collections_by_gen": gc_collections,
            "gc.objects_count": gc_objects_count,
            "tracemalloc.is_tracing": tm_is_tracing,
            "tracemalloc.current_kb": tm_current_kb,
            "tracemalloc.peak_kb": tm_peak_kb,
            # MISSION-A.1 F-002+F-003: dual-emit. ``window_*`` are canonical
            # (consumed by the governor); ``cumulative_*`` exposes the
            # lifetime accumulator; legacy ``retained_bytes_estimate`` /
            # ``distinct_group_id_count`` keys are LENIENT shims aliased
            # to the cumulative values (sunset v0.55.0 — ADR-D14).
            "exception_cohort.cumulative_retained_bytes_since_start": (cumulative_retained_bytes),
            "exception_cohort.cumulative_distinct_group_id_count": (
                cumulative_distinct_group_id_count
            ),
            "exception_cohort.window_retained_bytes": window_retained_bytes,
            "exception_cohort.window_distinct_group_id_count": window_distinct_group_id_count,
            "exception_cohort.last_observation_monotonic": last_observation_monotonic,
        }


# ── Module-level singleton ──


_SINGLETON: ResourceRegistry | None = None
_SINGLETON_LOCK: Final[threading.Lock] = threading.Lock()


def get_default_resource_registry() -> ResourceRegistry:
    """Return the process-local lazy-initialized :class:`ResourceRegistry`.

    Mirrors the C4 :class:`EngineDegradedStore` lazy-singleton pattern;
    no bootstrap dependency, easy test isolation via
    :func:`reset_default_resource_registry`.
    """
    global _SINGLETON  # noqa: PLW0603 — explicit module-level singleton.
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ResourceRegistry()
    return _SINGLETON


def reset_default_resource_registry() -> None:
    """Test-only — reset the singleton to a fresh registry."""
    global _SINGLETON  # noqa: PLW0603
    with _SINGLETON_LOCK:
        _SINGLETON = None


# ── Module-level helper functions (convenience wrappers) ──


def register_onnx_session(*, label: str, session: Any) -> None:  # noqa: ANN401 — opaque third-party type
    """Convenience wrapper — register an ONNX session on the default registry."""
    get_default_resource_registry().register_onnx_session(label=label, session=session)


def register_lock_dict(*, owner_id: str, dict_ref: Any) -> None:  # noqa: ANN401 — opaque mapping type
    """Convenience wrapper — register a lock-dict on the default registry."""
    get_default_resource_registry().register_lock_dict(owner_id=owner_id, dict_ref=dict_ref)


def record_to_thread_dispatch(
    *,
    label: str,
    worker_count_at_dispatch: int,
    queue_depth: int,
    max_workers: int,
) -> None:
    """Convenience wrapper — record a ``dispatch_to_thread`` call."""
    get_default_resource_registry().record_to_thread_dispatch(
        label=label,
        worker_count_at_dispatch=worker_count_at_dispatch,
        queue_depth=queue_depth,
        max_workers=max_workers,
    )


def record_exception_cohort(
    *,
    group_id: str,
    sub_exception_count: int,
    retained_bytes_estimate: int,
) -> None:
    """Convenience wrapper — record an :class:`ExceptionGroup` observation."""
    get_default_resource_registry().record_exception_cohort(
        group_id=group_id,
        sub_exception_count=sub_exception_count,
        retained_bytes_estimate=retained_bytes_estimate,
    )


__all__ = [
    "CohortAxis",
    "FieldSpec",
    "ResourceRegistry",
    "_HEALTH_SNAPSHOT_FIELDS",
    "get_default_resource_registry",
    "record_exception_cohort",
    "record_to_thread_dispatch",
    "register_lock_dict",
    "register_onnx_session",
    "reset_default_resource_registry",
]
