"""Sovyx CogLoopGate — serialize requests to the cognitive loop.

INT-001: PriorityQueue + single worker pattern.
Multiple channels submit requests, gate serializes processing.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
from typing import TYPE_CHECKING

from sovyx.engine.errors import CognitiveError
from sovyx.observability.logging import bind_request_context, clear_request_context, get_logger
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActionResult
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConversationId, MindId

logger = get_logger(__name__)


@dataclasses.dataclass
class CognitiveRequest:
    """Bundle of data needed to process a perception.

    The Gate is the boundary between Bridge and Cognitive.
    BridgeManager builds CognitiveRequest with ALL needed data.
    """

    perception: Perception
    mind_id: MindId
    conversation_id: ConversationId
    conversation_history: list[dict[str, str]]
    person_name: str | None = None


class CogLoopGate:
    """Serialize requests to CognitiveLoop via PriorityQueue.

    - PriorityQueue(maxsize=10) with backpressure
    - Single worker drains sequentially
    - asyncio.Future per request — caller awaits with timeout
    """

    def __init__(self, cognitive_loop: CognitiveLoop) -> None:
        self._loop = cognitive_loop
        self._queue: asyncio.PriorityQueue[
            tuple[int, int, CognitiveRequest, asyncio.Future[ActionResult]]
        ] = asyncio.PriorityQueue(maxsize=10)
        self._counter = itertools.count()
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        # Mission C6 §T4.2 — dependency-ready event (anti-pattern #44).
        # Initially SET — the worker drains the queue normally. Cleared
        # by `set_dependency_ready(False)` when the LLM router or brain
        # signals a missing dependency; the worker pauses awaiting the
        # event AND emits a throttled `cognitive.loop.gate.dependency_
        # check_failed` WARN at ≤ 1/min cadence to surface the pending
        # request count without log spam.
        self._dependency_ready_event = asyncio.Event()
        self._dependency_ready_event.set()
        # Last time the throttled WARN was emitted; monotonic seconds.
        self._last_throttled_warn_at_monotonic: float = 0.0
        self._throttle_min_interval_s: float = 60.0

    def set_dependency_ready(self, ready: bool) -> None:
        """Toggle the dependency-ready signal (Mission C6 §T4.2).

        Called by the liveness probe (or any future producer) when the
        cognitive loop's dependency state transitions. The gate worker
        pauses on the cleared event so it stops pulling requests off the
        queue until recovery — eliminates the per-request short-circuit
        cost when dependencies are absent for sustained periods.

        Idempotent — set->set or clear->clear is a no-op.
        """
        if ready:
            if not self._dependency_ready_event.is_set():
                self._dependency_ready_event.set()
                logger.info("cognitive.loop.dependency_recovered")
        elif self._dependency_ready_event.is_set():
            self._dependency_ready_event.clear()

    async def submit(
        self,
        request: CognitiveRequest,
        timeout: float = 30.0,
    ) -> ActionResult:
        """Submit a request and wait for result.

        Args:
            request: CognitiveRequest bundle.
            timeout: Max wait time in seconds.

        Returns:
            ActionResult from the cognitive loop.

        Raises:
            CognitiveError: On timeout or queue full.
        """
        future: asyncio.Future[ActionResult] = asyncio.get_running_loop().create_future()
        item = (
            request.perception.priority,
            next(self._counter),
            request,
            future,
        )

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            msg = "Cognitive loop queue full (backpressure)"
            raise CognitiveError(msg) from None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            msg = f"Cognitive loop timed out after {timeout}s"
            raise CognitiveError(msg) from None

    async def start(self) -> None:
        """Start background worker."""
        self._running = True
        self._worker_task = spawn(self._worker(), name="cognitive-gate-worker")
        logger.info("cogloop_gate_started")

    def _maybe_emit_throttled_dep_warn(self) -> None:
        """Emit the dependency-check-failed WARN at most once per minute.

        Throttled to ≤ 1/min so a sustained degraded state doesn't flood
        the log file (sibling of anti-pattern #7 observability hygiene
        from Mission C3 §T2.7).
        """
        import time

        now = time.monotonic()
        # c4-allowlist: dependency-check WARN is observability of the consumer side; the producing axis already records to the composite store.  # noqa: E501
        if now - self._last_throttled_warn_at_monotonic >= self._throttle_min_interval_s:
            self._last_throttled_warn_at_monotonic = now
            logger.warning(
                "cognitive.loop.gate.dependency_check_failed",
                missing_dependencies=list(
                    getattr(self._loop, "_missing_dependencies", ()),
                ),
                pending_requests_count=self._queue.qsize(),
            )

    async def stop(self) -> None:
        """Stop worker, drain pending."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        # Drain pending with errors
        while not self._queue.empty():
            try:
                _, _, _, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(CognitiveError("Gate shutting down"))
            except asyncio.QueueEmpty:
                break
        logger.info("cogloop_gate_stopped")

    async def _worker(self) -> None:
        """Single worker draining the queue.

        Binds request-scoped logging context (mind_id, conversation_id,
        request_id) before processing each request, so every log emitted
        during the cognitive loop carries full tracing context.

        Mission C6 §T4.2 — when ``_dependency_ready_event`` is cleared,
        the worker pauses awaiting the event AND emits a throttled
        ``cognitive.loop.gate.dependency_check_failed`` WARN at most
        once per minute, including the pending-request count for triage.
        Recovers automatically when the liveness probe re-sets the event.
        """
        while self._running:
            try:
                # Anti-pattern #44 — gate every iteration on the
                # dependency-ready signal. ``wait_for`` bounds the wait
                # so the loop remains responsive to cancellation.
                if not self._dependency_ready_event.is_set():
                    self._maybe_emit_throttled_dep_warn()
                    try:
                        await asyncio.wait_for(
                            self._dependency_ready_event.wait(),
                            timeout=1.0,
                        )
                    except TimeoutError:
                        continue
                priority, _, request, future = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except (TimeoutError, asyncio.CancelledError):
                continue

            # Bind structured context for the lifetime of this request
            clear_request_context()
            bind_request_context(
                mind_id=str(request.mind_id),
                conversation_id=str(request.conversation_id),
            )
            try:
                result = await self._loop.process_request(request)
                if not future.done():
                    future.set_result(result)
            except Exception as e:  # noqa: BLE001 — relays exception to awaiter via future.set_exception
                if not future.done():
                    future.set_exception(e)
            finally:
                clear_request_context()
