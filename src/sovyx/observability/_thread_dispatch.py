"""Mission H4 — labelled ``asyncio.to_thread`` wrapper.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T1.2.

Provides :func:`dispatch_to_thread` — a 1:1 drop-in for
:func:`asyncio.to_thread` augmented with a leading ``label`` parameter.
The wrapper records the dispatch via
:meth:`ResourceRegistry.record_to_thread_dispatch` so per-label
counters surface on ``self.health.snapshot`` under the
``to_thread.dispatch_count_per_label`` field.

Implementation:

* Resolves the running loop's default ``ThreadPoolExecutor`` (creating
  it implicitly on first call if ``None``) and introspects
  ``executor._threads`` / ``executor._work_queue`` for the
  ``worker_count_at_dispatch`` / ``queue_depth`` metrics. The private
  attributes are stable across CPython 3.8+ and the introspection is
  observability-only — failures fall back to ``(0, 0)`` and surface a
  DEBUG record instead of breaking the call.
* Returns ``await loop.run_in_executor(None, functools.partial(fn,
  *args, **kwargs))`` — identical semantics to
  :func:`asyncio.to_thread` so existing call-site signatures keep
  working without any change beyond the new ``label`` prefix.

Anti-pattern compliance:

* #14 — sync CPU-bound work MUST run via :func:`asyncio.to_thread`
  (or this wrapper); never inline in an ``async def`` body.
* #47 — every migrated site emits a label so the cohort governor
  (Phase 1.D) can attribute thread-pool growth to a specific cohort.

Threading model: :func:`dispatch_to_thread` is an async function; it
runs on the event loop. The ``record_to_thread_dispatch`` mutation
runs on the event loop side BEFORE the worker thread is dispatched, so
the registry's lock is never contended cross-thread on this path.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from sovyx.observability._resource_registry import record_to_thread_dispatch
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _introspect_default_executor(
    loop: asyncio.AbstractEventLoop,
) -> tuple[int, int, int]:
    """Return ``(worker_count, queue_depth, max_workers)`` for the loop's default executor.

    The default executor is a :class:`ThreadPoolExecutor` created lazily
    on first ``run_in_executor(None, ...)``. Pre-creation the tuple is
    ``(0, 0, 0)``. Post-creation the introspection reads:

    * ``len(executor._threads)`` — current live worker count.
    * ``executor._work_queue.qsize()`` — pending submissions in the
      executor's internal queue.
    * ``executor._max_workers`` — the configured cap (Python 3.12+
      default: ``min(32, os.cpu_count() + 4)``).

    These private attributes are stable across CPython 3.8+ but
    observability-only; failures fall back to ``(0, 0, 0)`` and
    surface a DEBUG record.
    """
    executor = getattr(loop, "_default_executor", None)
    if executor is None:
        return 0, 0, 0
    if not isinstance(executor, ThreadPoolExecutor):
        # An operator may have called ``loop.set_default_executor(...)``
        # with a custom executor that doesn't expose the standard
        # private attributes. Return zeros — observability gracefully
        # degrades; the call still succeeds.
        return 0, 0, 0
    try:
        worker_count = len(executor._threads)  # noqa: SLF001 — documented private API.
        queue_depth = executor._work_queue.qsize()  # noqa: SLF001
        max_workers = executor._max_workers  # noqa: SLF001
    except (AttributeError, RuntimeError) as exc:
        logger.debug(
            "h4.thread_dispatch.executor_introspection_failed",
            exc_type=type(exc).__name__,
        )
        return 0, 0, 0
    return worker_count, queue_depth, max_workers


async def dispatch_to_thread(
    label: str,
    fn: Callable[_P, _R],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _R:
    """Labelled :func:`asyncio.to_thread` 1:1 drop-in.

    Args:
        label: Stable, low-cardinality identifier for the cohort this
            dispatch belongs to. Conventional shape:
            ``"<subsystem>.<operation>"`` (e.g. ``"voice.vad.infer"``,
            ``"brain.embedding.infer"``). Quality Gate 15 enforces a
            non-empty literal at every call site.
        fn: The synchronous callable to run on a worker thread.
        *args, **kwargs: Forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns. Exceptions propagate identically to
        :func:`asyncio.to_thread`.

    Side-effects:
        Records the dispatch on the default :class:`ResourceRegistry`
        for visibility on ``self.health.snapshot``.

    Notes:
        Does NOT change the loop's default executor; uses
        ``loop.run_in_executor(None, ...)`` exactly as
        :func:`asyncio.to_thread` does. Operators who set a custom
        default executor at bootstrap time keep that override intact.
    """
    loop = asyncio.get_running_loop()
    worker_count, queue_depth, max_workers = _introspect_default_executor(loop)
    record_to_thread_dispatch(
        label=label,
        worker_count_at_dispatch=worker_count,
        queue_depth=queue_depth,
        max_workers=max_workers,
    )
    bound = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(None, bound)


__all__ = ["dispatch_to_thread"]
