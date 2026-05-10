"""F2-M01 — captured-frame buffer must be thread-safe.

Pre-fix `voice_wizard.py:_record_async` mutated a `list[np.ndarray]`
from two threads concurrently:

* PortAudio's driver thread, calling the registered callback every
  blocksize samples (~32 ms at 16 kHz / 512 frames).
* The main coroutine's event-loop thread, sleeping for `duration_s`
  then concatenating the buffer.

Python `list.append` is atomic at the bytecode level, but the backing
array's *resize* operation is not. A resize crossing concurrently with
a draining iteration can corrupt the array or crash the interpreter
under PyPy / sub-interpreter / free-threaded builds. The probability
on CPython 3.12 with the GIL is low, but the contract — "writes from
arbitrary threads, reads from arbitrary threads" — is exactly what
`queue.Queue` guarantees and `list` explicitly does not.

Post-fix the producer enqueues into a `queue.Queue[np.ndarray]`; the
main coroutine drains the queue after `stream.stop()` (no more
producers). This test asserts:

1. The queue accepts 1000 concurrent producer-thread put_nowaits
   without dropping or duplicating frames.
2. The drain pattern used by `_record_async` recovers exactly the
   producer-side count.

This test does NOT exercise `_record_async` end-to-end (that requires
sounddevice + a microphone); it asserts the primitive's contract under
the same load shape the callback subjects it to.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

_FRAMES_PER_PRODUCER = 1_000
_PRODUCERS = 4


class TestCapturedFrameQueueIsThreadSafe:
    """Concurrent put_nowait + post-stop drain matches the producer count."""

    def test_concurrent_puts_match_drain_count(self) -> None:
        captured_q: queue.Queue[np.ndarray] = queue.Queue()
        # Barrier so all producer threads release at the same moment —
        # maximises the chance of resize-boundary contention if the
        # implementation regressed to list.
        barrier = threading.Barrier(_PRODUCERS)

        def _producer(producer_id: int) -> None:
            barrier.wait()
            for frame_idx in range(_FRAMES_PER_PRODUCER):
                # Mark each frame so we can verify ordering didn't
                # silently corrupt the data.
                payload = np.full(
                    8,
                    producer_id * _FRAMES_PER_PRODUCER + frame_idx,
                    dtype=np.int16,
                )
                captured_q.put_nowait(payload)

        with ThreadPoolExecutor(max_workers=_PRODUCERS) as pool:
            futures = [pool.submit(_producer, pid) for pid in range(_PRODUCERS)]
            for fut in futures:
                fut.result(timeout=5.0)

        # Mirror the post-stop drain in _record_async exactly.
        captured_frames: list[np.ndarray] = []
        while True:
            try:
                captured_frames.append(captured_q.get_nowait())
            except queue.Empty:
                break

        # Exact-count contract: every producer-side put must be drained
        # exactly once. No drops, no duplicates.
        expected = _PRODUCERS * _FRAMES_PER_PRODUCER
        assert len(captured_frames) == expected, (
            f"frame count drift: expected {expected}, got {len(captured_frames)}"
        )

        # Payload integrity: union of (producer_id * N + frame_idx)
        # markers must equal the cartesian-product set. If any frame
        # was corrupted by a concurrent resize, its marker would be
        # garbage.
        observed_markers = {int(frame[0]) for frame in captured_frames}
        expected_markers = set(range(_PRODUCERS * _FRAMES_PER_PRODUCER))
        assert observed_markers == expected_markers, (
            f"frame payload corruption: missing="
            f"{sorted(expected_markers - observed_markers)[:10]}, "
            f"extra={sorted(observed_markers - expected_markers)[:10]}"
        )

    def test_drain_on_empty_queue_is_noop(self) -> None:
        captured_q: queue.Queue[np.ndarray] = queue.Queue()
        captured_frames: list[np.ndarray] = []
        while True:
            try:
                captured_frames.append(captured_q.get_nowait())
            except queue.Empty:
                break
        assert captured_frames == []

    def test_drain_recovers_single_producer_in_order(self) -> None:
        # FIFO contract: when only one producer puts, the drain order
        # must equal the put order. Validates the numpy-on-Queue
        # round-trip preserves shape / dtype / values.
        captured_q: queue.Queue[np.ndarray] = queue.Queue()
        for i in range(64):
            captured_q.put_nowait(np.array([i, i + 1, i + 2], dtype=np.int16))

        captured_frames: list[np.ndarray] = []
        while True:
            try:
                captured_frames.append(captured_q.get_nowait())
            except queue.Empty:
                break

        assert len(captured_frames) == 64
        for i, frame in enumerate(captured_frames):
            assert frame.dtype == np.int16
            np.testing.assert_array_equal(frame, np.array([i, i + 1, i + 2]))


class TestCapturedFrameQueueImplementationContract:
    """Source-level contract: voice_wizard does NOT use list for capture."""

    def test_record_async_uses_queue_not_list(self) -> None:
        # Cheap structural assertion: regression would re-introduce the
        # list-based pattern. A grep-style check is faster + more
        # deterministic than reproducing the PortAudio race.
        import inspect

        from sovyx.dashboard.routes import voice_wizard

        source = inspect.getsource(
            voice_wizard.SoundDeviceWizardRecorder._record_async,  # noqa: SLF001
        )
        assert "queue.Queue" in source, (
            "_record_async must capture frames into a queue.Queue "
            "(thread-safe FIFO). Regression of F2-M01."
        )
        # Also forbid the pre-fix pattern.
        assert "captured_frames.append(np.asarray" not in source, (
            "_record_async appears to mutate captured_frames from the "
            "PortAudio callback thread again — this is the F2-M01 bug."
        )
