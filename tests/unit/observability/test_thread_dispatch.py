"""Unit tests for Mission H4 §T1.2 — ``dispatch_to_thread`` wrapper."""

from __future__ import annotations

import asyncio

import pytest

from sovyx.observability._resource_registry import (
    get_default_resource_registry,
    reset_default_resource_registry,
)
from sovyx.observability._thread_dispatch import (
    _introspect_default_executor,
    dispatch_to_thread,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


# ── Functional behaviour (1:1 with asyncio.to_thread) ──


class TestFunctionalParityWithAsyncioToThread:
    @pytest.mark.asyncio()
    async def test_returns_fn_result(self) -> None:
        def square(x: int) -> int:
            return x * x

        result = await dispatch_to_thread("test.square", square, 7)
        assert result == 49

    @pytest.mark.asyncio()
    async def test_propagates_exceptions(self) -> None:
        def boom() -> None:
            msg = "expected"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="expected"):
            await dispatch_to_thread("test.boom", boom)

    @pytest.mark.asyncio()
    async def test_forwards_args_and_kwargs(self) -> None:
        def combine(a: int, b: int, *, sep: str = ",") -> str:
            return f"{a}{sep}{b}"

        result = await dispatch_to_thread("test.combine", combine, 1, 2, sep="-")
        assert result == "1-2"


# ── Registry instrumentation side-effect ──


class TestDispatchRecordsRegistry:
    @pytest.mark.asyncio()
    async def test_dispatch_records_label(self) -> None:
        await dispatch_to_thread("voice.vad.infer", lambda: None)
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["to_thread.dispatch_count_total"] == 1
        assert fields["to_thread.dispatch_count_per_label"] == {"voice.vad.infer": 1}

    @pytest.mark.asyncio()
    async def test_multiple_dispatches_accumulate(self) -> None:
        for _ in range(3):
            await dispatch_to_thread("brain.embedding.infer", lambda: 0)
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["to_thread.dispatch_count_total"] == 3

    @pytest.mark.asyncio()
    async def test_distinct_labels_split(self) -> None:
        await dispatch_to_thread("a", lambda: None)
        await dispatch_to_thread("b", lambda: None)
        await dispatch_to_thread("a", lambda: None)
        fields = get_default_resource_registry().snapshot_fields()
        assert fields["to_thread.dispatch_count_per_label"] == {"a": 2, "b": 1}


# ── Default executor introspection (private-API observability) ──


class TestDefaultExecutorIntrospection:
    @pytest.mark.asyncio()
    async def test_pre_executor_creation_returns_zeros(self) -> None:
        loop = asyncio.get_running_loop()
        # Ensure the default executor is not yet created.
        loop._default_executor = None  # type: ignore[attr-defined]
        worker_count, queue_depth, max_workers = _introspect_default_executor(loop)
        assert worker_count == 0
        assert queue_depth == 0
        assert max_workers == 0

    @pytest.mark.asyncio()
    async def test_post_executor_creation_returns_valid_state(self) -> None:
        # Trigger executor creation by running a no-op via the wrapper.
        await dispatch_to_thread("priming", lambda: None)
        loop = asyncio.get_running_loop()
        worker_count, queue_depth, max_workers = _introspect_default_executor(loop)
        # max_workers is always positive when the executor exists.
        assert max_workers > 0
        # Worker count is in [0, max_workers]; queue_depth is non-negative.
        assert 0 <= worker_count <= max_workers
        assert queue_depth >= 0

    @pytest.mark.asyncio()
    async def test_custom_executor_does_not_break(self) -> None:
        """If the operator installs a non-ThreadPoolExecutor default, introspection degrades."""

        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):  # noqa: ANN001 — minimal stub
                fut = asyncio.Future()
                fut.set_result(fn(*args, **kwargs))
                return fut

            def shutdown(self, *, wait: bool = True) -> None:  # noqa: ARG002
                pass

        loop = asyncio.get_running_loop()
        # We don't actually swap in the fake (the real executor needs to
        # be ThreadPoolExecutor for run_in_executor to behave). Instead
        # we just call introspection against a synthetic loop attribute.
        loop._default_executor = _FakeExecutor()  # type: ignore[attr-defined]
        worker_count, queue_depth, max_workers = _introspect_default_executor(loop)
        assert (worker_count, queue_depth, max_workers) == (0, 0, 0)
        # Restore None so other tests aren't disturbed.
        loop._default_executor = None  # type: ignore[attr-defined]
