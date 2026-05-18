"""Unit tests — `CogLoopGate` dependency-ready-event worker pause (Mission C6 §T4.2).

Coverage:
* ``set_dependency_ready(False)`` clears the event; worker pauses on
  ``event.wait()`` instead of pulling requests.
* ``set_dependency_ready(True)`` re-sets the event; worker drains
  pending requests.
* Idempotency: set->set and clear->clear are no-ops.
* Throttled WARN: ``cognitive.loop.gate.dependency_check_failed`` fires
  at most once per minute.
* Recovery: ``cognitive.loop.dependency_recovered`` fires once on
  clear->set transition.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.cognitive.gate import CogLoopGate


def _make_gate() -> CogLoopGate:
    cognitive_loop = MagicMock()
    cognitive_loop._missing_dependencies = ()
    cognitive_loop.process_request = AsyncMock(return_value=MagicMock())
    return CogLoopGate(cognitive_loop)


class TestSetDependencyReady:
    def test_default_event_is_set(self) -> None:
        gate = _make_gate()
        assert gate._dependency_ready_event.is_set()

    def test_set_ready_false_clears_event(self) -> None:
        gate = _make_gate()
        gate.set_dependency_ready(False)
        assert not gate._dependency_ready_event.is_set()

    def test_set_ready_true_after_false_sets_event(self) -> None:
        gate = _make_gate()
        gate.set_dependency_ready(False)
        gate.set_dependency_ready(True)
        assert gate._dependency_ready_event.is_set()

    def test_set_ready_idempotent_true(self) -> None:
        gate = _make_gate()
        # Default: True. Calling True again is no-op.
        gate.set_dependency_ready(True)
        assert gate._dependency_ready_event.is_set()

    def test_set_ready_idempotent_false(self) -> None:
        gate = _make_gate()
        gate.set_dependency_ready(False)
        gate.set_dependency_ready(False)
        assert not gate._dependency_ready_event.is_set()


class TestRecoveryLogEvent:
    def test_recovery_fires_once_on_clear_to_set(self) -> None:
        gate = _make_gate()
        gate.set_dependency_ready(False)
        with patch("sovyx.cognitive.gate.logger") as mock_logger:
            gate.set_dependency_ready(True)
        recovery_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c[0][0] == "cognitive.loop.dependency_recovered"
        ]
        assert len(recovery_calls) == 1

    def test_recovery_does_not_fire_on_idempotent_set(self) -> None:
        gate = _make_gate()
        # Already-set → True again. No recovery event.
        with patch("sovyx.cognitive.gate.logger") as mock_logger:
            gate.set_dependency_ready(True)
        recovery_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c[0][0] == "cognitive.loop.dependency_recovered"
        ]
        assert len(recovery_calls) == 0


class TestThrottledWarn:
    def test_first_warn_emits(self) -> None:
        gate = _make_gate()
        gate._loop._missing_dependencies = ("llm_router_no_available_provider",)
        with patch("sovyx.cognitive.gate.logger") as mock_logger:
            gate._maybe_emit_throttled_dep_warn()
        warn_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c[0][0] == "cognitive.loop.gate.dependency_check_failed"
        ]
        assert len(warn_calls) == 1

    def test_second_warn_within_throttle_window_suppressed(self) -> None:
        gate = _make_gate()
        gate._loop._missing_dependencies = ("llm_router_no_available_provider",)
        with patch("sovyx.cognitive.gate.logger") as mock_logger:
            gate._maybe_emit_throttled_dep_warn()
            gate._maybe_emit_throttled_dep_warn()
            gate._maybe_emit_throttled_dep_warn()
        warn_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c[0][0] == "cognitive.loop.gate.dependency_check_failed"
        ]
        # Exactly one — the second + third were throttled
        assert len(warn_calls) == 1

    def test_warn_re_emits_after_throttle_window(self) -> None:
        gate = _make_gate()
        gate._loop._missing_dependencies = ("llm_router_no_available_provider",)
        # Shrink the throttle for the test
        gate._throttle_min_interval_s = 0.0
        with patch("sovyx.cognitive.gate.logger") as mock_logger:
            gate._maybe_emit_throttled_dep_warn()
            gate._maybe_emit_throttled_dep_warn()
        warn_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c[0][0] == "cognitive.loop.gate.dependency_check_failed"
        ]
        # Throttle is 0 → both emit
        assert len(warn_calls) == 2


class TestWorkerPauseOnClearedEvent:
    @pytest.mark.asyncio
    async def test_worker_pauses_when_event_cleared(self) -> None:
        """The worker MUST NOT drain the queue when dependency_ready is False."""
        gate = _make_gate()
        gate.set_dependency_ready(False)
        # Manually run one iteration of the worker — should NOT pull from queue.
        # Start worker briefly, then stop.
        gate._running = True
        worker_task = asyncio.create_task(gate._worker())
        # Submit a request while paused — it should sit in the queue
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await gate._queue.put((1, 0, MagicMock(), future))
        # Yield briefly so the worker iterates
        await asyncio.sleep(0.3)
        # process_request must NOT have been called
        gate._loop.process_request.assert_not_called()
        # Future is still pending
        assert not future.done()
        gate._running = False
        worker_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    @pytest.mark.asyncio
    async def test_worker_resumes_when_event_set(self) -> None:
        """Setting dependency_ready=True must unblock the worker."""
        gate = _make_gate()
        gate.set_dependency_ready(False)
        gate._running = True
        worker_task = asyncio.create_task(gate._worker())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await gate._queue.put((1, 0, MagicMock(), future))
        await asyncio.sleep(0.2)
        # Still paused
        gate._loop.process_request.assert_not_called()
        # Re-set the event
        gate.set_dependency_ready(True)
        # Yield so worker drains
        await asyncio.sleep(0.5)
        # Worker should have processed at least once
        assert gate._loop.process_request.call_count >= 1
        gate._running = False
        worker_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
