"""Unit tests for Mission H4 §T1.2 — ``dispatch_to_thread`` wrapper."""

from __future__ import annotations

import asyncio

import pytest

from sovyx.observability._resource_cohort_governor import (
    get_default_resource_cohort_governor,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import (
    CohortAxis,
    get_default_resource_registry,
    reset_default_resource_registry,
)
from sovyx.observability._thread_dispatch import (
    CohortBreakerEngagedError,
    _introspect_default_executor,
    dispatch_to_thread,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_resource_registry()
    reset_default_resource_cohort_governor()
    yield
    reset_default_resource_registry()
    reset_default_resource_cohort_governor()


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


class TestF7CircuitBreakerEnforcement:
    """Mission H4 §3 F7 + v0.49.29 — dispatch_to_thread consults cohort
    breakers and refuses work when any is engaged.
    """

    @pytest.mark.asyncio()
    async def test_dispatch_succeeds_when_no_breaker_engaged(self) -> None:
        """Baseline: governor singleton has no engaged breakers → dispatch proceeds."""

        def add(a: int, b: int) -> int:
            return a + b

        result = await dispatch_to_thread("test.add", add, 2, 3)
        assert result == 5

    @pytest.mark.asyncio()
    async def test_dispatch_refused_when_thread_count_breaker_engaged(self) -> None:
        """F7: 3 breaches engage breaker → dispatch raises CohortBreakerEngagedError."""
        governor = get_default_resource_cohort_governor()
        # Engage the THREAD_COUNT breaker (3 breaches within window).
        for _ in range(3):
            governor.record_breach(CohortAxis.THREAD_COUNT)
        assert governor.is_breaker_engaged(CohortAxis.THREAD_COUNT) is True

        def add(a: int, b: int) -> int:
            return a + b

        with pytest.raises(CohortBreakerEngagedError) as exc_info:
            await dispatch_to_thread("test.add", add, 2, 3)
        assert exc_info.value.axis == CohortAxis.THREAD_COUNT
        assert exc_info.value.label == "test.add"
        # Error message carries the ack hint.
        assert "/api/engine/resources/cohort/ack" in str(exc_info.value)

    @pytest.mark.asyncio()
    async def test_dispatch_proceeds_after_ack_clears_breaker(self) -> None:
        """F7: operator-acked breaker → subsequent dispatch proceeds."""
        governor = get_default_resource_cohort_governor()
        for _ in range(3):
            governor.record_breach(CohortAxis.RSS_GROWTH)
        assert governor.is_breaker_engaged(CohortAxis.RSS_GROWTH) is True
        # Operator acks via clear_breaker.
        governor.clear_breaker(CohortAxis.RSS_GROWTH)
        assert governor.is_breaker_engaged(CohortAxis.RSS_GROWTH) is False

        def double(x: int) -> int:
            return x * 2

        result = await dispatch_to_thread("test.double", double, 7)
        assert result == 14

    @pytest.mark.asyncio()
    async def test_dispatch_refused_when_any_axis_breaker_engaged(self) -> None:
        """F7: ANY engaged breaker (not just THREAD_COUNT) refuses dispatch.

        Conservative interpretation — when the governor observes ANY
        cohort under sustained distress (3+ breaches), additional
        thread spawning pauses until the operator acks.
        """
        governor = get_default_resource_cohort_governor()
        for _ in range(3):
            governor.record_breach(CohortAxis.ONNX_SESSION)

        def noop() -> int:
            return 42

        with pytest.raises(CohortBreakerEngagedError) as exc_info:
            await dispatch_to_thread("test.noop", noop)
        assert exc_info.value.axis == CohortAxis.ONNX_SESSION
