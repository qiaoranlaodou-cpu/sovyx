"""Tests for :meth:`SessionRegistry.acquire_exclusive`.

v0.38.0 / F2-H01 closure — verifies the exclusive lock contract that
fences live VU subscribes during a recorder window. See audit §3.C.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from sovyx.voice.device_test._protocol import CloseReason
from sovyx.voice.device_test._session import SessionRegistry


class TestAcquireExclusiveLockSemantics:
    """Lock acquisition + release contract."""

    @pytest.mark.asyncio
    async def test_yields_when_lock_is_free(self) -> None:
        """Happy path — lock free, context manager enters and exits."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        assert not registry.exclusive_lock.locked()

        async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
            # Lock observable as held while the caller runs.
            assert registry.exclusive_lock.locked()

        # Released cleanly on exit.
        assert not registry.exclusive_lock.locked()

    @pytest.mark.asyncio
    async def test_calls_close_all_before_yielding(self) -> None:
        """``close_all`` runs INSIDE the lock, BEFORE caller body."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        order: list[str] = []
        # Replace ``close_all`` with a tracked AsyncMock — anti-pattern #36
        # autodetect makes the awaitable contract correct.
        original_close_all = registry.close_all

        async def _tracked_close_all(*, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
            order.append("close_all")
            await original_close_all(reason=reason)

        registry.close_all = _tracked_close_all  # type: ignore[method-assign]

        async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
            order.append("body")

        assert order == ["close_all", "body"]

    @pytest.mark.asyncio
    async def test_releases_lock_on_caller_exception(self) -> None:
        """Caller raising INSIDE the body still releases the lock."""
        registry = SessionRegistry(force_close_grace_s=0.05)

        with pytest.raises(RuntimeError, match="boom"):
            async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
                msg = "boom"
                raise RuntimeError(msg)

        assert not registry.exclusive_lock.locked()

    @pytest.mark.asyncio
    async def test_concurrent_callers_serialise(self) -> None:
        """Two callers contending for the lock run sequentially.

        The second caller must NOT enter the body until the first
        releases. This is the core invariant the wizard relies on.
        """
        registry = SessionRegistry(force_close_grace_s=0.05)
        events: list[str] = []
        first_inside = asyncio.Event()
        first_release = asyncio.Event()

        async def _first() -> None:
            async with registry.acquire_exclusive(role="first", ttl_s=2.0):
                events.append("first_enter")
                first_inside.set()
                await first_release.wait()
                events.append("first_exit")

        async def _second() -> None:
            await first_inside.wait()
            async with registry.acquire_exclusive(role="second", ttl_s=2.0):
                events.append("second_enter")

        first_task = asyncio.create_task(_first())
        second_task = asyncio.create_task(_second())

        await first_inside.wait()
        # Give the second task a chance to attempt acquiring — it must
        # block on the lock.
        await asyncio.sleep(0.05)
        assert events == ["first_enter"], "second caller entered before first released"

        first_release.set()
        await asyncio.gather(first_task, second_task)
        assert events == ["first_enter", "first_exit", "second_enter"]


class TestAcquireExclusiveTimeout:
    """Defensive ttl ceiling on the acquire path."""

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_timeout(self) -> None:
        """Lock held by another holder past ttl + 1.0 → RuntimeError."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        # Pre-acquire the lock manually so the test caller hits the
        # wait_for ceiling without spawning a parallel task.
        await registry.exclusive_lock.acquire()
        try:
            with pytest.raises(RuntimeError, match="role='probe'"):
                async with registry.acquire_exclusive(role="probe", ttl_s=0.05):
                    pytest.fail("should not have entered the body")
        finally:
            registry.exclusive_lock.release()
        assert not registry.exclusive_lock.locked()


class TestAcquireExclusiveCallerLifecycle:
    """The lock survives the caller's full critical section, not just close_all."""

    @pytest.mark.asyncio
    async def test_lock_remains_held_across_body_awaits(self) -> None:
        """Awaiting INSIDE the body keeps the lock held the whole time."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        observed: list[bool] = []

        async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
            observed.append(registry.exclusive_lock.locked())
            await asyncio.sleep(0)
            observed.append(registry.exclusive_lock.locked())
            await asyncio.sleep(0)
            observed.append(registry.exclusive_lock.locked())

        assert observed == [True, True, True]
        assert not registry.exclusive_lock.locked()

    @pytest.mark.asyncio
    async def test_close_all_invocation_uses_async_mock_pattern(self) -> None:
        """Sanity: AsyncMock substitution preserves the await contract."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        mock_close_all = AsyncMock(return_value=None)
        registry.close_all = mock_close_all  # type: ignore[method-assign]

        async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
            # close_all called exactly once, awaited correctly.
            mock_close_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_all_failure_propagates_and_releases_lock(self) -> None:
        """If close_all raises, the lock still releases (finally branch)."""
        registry = SessionRegistry(force_close_grace_s=0.05)

        async def _failing_close_all(*, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
            del reason
            msg = "close_all blew up"
            raise RuntimeError(msg)

        registry.close_all = _failing_close_all  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="close_all blew up"):
            async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
                pytest.fail("body should not run if close_all raises")

        assert not registry.exclusive_lock.locked()


class TestAcquireExclusiveLockedObservable:
    """``exclusive_lock.locked()`` is observable from external callers."""

    @pytest.mark.asyncio
    async def test_observable_for_ws_reject_path(self) -> None:
        """A would-be VU subscriber polling locked() sees True mid-window."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        observed_during: bool | None = None
        observed_after: bool | None = None
        body_started = asyncio.Event()
        body_release = asyncio.Event()

        async def _holder() -> None:
            async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=1.0):
                body_started.set()
                await body_release.wait()

        async def _observer() -> None:
            nonlocal observed_during, observed_after
            await body_started.wait()
            observed_during = registry.exclusive_lock.locked()
            body_release.set()
            # Yield so the holder's finally block runs.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            observed_after = registry.exclusive_lock.locked()

        await asyncio.gather(_holder(), _observer())

        assert observed_during is True
        assert observed_after is False


class TestAcquireExclusiveCancellation:
    """Cancellation of the holder still releases the lock."""

    @pytest.mark.asyncio
    async def test_cancelled_holder_releases_lock(self) -> None:
        """Cancelling the task inside the body releases the lock."""
        registry = SessionRegistry(force_close_grace_s=0.05)
        body_started = asyncio.Event()

        async def _holder() -> None:
            async with registry.acquire_exclusive(role="wizard_test_record", ttl_s=2.0):
                body_started.set()
                await asyncio.sleep(60.0)

        task = asyncio.create_task(_holder())
        await body_started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert not registry.exclusive_lock.locked()
