"""Per-field operator-hint mapping for ``sovyx doctor resources --explain``.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T3.4. Mirrors the H3 ``_user_remediation.py`` pattern at the field-name
level — operators running ``sovyx doctor resources --explain <field>``
get an operator-actionable hint that says (a) what the field measures,
(b) what a healthy range looks like, and (c) what to do when the
:class:`ResourceCohortGovernor` flags it.

Public surface: :data:`FIELD_REMEDIATIONS` (Mapping[str, str]) and
:func:`remediation_for` (lookup helper with fallback).

The keys MUST match the canonical field names in
:data:`sovyx.observability._resource_registry._HEALTH_SNAPSHOT_FIELDS`
— Quality Gate 15 keeps producer ↔ consumer ↔ remediation in lockstep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── Per-field operator hints ────────────────────────────────────────
#
# Each entry is one paragraph keyed on a canonical
# ``self.health.snapshot`` field. The hint follows a stable shape:
#
# 1. What the field measures (one sentence).
# 2. What a healthy range looks like for single-mind GA hardware.
# 3. What to inspect / remediate when the field crosses budget.
#
# Operators typically run ``sovyx doctor resources --explain
# <field>`` after a ``engine.resources.cohort_budget_exceeded`` WARN
# fires; the hint plus the C4 composite-banner action chips drive
# remediation without needing to dig into Sovyx source.

FIELD_REMEDIATIONS: Final[Mapping[str, str]] = {
    # ── process / OS resource block ──
    "process.rss_bytes": (
        "Process resident set size (live memory; psutil-derived). "
        "Healthy range for single-mind GA: 250-800 MiB depending on "
        "loaded models (Moonshine + Kokoro + brain embedding ≈ 600 MiB). "
        "Sustained > 1.5 GiB OR Δ > 512 MiB in 60 s triggers the "
        "RSS_GROWTH cohort; inspect `onnx.session_count` + "
        "`lock_dict.per_owner` on the same snapshot to attribute. "
        "Enable `observability.features.tracemalloc=True` for "
        "allocator-level forensics on the next breach."
    ),
    "process.vms_bytes": (
        "Process virtual memory size (psutil-derived). Generally tracks "
        "`process.rss_bytes` with a 2-4× ratio; large divergences "
        "suggest mapped files or memory-mapped ONNX models. Not "
        "directly cohort-governed — informational only."
    ),
    "process.cpu_percent": (
        "Snapshot-tick CPU utilization (psutil-derived, interval since "
        "last snapshot). Sustained > 80% with a quiescent operator "
        "session suggests a runaway loop; cross-reference "
        "`asyncio.task_count` to find which subsystem is busy. "
        "MISSION-A.2.P5 F-010 disclosure: ``psutil.cpu_percent(interval=None)`` "
        "returns 0.0 on the FIRST call after process start (no prior "
        "sample to delta against) — treat the first snapshot's value "
        "as calibration; meaningful CPU readings start at the second "
        "snapshot tick (default 60 s after boot). Operators alerting on "
        "CPU should skip the first sample or filter on `uptime_s > "
        "tick_interval`."
    ),
    "process.num_threads": (
        "Live OS thread count (psutil-derived). Single-mind GA baseline: "
        "18-70 threads (loaded ONNX sessions + asyncio default executor "
        "+ uvicorn workers + audio capture threads). Δ > 32 in 60 s "
        "triggers the THREAD_COUNT cohort; inspect "
        "`to_thread.dispatch_count_per_label` for the noisy label."
    ),
    "process.num_handles_or_fds": (
        "Open file descriptors (POSIX) or kernel handles (Windows). "
        "MISSION-A.2.P5 F-009 disclosure: PER-PLATFORM baselines — "
        "POSIX (Linux/macOS): `psutil.num_fds()` returns file descriptor "
        "count only; healthy single-mind GA 30–80. WINDOWS: "
        "`psutil.num_handles()` returns ALL kernel handles (files, "
        "registry, GDI, threads, events, mutexes); healthy single-mind "
        "GA 2,000–10,000+. Magnitudes are NOT comparable across "
        "platforms. Alert thresholds MUST be per-platform. Sustained "
        "growth suggests an unclosed socket / file pool — "
        "`sovyx doctor` general check surfaces hung connections."
    ),
    "process.open_files_count": (
        "Count of `proc.open_files()` (psutil; cheap on Linux/macOS, "
        "expensive on Windows). Skipped on the `final=True` shutdown "
        "snapshot to avoid the Windows `os.stat()` hang documented in "
        "CLAUDE.md anti-pattern #30. When ``None`` inspect the parallel "
        "``process.open_files_status`` field for the exact reason "
        "(MISSION-A.2.P4 F-012)."
    ),
    "process.open_files_status": (
        "Disambiguation of why ``process.open_files_count`` may be "
        "``None`` (MISSION-A.2.P4 F-012). One of: ``ok`` (count is "
        "live), ``skipped_shutdown`` (final shutdown snapshot — "
        "expensive psutil calls skipped to avoid Windows os.stat() "
        "hang), ``denied`` (psutil raised PermissionError), "
        "``unsupported`` (psutil edge case — NoSuchProcess, OSError), "
        "``psutil_missing`` (psutil not installed). Operator action "
        "depends on the status: ``denied`` → check process "
        "capabilities; ``unsupported`` → file a bug with platform info."
    ),
    "process.connections_count": (
        "Count of `proc.net_connections(kind='inet')` (psutil). Healthy "
        "single-mind GA: 1-10 (dashboard HTTP + LLM provider keepalive "
        "+ bridge channels). Skipped on the `final=True` shutdown "
        "snapshot. When ``None`` inspect the parallel "
        "``process.connections_status`` field for the exact reason "
        "(MISSION-A.2.P4 F-012)."
    ),
    "process.connections_status": (
        "Disambiguation of why ``process.connections_count`` may be "
        "``None`` (MISSION-A.2.P4 F-012). Same status enum as "
        "``process.open_files_status``: ``ok`` / ``skipped_shutdown`` / "
        "``denied`` / ``unsupported`` / ``psutil_missing``. On Linux "
        "non-root daemons ``net_connections`` shows only the process's "
        "own connections (system-wide requires CAP_NET_ADMIN) — a "
        "lower-than-expected count with status ``ok`` is permission-"
        "restricted, not under-reporting."
    ),
    "process.memory_percent": (
        "Process RSS as percentage of system physical memory "
        "(psutil-derived). Single-mind GA baseline: 3-15% on 8 GiB hosts "
        "(250-800 MiB loaded). Sustained > 25% on a quiescent operator "
        "session suggests a leak; cross-reference `process.rss_bytes` "
        "growth + `lock_dict.total_cardinality` + `onnx.session_count` "
        "for cohort attribution. Enable "
        "`observability.features.tracemalloc=True` for allocator-level "
        "forensics on the next RSS_GROWTH cohort breach. "
        "MISSION-A.2.P5 F-008 disclosure: psutil reads HOST physical "
        "memory total — on Docker/k8s with cgroup limits the host "
        "total is NOT the container limit. A process consuming 1.5 GiB "
        "in a 2 GiB cgroup shows ~5% on a 32 GiB host (apparent ample "
        "headroom) but is one allocation away from OOM kill. "
        "Containerized operators MUST cross-reference "
        "`/sys/fs/cgroup/memory.max` (cgroup v2) or "
        "`/sys/fs/cgroup/memory/memory.limit_in_bytes` (cgroup v1) "
        "for the container-relative percentage."
    ),
    "process.cpu_times_user_s": (
        "Cumulative user-mode CPU seconds (psutil-derived, monotonic per "
        "process). Healthy operator session: ~0.1-2 s/min depending on "
        "voice activity. MISSION-A.2.P5 F-011 disclosure: this field is "
        "CUMULATIVE since process start (NOT instantaneous). Reading "
        "the raw value as a rate is wrong. Concrete derivative formula: "
        "``cpu_pct = (cpu_times_user_s[N] - cpu_times_user_s[N-1]) / "
        "(snapshot_taken_at_monotonic[N] - snapshot_taken_at_monotonic[N-1])``. "
        "Sustained ratio > 0.8 indicates a CPU-bound subsystem — "
        "cross-reference ``to_thread.dispatch_count_per_label`` for "
        "the noisy worker."
    ),
    "process.cpu_times_system_s": (
        "Cumulative kernel-mode CPU seconds (psutil-derived, monotonic — "
        "MISSION-A.2.P5 F-011 disclosure: use the same Δ/Δt derivative "
        "formula as ``cpu_times_user_s``). Healthy single-mind GA: << "
        "user time (kernel-bound work is rare). High system-time growth "
        "without matching user-time growth suggests heavy syscall "
        "pressure (FD churn, network IO, or `psutil.open_files()` "
        "enumeration on Windows — see anti-pattern #30)."
    ),
    # ── asyncio block ──
    "asyncio.task_count": (
        "Total `asyncio.all_tasks()` count at snapshot time. Healthy: "
        "10-25 (heartbeat + capture loop + LLM router + brain + dashboard "
        "background tasks + various schedulers). Sustained > 50 "
        "suggests a task leak — inspect `asyncio.awaiting_count` (post-"
        "MISSION-A.1.P3.b rename of pending_count, ADR-D16) to see "
        "which tasks are stuck awaiting."
    ),
    "asyncio.not_done_count": (
        "Count of asyncio tasks whose ``done()`` predicate is False at "
        "snapshot time — i.e. tasks not yet completed. NOTE: this is NOT "
        "the count of tasks actively executing on the loop step; asyncio "
        "does not expose that metric. Tasks blocked on ``await `` (sleep, "
        "I/O, lock) count as 'not done'. Renamed from the pre-MISSION-A.1 "
        "``asyncio.running_count`` which had the same math but a name that "
        "promised executor-step semantics (anti-pattern #51, ADR-D16). "
        "Sunset for the legacy ``running_count`` shim: v0.55.0."
    ),
    "asyncio.awaiting_count": (
        "Count of asyncio tasks that are not done AND not the currently-"
        "running task (snapshot task excluded). Most workload tasks live "
        "here — sustained growth without matching completion suggests a "
        "task leak. Renamed from the pre-MISSION-A.1 "
        "``asyncio.pending_count`` (anti-pattern #51, ADR-D16). Sunset "
        "for the legacy ``pending_count`` shim: v0.55.0."
    ),
    "asyncio.all_task_names": (
        "List of names of every not-done asyncio task at snapshot time, "
        "capped at 64. Replaces the pre-MISSION-A.1 "
        "``asyncio.current_running_task_name`` field which always "
        "returned the SNAPSHOTTER task name (observation paradox — from "
        "inside the snapshotter coroutine, ``asyncio.current_task()`` "
        "IS the snapshotter). Operators inspecting concurrency see the "
        "actual workload; anonymous tasks default to ``Task-N`` "
        "(asyncio's auto-name) — production code SHOULD pass "
        "``name=...`` to ``loop.create_task(...)`` for forensic clarity. "
        "Anti-pattern #50 + ADR-D15. Sunset for the legacy "
        "``current_running_task_name`` shim: v0.55.0."
    ),
    "asyncio.default_executor_state": (
        "Dict snapshot of the loop's default ThreadPoolExecutor: "
        "`{pool_size, queue_depth, max_workers}`. Mirrors the per-call "
        "dispatch metrics under `to_thread.*` but at the executor-state "
        "level — operators see the pool size INDEPENDENTLY of dispatch "
        "history (which may be stale if no work was submitted recently). "
        "Healthy single-mind GA: pool_size ≤ ~12, queue_depth 0-2. "
        "Large queue_depth means inference latency outpaces worker "
        "availability."
    ),
    # ── to_thread block ──
    # MISSION-A.1.P3.b F-007 (anti-pattern #51, ADR-D16): the three
    # ``pool_size`` / ``max_workers`` / ``queue_depth`` fields are
    # STALE — recorded at last ``dispatch_to_thread()`` call rather
    # than read live from the executor at snapshot time. Renamed to
    # ``_at_last_dispatch`` to make staleness explicit; the LIVE
    # twin-named fields live under
    # ``asyncio.default_executor_state.{pool_size, queue_depth, max_workers}``.
    # Legacy keys LENIENT-emitted by the snapshotter (sunset v0.55.0).
    "to_thread.pool_size_at_last_dispatch": (
        "Number of live worker threads in the loop default "
        "ThreadPoolExecutor, as recorded at the LAST ``dispatch_to_thread`` "
        "call. NOT a live read — may be up to one snapshot interval "
        "stale if no dispatch occurred recently. For the LIVE pool "
        "size at snapshot time, inspect "
        "``asyncio.default_executor_state.pool_size``. Renamed from "
        "the pre-MISSION-A.1 ``to_thread.pool_size``; legacy key "
        "LENIENT-emitted through v0.55.0."
    ),
    "to_thread.queue_depth_at_last_dispatch": (
        "Pending submissions in the ThreadPoolExecutor work queue at "
        "the LAST ``dispatch_to_thread`` call. NOT live — see the LIVE "
        "twin at ``asyncio.default_executor_state.queue_depth``. "
        "Spikes mean inference latency was outpacing worker "
        "availability at last dispatch. Renamed from "
        "``to_thread.queue_depth``; legacy key LENIENT-emitted "
        "through v0.55.0."
    ),
    "to_thread.max_workers_at_last_dispatch": (
        "Configured ThreadPoolExecutor ceiling as recorded at the "
        "LAST ``dispatch_to_thread`` call. Default Python "
        "``min(32, os.cpu_count() + 4)``. For the LIVE max see "
        "``asyncio.default_executor_state.max_workers``. Operator "
        "override via "
        "``SOVYX_OBSERVABILITY__TUNING__TO_THREAD_MAX_WORKERS`` (deferred "
        "to Phase 1.E). Renamed from ``to_thread.max_workers``; legacy "
        "key LENIENT-emitted through v0.55.0."
    ),
    "to_thread.dispatch_count_total": (
        "Cumulative count of `dispatch_to_thread(...)` calls since "
        "process start. Monotonic; rate-of-growth (e.g. delta per "
        "snapshot tick) is the operator-actionable signal."
    ),
    "to_thread.dispatch_count_per_label": (
        "Per-label dispatch counter. Labels match the `dispatch_to_thread(label='...')` "
        "argument: typically `voice.vad.silero`, `brain.embedding`, etc. "
        "If a single label dominates, it's a hot path; if a synthetic "
        "label `_overflow_` appears, you have > 128 distinct dispatch "
        "labels — audit call sites for dynamically-generated labels."
    ),
    # ── lock_dict block ──
    "lock_dict.total_cardinality": (
        "Aggregate `len()` across every registered `LRULockDict` "
        "instance. Healthy single-mind GA: 0-2000 keys total. Crossing "
        "the 6000 soft cap triggers the LOCK_DICT_CARDINALITY cohort; "
        "inspect `lock_dict.per_owner` for the saturated owner_id."
    ),
    "lock_dict.per_owner": (
        "Per-owner-id breakdown of `LRULockDict` cardinality. Common "
        "owners: `bridge.manager.conv_locks`, "
        "`voice.health.watchdog.lifecycle_locks`, etc. Each owner has "
        "its own `maxsize`; if one dominates, bump its maxsize or audit "
        "the eviction rate."
    ),
    "lock_dict.instance_count": (
        "Number of registered `LRULockDict` instances. Static at "
        "single-mind GA (9 instances); growth suggests a new subsystem "
        "shipped without registering its locks — Quality Gate 15 catches "
        "this at build time."
    ),
    # ── onnx block ──
    "onnx.session_count": (
        "Live `onnxruntime.InferenceSession` count (weakref-tracked). "
        "Single-mind GA expects 4-5: VAD (Silero) + STT (Moonshine) + "
        "wake-word + brain embedding + optional TTS (Piper OR Kokoro). "
        "> 8 triggers the ONNX_SESSION cohort; inspect "
        "`onnx.session_labels` for duplicates that suggest a lifecycle "
        "leak."
    ),
    "onnx.session_labels": (
        "Ordered list of registered ONNX session labels. Look for "
        "duplicates (same label appearing twice) — that's a leak: a "
        "session was constructed but the old reference was not "
        "released. Garbage-collected sessions are auto-reaped via "
        "weakref."
    ),
    # ── gc block ──
    "gc.collections_by_gen": (
        "Per-generation `gc.get_count()` 3-tuple. Healthy: gen0 is in "
        "the hundreds, gen1 small (1-20), gen2 single-digit. Inverted "
        "counts (gen2 > gen1 > gen0) suggest manual `gc.collect()` "
        "abuse OR a long-lived reference cycle."
    ),
    "gc.objects_count": (
        "Total `len(gc.get_objects())`. Single-mind GA: 50-100k objects. "
        "Steady growth suggests a reference leak; correlate with "
        "`tracemalloc.current_kb` for allocator attribution."
    ),
    # ── tracemalloc block ──
    "tracemalloc.is_tracing": (
        "Boolean `tracemalloc.is_tracing()`. False by default; set "
        "`SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC=true` + restart "
        "daemon to enable. Adds 25-30% memory overhead — operator opt-in "
        "for forensic deep-dive sessions only."
    ),
    "tracemalloc.current_kb": (
        "Currently-traced memory (KiB). Only meaningful when "
        "`tracemalloc.is_tracing=True`. Diverges from "
        "`process.rss_bytes` by external mappings (ONNX models, "
        "shared libraries) + tracemalloc overhead."
    ),
    "tracemalloc.peak_kb": (
        "Peak tracemalloc-observed allocation since `tracemalloc.start()` "
        "(KiB). Spikes here indicate transient allocations the OS later "
        "reclaimed."
    ),
    # ── exception_cohort block ──
    # MISSION-A.1 F-002+F-003 (anti-pattern #49): cumulative-vs-window split.
    "exception_cohort.cumulative_retained_bytes_since_start": (
        "MONOTONIC ACCUMULATOR — bytes retained by `ExceptionGroup` "
        "traceback chains summed across every observation since process "
        "start. Never decays; never resets. Useful for forensic 'total "
        "exception cost since boot'. Do NOT use for real-time cohort "
        "budget: use `exception_cohort.window_retained_bytes` instead."
    ),
    "exception_cohort.cumulative_distinct_group_id_count": (
        "MONOTONIC ACCUMULATOR — count of distinct ExceptionGroup "
        "synthetic IDs ever observed. Grows unbounded across process "
        "lifetime; loses 'recent diversity' meaning over time. Use "
        "`exception_cohort.window_distinct_group_id_count` for "
        "operationally-meaningful diversity within the current window."
    ),
    "exception_cohort.window_retained_bytes": (
        "Sum of `ExceptionGroup` retained-bytes estimates observed "
        "within the last `tuning.exception_cohort_window_s` seconds "
        "(default 300 s). Decays naturally as observations age out of "
        "the deque (maxlen 128). The cohort governor reads THIS field "
        "for the EXCEPTION_COHORT budget verdict — large values "
        "(> 16 MiB by default) trigger BUDGET_EXCEEDED. Typically "
        "follows a 500-storm (Mission C2 class) — fix the producer "
        "boundary, not the cohort governor."
    ),
    "exception_cohort.window_distinct_group_id_count": (
        "Count of distinct ExceptionGroup synthetic IDs observed within "
        "the last `tuning.exception_cohort_window_s` seconds. Decays as "
        "observations age out of the deque. High values + high "
        "`window_retained_bytes` = single bug producing many small "
        "ExceptionGroups; high count + low bytes = diverse-but-cheap "
        "exception landscape."
    ),
    "exception_cohort.last_observation_monotonic": (
        "monotonic-clock timestamp of the last "
        "`record_exception_cohort` call. Operators use this to identify "
        "stale data (no recent exceptions means the cohort retained_bytes "
        "reflects past state)."
    ),
}


def remediation_for(field: str) -> str:
    """Return the operator-actionable hint for *field*.

    Returns a fallback "no remediation available; check
    docs/operations/resource-hygiene.md" string when the field is not
    registered — never raises, so the doctor CLI surface stays robust
    on novel fields.
    """
    return FIELD_REMEDIATIONS.get(
        field,
        (
            f"No operator-hint registered for field {field!r}. See "
            "docs/operations/resource-hygiene.md for the canonical field "
            "taxonomy + remediation playbook. If this field is new and "
            "should have a hint, add an entry to "
            "src/sovyx/observability/_resource_remediation.py "
            "FIELD_REMEDIATIONS."
        ),
    )


__all__ = ["FIELD_REMEDIATIONS", "remediation_for"]
