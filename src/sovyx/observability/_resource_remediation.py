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
        "`asyncio.task_count` to find which subsystem is busy."
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
        "Healthy single-mind GA: 30-80. Sustained growth suggests an "
        "unclosed socket / file pool — `sovyx doctor` general check "
        "surfaces hung connections."
    ),
    "process.open_files_count": (
        "Count of `proc.open_files()` (psutil; cheap on Linux/macOS, "
        "expensive on Windows). Skipped on the `final=True` shutdown "
        "snapshot to avoid the Windows `os.stat()` hang documented in "
        "CLAUDE.md anti-pattern #30."
    ),
    "process.connections_count": (
        "Count of `proc.net_connections(kind='inet')` (psutil). Healthy "
        "single-mind GA: 1-10 (dashboard HTTP + LLM provider keepalive "
        "+ bridge channels). Skipped on the `final=True` shutdown "
        "snapshot."
    ),
    "process.memory_percent": (
        "Process RSS as percentage of system physical memory "
        "(psutil-derived). Single-mind GA baseline: 3-15% on 8 GiB hosts "
        "(250-800 MiB loaded). Sustained > 25% on a quiescent operator "
        "session suggests a leak; cross-reference `process.rss_bytes` "
        "growth + `lock_dict.total_cardinality` + `onnx.session_count` "
        "for cohort attribution. Enable "
        "`observability.features.tracemalloc=True` for allocator-level "
        "forensics on the next RSS_GROWTH cohort breach."
    ),
    "process.cpu_times_user_s": (
        "Cumulative user-mode CPU seconds (psutil-derived, monotonic per "
        "process). Healthy operator session: ~0.1-2 s/min depending on "
        "voice activity. Compute Δ user / Δ wallclock between snapshots "
        "for instantaneous CPU-bound classification; sustained ratio > "
        "0.8 indicates a CPU-bound subsystem — cross-reference "
        "`to_thread.dispatch_count_per_label` for the noisy worker."
    ),
    "process.cpu_times_system_s": (
        "Cumulative kernel-mode CPU seconds (psutil-derived, monotonic). "
        "Healthy single-mind GA: << user time (kernel-bound work is "
        "rare). High system-time growth without matching user-time "
        "growth suggests heavy syscall pressure (FD churn, network IO, "
        "or `psutil.open_files()` enumeration on Windows — see "
        "anti-pattern #30)."
    ),
    # ── asyncio block ──
    "asyncio.task_count": (
        "Total `asyncio.all_tasks()` count at snapshot time. Healthy: "
        "10-25 (heartbeat + capture loop + LLM router + brain + dashboard "
        "background tasks + various schedulers). Sustained > 50 "
        "suggests a task leak — inspect `asyncio.pending_count` to see "
        "which tasks are stuck awaiting."
    ),
    "asyncio.running_count": (
        "Tasks in non-done state (subset of `asyncio.task_count`). "
        "Typically 1-3 truly-running + N awaiting tasks; large running "
        "counts under non-CPU-bound load suggest a busy-loop."
    ),
    "asyncio.pending_count": (
        "Tasks that are not done AND not the currently-running task. "
        "Most tasks live here (awaiting I/O); sustained growth without "
        "matching completion is a leak."
    ),
    "asyncio.current_running_task_name": (
        "Name of `asyncio.current_task()` at snapshot time (or None "
        "outside a running loop). Useful for correlating snapshot ticks "
        "to specific coroutines during forensic replay — when a cohort "
        "fires BUDGET_EXCEEDED on tick T, the current task name surfaces "
        "which subsystem was active. Anonymous tasks default to "
        "`Task-N` (asyncio's auto-name); production code SHOULD pass "
        "`name=...` to `loop.create_task(...)` for forensic clarity."
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
    "to_thread.pool_size": (
        "Number of live worker threads in the loop default "
        "ThreadPoolExecutor. Grows on demand up to "
        "`to_thread.max_workers`; high values during failover suggest "
        "ONNX inference contention. Inspect "
        "`to_thread.dispatch_count_per_label` for the noisy label."
    ),
    "to_thread.active_workers": (
        "Alias of `to_thread.pool_size` per Mission H4 §3 F2 canonical "
        "field name (Python's ThreadPoolExecutor exposes only "
        "`len(_threads)` — total alive workers — without a separate "
        "busy/idle metric). Treat this and `pool_size` as the same "
        "value; remediation guidance is the same."
    ),
    "to_thread.queue_depth": (
        "Pending submissions waiting for a worker thread (internal "
        "ThreadPoolExecutor queue). Spikes mean inference latency is "
        "outpacing worker availability."
    ),
    "to_thread.max_workers": (
        "Configured ThreadPoolExecutor ceiling (default Python "
        "`min(32, os.cpu_count() + 4)`). Operator override via "
        "`SOVYX_OBSERVABILITY__TUNING__TO_THREAD_MAX_WORKERS` (deferred "
        "to Phase 1.E)."
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
