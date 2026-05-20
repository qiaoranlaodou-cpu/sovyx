"""Periodic process-health snapshots — RSS, CPU, threads, fds, queues.

Background snapshotter that emits a structured ``self.health.snapshot``
record at a configurable interval (default 60 s — read from
:attr:`ObservabilitySamplingConfig.perf_hotpath_interval_seconds`). The
snapshot bundles process resource usage and async-loop pressure so a
single line in the log stream describes the daemon's overall load
without having to cross-reference multiple sources.

The snapshotter is started during bootstrap (Phase 6 Task 6.8) via
:func:`sovyx.observability.tasks.spawn` so it inherits the project's
task-tracking discipline; cancellation during shutdown is honoured.

``psutil`` is optional. When it is missing, the snapshotter still emits
asyncio-loop metrics (task counts) so operators don't lose all
observability — only the OS-level fields go to ``None`` and a one-time
WARNING ``self.health.psutil_missing`` flags the gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import ObservabilityConfig

logger = get_logger(__name__)


_PSUTIL_WARNED: bool = False


class QueueSnapshot(NamedTuple):
    """Single named queue's current depth and capacity at snapshot time."""

    name: str
    depth: int
    maxsize: int | None


# Provider returns the live (depth, maxsize) tuple at call time. Maxsize
# may be ``None`` for unbounded queues. Providers must be cheap and
# non-blocking — they are called from the snapshotter loop.
QueueProvider = Callable[[], tuple[int, int | None]]


def _capture_psutil_metrics(*, skip_expensive: bool = False) -> dict[str, object]:
    """Return ``psutil``-derived process metrics, or ``None`` fields on miss.

    Emits a one-time WARNING when ``psutil`` cannot be imported so the
    dependency gap is visible in the log stream without spamming every
    snapshot tick.

    ``skip_expensive`` skips ``proc.open_files()`` and ``proc.net_connections()``
    — both iterate the kernel handle table and on Windows call
    ``os.stat()`` on each handle. During async teardown (e.g. pytest
    ``_cancel_all_tasks`` invoking the snapshotter's
    :class:`asyncio.CancelledError` branch), handles in a closing state
    cause ``os.stat()`` to block indefinitely, hanging shutdown. The
    ``try/except`` wrappers below catch raised exceptions but not OS
    blocking — so the only safe option for the final shutdown snapshot
    is to skip these calls entirely. Best-effort metrics are accepted
    in shutdown by design.
    """
    global _PSUTIL_WARNED  # noqa: PLW0603 — module-level latch for "warn once".
    try:
        import psutil
    except ImportError:
        if not _PSUTIL_WARNED:
            _PSUTIL_WARNED = True
            logger.warning(
                "self.health.psutil_missing",
                **{"self.health.reason": "psutil unavailable; OS metrics dropped"},
            )
        return {
            "process.rss_bytes": None,
            "process.vms_bytes": None,
            "process.cpu_percent": None,
            "process.num_threads": None,
            "process.num_handles_or_fds": None,
            "process.open_files_count": None,
            "process.connections_count": None,
            # Mission H4 §0 item 4 + §T2.1 + §3 F2 extension fields.
            "process.memory_percent": None,
            "process.cpu_times_user_s": None,
            "process.cpu_times_system_s": None,
        }

    proc = psutil.Process()
    # cpu_percent() with interval=None returns the value since the last
    # call; the snapshotter's first tick will report 0.0, subsequent
    # ticks return a meaningful delta. We accept that tradeoff to keep
    # the snapshot non-blocking.
    try:
        cpu_percent = proc.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001 — psutil can raise NoSuchProcess on edge cases.
        cpu_percent = None

    try:
        mem = proc.memory_info()
        rss_bytes: int | None = int(mem.rss)
        vms_bytes: int | None = int(mem.vms)
    except Exception:  # noqa: BLE001
        rss_bytes = None
        vms_bytes = None

    try:
        num_threads: int | None = int(proc.num_threads())
    except Exception:  # noqa: BLE001
        num_threads = None

    # File descriptor count is platform-specific. Windows exposes
    # ``num_handles``; POSIX exposes ``num_fds``. Probe both so a
    # snapshot always has *something* meaningful in this slot.
    handles_or_fds: int | None
    try:
        if sys.platform == "win32":
            handles_or_fds = int(proc.num_handles())
        else:
            handles_or_fds = int(proc.num_fds())
    except Exception:  # noqa: BLE001
        handles_or_fds = None

    # ``open_files()`` and ``connections()`` can be expensive on
    # Windows (each call enumerates the kernel handle table). Wrap in
    # try/except and accept ``None`` if the OS denies access — the
    # snapshot is best-effort, not a forensic capture. Skip entirely
    # during shutdown to avoid the ``os.stat()`` hang on closing
    # handles documented in :func:`_capture_psutil_metrics`.
    open_files_count: int | None
    connections_count: int | None
    if skip_expensive:
        open_files_count = None
        connections_count = None
    else:
        try:
            open_files_count = len(proc.open_files())
        except Exception:  # noqa: BLE001
            open_files_count = None
        try:
            connections_count = len(proc.net_connections(kind="inet"))
        except Exception:  # noqa: BLE001
            connections_count = None

    # Mission H4 §0 item 4 + §T2.1 + §3 F2 — extension fields.
    # ``memory_percent`` is psutil-derived (rss / total physical memory).
    # ``cpu_times`` returns user + system seconds as floats; both fields
    # are monotonic per-process counters useful for derivative ratios
    # (e.g. CPU-bound classification via Δ user / Δ wallclock).
    try:
        memory_percent: float | None = float(proc.memory_percent())
    except Exception:  # noqa: BLE001 — psutil edge cases
        memory_percent = None

    try:
        cpu_times = proc.cpu_times()
        cpu_times_user_s: float | None = float(cpu_times.user)
        cpu_times_system_s: float | None = float(cpu_times.system)
    except Exception:  # noqa: BLE001
        cpu_times_user_s = None
        cpu_times_system_s = None

    return {
        "process.rss_bytes": rss_bytes,
        "process.vms_bytes": vms_bytes,
        "process.cpu_percent": cpu_percent,
        "process.num_threads": num_threads,
        "process.num_handles_or_fds": handles_or_fds,
        "process.open_files_count": open_files_count,
        "process.connections_count": connections_count,
        "process.memory_percent": memory_percent,
        "process.cpu_times_user_s": cpu_times_user_s,
        "process.cpu_times_system_s": cpu_times_system_s,
    }


def _capture_resource_registry_metrics() -> dict[str, object]:
    """Return per-cohort registry metrics consumed by snapshot emission.

    Delegates to :meth:`ResourceRegistry.snapshot_fields()`. Imports the
    registry lazily so the snapshotter doesn't pin the registry module
    at import time (the registry is initialized at first use; tests can
    reset it without affecting the snapshotter).

    Failures fall back to an empty dict — observability is best-effort,
    not load-bearing.

    Mission H4 §T2.1 — Phase 1.B wire-up.
    """
    try:
        from sovyx.observability._resource_registry import (  # noqa: PLC0415 — lazy by design
            get_default_resource_registry,
        )

        return get_default_resource_registry().snapshot_fields()
    except Exception:  # noqa: BLE001 — registry must never break the snapshot path
        logger.debug("self.health.resource_registry_capture_failed", exc_info=True)
        return {}


def _capture_asyncio_metrics() -> dict[str, object]:
    """Return current event-loop task counts + extension fields.

    Mission H4 §0 item 4 + §T2.1 + §3 F2 — extends the 3-field pre-H4
    block by 2 H4 fields:

    * ``asyncio.current_running_task_name`` — name of the currently-running
      task (or ``None`` outside a loop). Useful for correlating snapshot
      ticks to specific coroutines during forensic replay.
    * ``asyncio.default_executor_state`` — dict snapshot of the loop's
      default :class:`ThreadPoolExecutor` (pool_size + queue_depth +
      max_workers). Mirrors the per-call dispatch metrics under
      :data:`to_thread.*` but at the executor-state level so operators
      can see the pool independently of dispatch history.

    ``asyncio.all_tasks()`` requires a running loop; if called outside
    one, fall back to zeros + ``None`` task name rather than raising —
    the snapshotter loop itself is async, so this branch only triggers
    in test fixtures that import the helper directly.
    """
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return {
            "asyncio.task_count": 0,
            "asyncio.running_count": 0,
            "asyncio.pending_count": 0,
            "asyncio.current_running_task_name": None,
            "asyncio.default_executor_state": {
                "pool_size": 0,
                "queue_depth": 0,
                "max_workers": 0,
            },
        }
    running = sum(1 for t in tasks if not t.done())
    pending = sum(1 for t in tasks if not t.done() and not _is_currently_running(t))

    current_task_name: str | None
    try:
        current = asyncio.current_task()
        current_task_name = current.get_name() if current is not None else None
    except RuntimeError:
        current_task_name = None

    # Default executor state — same shape as the dispatch wrapper records.
    # We re-introspect here (not via ResourceRegistry) because the snapshot
    # represents the executor at observe-time, independent of the most-recent
    # dispatch (which may be stale if no work was submitted recently).
    executor_state: dict[str, int] = {"pool_size": 0, "queue_depth": 0, "max_workers": 0}
    try:
        loop = asyncio.get_running_loop()
        # ``_default_executor`` is documented CPython internal stable since 3.8;
        # mypy doesn't model it because it's an implementation detail. We rely
        # on it for observability only; failures degrade to zeros, not crashes.
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            with contextlib.suppress(AttributeError, RuntimeError):
                executor_state["pool_size"] = len(executor._threads)  # noqa: SLF001
            with contextlib.suppress(AttributeError, RuntimeError):
                executor_state["queue_depth"] = executor._work_queue.qsize()  # noqa: SLF001
            with contextlib.suppress(AttributeError, RuntimeError):
                executor_state["max_workers"] = executor._max_workers  # noqa: SLF001
    except RuntimeError:
        # No running loop — degraded already returned above; defensive.
        pass

    return {
        "asyncio.task_count": len(tasks),
        "asyncio.running_count": running,
        "asyncio.pending_count": pending,
        "asyncio.current_running_task_name": current_task_name,
        "asyncio.default_executor_state": executor_state,
    }


def _is_currently_running(task: asyncio.Task[object]) -> bool:
    """Best-effort check for whether *task* is mid-step on the loop.

    asyncio doesn't expose this directly; we treat the *current* task
    as "running" and everything else with ``done()`` false as
    "pending" (i.e. awaiting something). This is good enough for a
    coarse load metric.
    """
    try:
        return task is asyncio.current_task()
    except RuntimeError:
        return False


def _capture_queue_metrics(
    providers: Iterable[tuple[str, QueueProvider]],
) -> list[QueueSnapshot]:
    """Drain every registered queue provider into a list of snapshots.

    Providers that raise are logged at DEBUG (so a flaky source doesn't
    poison the whole snapshot) and skipped.
    """
    out: list[QueueSnapshot] = []
    for name, provider in providers:
        try:
            depth, maxsize = provider()
        except Exception:  # noqa: BLE001 — providers must never break the snapshot.
            logger.debug(
                "self.health.queue_provider_failed",
                **{"queue.name": name},
                exc_info=True,
            )
            continue
        out.append(QueueSnapshot(name=name, depth=int(depth), maxsize=maxsize))
    return out


class ResourceSnapshotter:
    """Periodically emit ``self.health.snapshot`` with process + loop metrics.

    Wire it from bootstrap (Phase 6 Task 6.8) when
    :attr:`ObservabilityFeaturesConfig.async_queue` is enabled. Stop it
    during shutdown by cancelling the task returned by
    :meth:`spawn`-ing :meth:`run`.

    Args:
        observability_config: The active :class:`ObservabilityConfig`.
            The interval is read from
            ``observability_config.sampling.perf_hotpath_interval_seconds``.
        queue_providers: Optional iterable of ``(name, provider)`` tuples.
            Each provider returns the live ``(depth, maxsize)`` of a
            named queue. Cheap, synchronous, must not block.
    """

    def __init__(
        self,
        observability_config: ObservabilityConfig,
        queue_providers: Iterable[tuple[str, QueueProvider]] | None = None,
    ) -> None:
        self._config = observability_config
        self._providers: list[tuple[str, QueueProvider]] = list(queue_providers or [])
        self._stop_event = asyncio.Event()
        self._started_at: float | None = None

    def register_queue(self, name: str, provider: QueueProvider) -> None:
        """Add a queue provider after construction.

        Useful when subsystems with their own lifecycle (audio capture,
        output queue) come up after the snapshotter has already been
        started — they can register their queue without restarting the
        loop.
        """
        self._providers.append((name, provider))

    def stop(self) -> None:
        """Signal the loop to exit on its next wake-up."""
        self._stop_event.set()

    async def run(self) -> None:
        """Background loop body — call via ``spawn()``.

        Wakes every ``perf_hotpath_interval_seconds`` (configurable),
        captures a snapshot, and emits ``self.health.snapshot``.
        Cancellation triggers a final snapshot tagged
        ``self.health.snapshot_final=True`` so a graceful shutdown
        leaves a closing line in the log.
        """
        interval = max(1, int(self._config.sampling.perf_hotpath_interval_seconds))
        self._started_at = time.monotonic()
        logger.info(
            "self.health.snapshotter_started",
            **{"self.health.interval_seconds": interval},
        )
        try:
            while not self._stop_event.is_set():
                self._emit_snapshot(final=False)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            self._emit_snapshot(final=True)
            raise
        else:
            self._emit_snapshot(final=True)
        finally:
            logger.info("self.health.snapshotter_stopped")

    def _emit_snapshot(self, *, final: bool) -> None:
        """Capture and emit a single snapshot record.

        Failures inside the capture helpers are absorbed there; this
        function only fails if the structured logger itself raises,
        which is treated as a bug worth surfacing.

        On the ``final=True`` path (shutdown), expensive psutil calls
        (``open_files``/``net_connections``) are skipped to avoid a
        Windows-specific ``os.stat()`` hang on closing handles during
        async teardown — see :func:`_capture_psutil_metrics`.

        Mission H4 §T2.1 extension: the payload now includes the
        per-cohort registry metrics (ONNX session count, LRULockDict
        cardinality, asyncio.to_thread dispatch counters, gc /
        tracemalloc / exception-cohort retention) consumed by the Phase
        1.D :class:`ResourceCohortGovernor`. The ``process.rss_bytes``
        field dual-emits the legacy ``system.rss_bytes`` alias during
        the LENIENT calibration window per ADR-D9.
        """
        psutil_metrics = _capture_psutil_metrics(skip_expensive=final)
        asyncio_metrics = _capture_asyncio_metrics()
        registry_metrics = _capture_resource_registry_metrics()
        queues = _capture_queue_metrics(self._providers)

        uptime_s: float | None = None
        if self._started_at is not None:
            uptime_s = round(time.monotonic() - self._started_at, 3)

        # ADR-D9 dual-emit during Mission H4 LENIENT window — both
        # ``process.rss_bytes`` (canonical) and ``system.rss_bytes``
        # (legacy alias) appear in the payload. The H4 STRICT flip at
        # v0.54.0 drops the alias.
        psutil_with_legacy: dict[str, object] = dict(psutil_metrics)
        rss = psutil_with_legacy.get("process.rss_bytes")
        if isinstance(rss, int):
            psutil_with_legacy["system.rss_bytes"] = rss  # h4-allowlist: legacy alias

        payload: dict[str, object] = {
            "self.health.snapshot_final": final,
            "self.health.uptime_s": uptime_s,
            **psutil_with_legacy,
            **asyncio_metrics,
            **registry_metrics,
            "self.health.queue_count": len(queues),
            "self.health.queues": [
                {
                    "name": q.name,
                    "depth": q.depth,
                    "maxsize": q.maxsize,
                }
                for q in queues
            ],
        }
        logger.info("self.health.snapshot", **payload)

        # Mission H4 §T4.1 — Phase 1.D ResourceCohortGovernor evaluation.
        # Best-effort; failures absorbed (governor must NEVER break the
        # snapshot path). On budget-exceeded, the governor emits a WARN
        # + records to the C4 composite store under
        # ``axis="engine_resources"`` so the existing DegradedBanner
        # renders the new cohort automatically.
        try:
            from sovyx.observability._resource_cohort_governor import (  # noqa: PLC0415 — lazy import
                emit_axis_entries,
                get_default_resource_cohort_governor,
                record_resource_snapshot_emission,
            )

            record_resource_snapshot_emission(final=final)
            evaluations = get_default_resource_cohort_governor().evaluate_snapshot(payload)
            emit_axis_entries(evaluations)
        except Exception:  # noqa: BLE001 — governor must NEVER break the snapshot path
            logger.debug("self.health.cohort_governor_failed", exc_info=True)
