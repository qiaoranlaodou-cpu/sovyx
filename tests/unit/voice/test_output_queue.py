"""Tests for the T1.2 (CRITICAL CR2) interruption-aware play_immediate.

Pre-T1.2 behaviour: ``play_immediate`` called ``OutputStream.write(buffer)``
once via ``asyncio.to_thread``. PortAudio blocked until the entire chunk
was consumed (5+ s on long sentences). The ``interrupt()`` flag was set
but never read, so operator barge-in didn't actually stop the audio
until the sentence finished.

Post-T1.2 contract:

* ``_play_audio`` slices ``chunk.audio`` into ``_PLAYBACK_SLICE_MS``-ms
  pieces and dispatches each piece via :func:`asyncio.to_thread`.
* Between slices, the calling queue's ``_interrupted`` flag is polled
  (sourced from the ``_active_queue`` contextvar set by
  ``play_immediate`` / ``drain``).
* When the flag flips True, the loop returns *before* dispatching the
  next slice — bounding worst-case barge-in latency to ~one slice plus
  thread-pool overhead.
* The ``drain`` path's existing per-chunk interrupt check is preserved
  (this is a sanity test that the contextvar wiring did not regress
  the old per-chunk loop).
* The headless / no-PortAudio simulation branches mirror the slice
  loop semantics so unit tests on CI runners observe the same
  interrupt latency as production hosts.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from sovyx.voice.pipeline import _output_queue as _output_queue_mod
from sovyx.voice.pipeline._output_queue import (
    _PLAYBACK_SLICE_MS,
    AudioOutputQueue,
    _slice_audio,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# ── Helpers ──────────────────────────────────────────────────────────────


def _silent_chunk(*, duration_ms: float, sample_rate: int = 22_050) -> object:
    """Build an AudioChunk-like duck-typed value of the requested duration.

    Mirrors the helper in test_output_queue_aec_wireup.py so tests stay
    free of TTS engine deps while still exercising the real
    AudioOutputQueue path.
    """
    n_samples = int(round(sample_rate * duration_ms / 1000))

    class _Chunk:
        def __init__(self) -> None:
            self.audio = np.zeros(n_samples, dtype=np.int16)
            self.sample_rate = sample_rate
            self.duration_ms = duration_ms

    return _Chunk()


# ── _slice_audio unit-level ─────────────────────────────────────────────


class TestSliceAudio:
    """Verify slice math on the helper directly so the loop in
    ``_play_audio`` has a known-good slicer."""

    def test_evenly_divisible_buffer_yields_exact_count(self) -> None:
        # 100 ms @ 1000 Hz = 100 samples. Slice 50 ms = 50 samples each.
        buf = np.arange(100, dtype=np.int16)
        slices = _slice_audio(buf, sample_rate=1000, slice_ms=50)
        assert len(slices) == 2  # noqa: PLR2004 — slice count pin
        assert all(len(s) == 50 for s in slices)  # type: ignore[arg-type]  # noqa: PLR2004

    def test_uneven_remainder_yields_short_final_slice(self) -> None:
        # 110 samples @ 1000 Hz, slice 50 ms (50 samples). Expect [50, 50, 10].
        buf = np.arange(110, dtype=np.int16)
        slices = _slice_audio(buf, sample_rate=1000, slice_ms=50)
        assert [len(s) for s in slices] == [50, 50, 10]  # type: ignore[arg-type]

    def test_empty_buffer_collapses_to_single_iteration(self) -> None:
        buf = np.zeros(0, dtype=np.int16)
        slices = _slice_audio(buf, sample_rate=22_050, slice_ms=50)
        # Single iteration so the headless / simulate path always runs at
        # least once and "0 ms duration" callers do not silently bypass
        # the loop.
        assert len(slices) == 1

    def test_zero_slice_ms_collapses_to_single_iteration(self) -> None:
        buf = np.arange(100, dtype=np.int16)
        slices = _slice_audio(buf, sample_rate=22_050, slice_ms=0)
        # Defensive against config / tuning that drops slice_ms to 0.
        assert len(slices) == 1


# ── play_immediate interruption ─────────────────────────────────────────


class TestPlayImmediateInterruption:
    """Pin the T1.2 contract: ``play_immediate`` must abandon a chunk
    mid-playback when ``interrupt()`` is called."""

    @pytest.mark.asyncio()
    async def test_play_immediate_interrupted_stops_within_target_latency(
        self,
    ) -> None:
        """Barge-in mid-playback must short-circuit within ~150 ms.

        Constructs a 5-second silent chunk that, on the production path,
        would need 5 seconds of PortAudio time. We mock
        ``blocking_write_play`` to introduce a per-slice delay so the
        real slice-loop in ``_play_audio`` runs and we can observe its
        interrupt-poll cadence. The total wall-clock time MUST stay
        well under the chunk duration (one slice latency + scheduler
        slack) — pre-T1.2 it would equal the chunk duration.
        """
        q = AudioOutputQueue()
        chunk = _silent_chunk(duration_ms=5000.0, sample_rate=22_050)

        slice_count = 0

        def _delayed_write_slice(
            sd_module: object,  # noqa: ARG001
            audio: object,  # noqa: ARG001
            sample_rate: int,  # noqa: ARG001
        ) -> None:
            """Mimic per-slice PortAudio write latency."""
            nonlocal slice_count
            slice_count += 1
            # 50 ms slice ≈ 50 ms PortAudio block; we use a slightly
            # smaller wall-clock to keep the test fast while still
            # giving the interrupt thread a chance to flip the flag.
            time.sleep(0.02)

        # Mock the lazy-imported ``blocking_write_play`` symbol so the
        # real ``_play_audio`` slice-loop runs but no real PortAudio
        # device is touched.
        with patch(
            "sovyx.voice._stream_opener.blocking_write_play",
            side_effect=_delayed_write_slice,
        ):
            start = time.monotonic()
            task = asyncio.create_task(q.play_immediate(chunk))  # type: ignore[arg-type]
            # Let a few slices fire before barging in.
            await asyncio.sleep(0.05)
            q.interrupt()
            await asyncio.wait_for(task, timeout=1.0)
            elapsed = time.monotonic() - start

        # Pre-T1.2: would have been ~5 s. Post-T1.2: ~50-150 ms (one
        # slice in flight + scheduler slack). Allow 500 ms generous
        # ceiling — Windows clock granularity (anti-pattern #22) and
        # threadpool dispatch can spike on contended CI.
        assert elapsed < 0.5  # noqa: PLR2004 — latency budget pin
        # At least one slice must have run; interrupt must have stopped
        # us before all 100 slices (5000 ms / 50 ms) executed.
        max_slices_before_interrupt = 5000 // _PLAYBACK_SLICE_MS
        assert 0 < slice_count < max_slices_before_interrupt

    @pytest.mark.asyncio()
    async def test_play_immediate_uninterrupted_plays_full_chunk(self) -> None:
        """Without interrupt, the full chunk runs to completion.

        Verifies the slice-loop preserves the existing "play out the
        whole chunk" success path — every slice fires.
        """
        q = AudioOutputQueue()
        # Short chunk so the test stays fast: 200 ms / 50 ms slice = 4 slices.
        chunk = _silent_chunk(duration_ms=200.0, sample_rate=22_050)
        slice_count = 0

        def _instant_write_slice(
            sd_module: object,  # noqa: ARG001
            audio: object,  # noqa: ARG001
            sample_rate: int,  # noqa: ARG001
        ) -> None:
            nonlocal slice_count
            slice_count += 1

        with patch(
            "sovyx.voice._stream_opener.blocking_write_play",
            side_effect=_instant_write_slice,
        ):
            await q.play_immediate(chunk)  # type: ignore[arg-type]

        # ``samples_per_slice = int(sample_rate * slice_ms / 1000)`` truncates
        # downward — for 22 050 Hz / 50 ms that's 1102 samples/slice. A
        # 200 ms / 22 050 Hz buffer has 4410 samples. ``ceil(4410 / 1102)``
        # = 5 (four full slices + 2-sample remainder); the helper preserves
        # bit-exact playback so the remainder fires its own slice rather
        # than being dropped.
        chunk_samples = int(round(22_050 * 200 / 1000))
        samples_per_slice = int(22_050 * _PLAYBACK_SLICE_MS / 1000)
        expected_slices = math.ceil(chunk_samples / samples_per_slice)
        assert slice_count == expected_slices
        assert q.is_playing is False

    @pytest.mark.asyncio()
    async def test_play_immediate_simulate_path_is_interruptible(self) -> None:
        """Headless / no-PortAudio simulation must also honour interrupt.

        The simulation branch in ``_play_audio`` runs when sounddevice
        cannot be imported (CI / headless container). It mirrors the
        slice loop via ``_simulate_playback_interruptible`` so unit
        tests behave the same on both paths.
        """
        q = AudioOutputQueue()
        chunk = _silent_chunk(duration_ms=5000.0, sample_rate=22_050)

        # Force the simulate path by making the lazy import fail.
        import builtins

        real_import = builtins.__import__

        def _fail_sounddevice_import(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> object:
            if name == "sounddevice":
                raise ImportError("simulated headless environment")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_fail_sounddevice_import):
            start = time.monotonic()
            task = asyncio.create_task(q.play_immediate(chunk))  # type: ignore[arg-type]
            # Yield repeatedly so the simulate loop's first
            # ``asyncio.sleep(0.05)`` schedules; then barge in.
            await asyncio.sleep(0.05)
            q.interrupt()
            await asyncio.wait_for(task, timeout=1.0)
            elapsed = time.monotonic() - start

        assert elapsed < 0.5  # noqa: PLR2004 — same latency pin as production


# ── drain interruption preserved ────────────────────────────────────────


class TestDrainInterruptionPreserved:
    """Sanity: the existing per-chunk interrupt path on ``drain`` is
    unchanged. The slice-loop sits *inside* ``_play_audio``; the
    chunk-level guard is still on the ``while not self._interrupted``
    loop."""

    @pytest.mark.asyncio()
    async def test_drain_still_interruptible_per_chunk(self) -> None:
        q = AudioOutputQueue()
        chunks_played: list[object] = []

        async def _record_and_interrupt_after_first(chunk: object) -> None:
            chunks_played.append(chunk)
            if len(chunks_played) == 1:
                q.interrupt()

        for _ in range(5):
            await q.enqueue(_silent_chunk(duration_ms=10.0))  # type: ignore[arg-type]

        with patch.object(
            _output_queue_mod,
            "_play_audio",
            side_effect=_record_and_interrupt_after_first,
        ):
            await q.drain()

        # Only the first chunk plays before the interrupt loop guard
        # exits; the existing per-chunk semantics survived the T1.2
        # refactor untouched.
        assert len(chunks_played) == 1


# ── _is_interrupted contract ────────────────────────────────────────────


class TestIsInterruptedHelper:
    """Pin the contextvar-read helper used by ``_play_audio``."""

    def test_returns_false_when_no_queue_context(self) -> None:
        # No queue set on the contextvar (default None). Helper should
        # return False so legacy callers invoking _play_audio outside
        # AudioOutputQueue keep non-interruptible semantics.
        assert _output_queue_mod._is_interrupted() is False

    @pytest.mark.asyncio()
    async def test_reads_active_queue_flag_inside_play_immediate(self) -> None:
        """The contextvar token set by ``play_immediate`` must be visible
        to ``_is_interrupted`` from inside the dispatched coroutine."""
        q = AudioOutputQueue()
        observed: list[bool] = []

        async def _record_then_check(chunk: object) -> None:  # noqa: ARG001
            # Initial read: queue is set, not interrupted yet.
            observed.append(_output_queue_mod._is_interrupted())
            q.interrupt()
            # After interrupt: helper sees the flag flip.
            observed.append(_output_queue_mod._is_interrupted())

        with patch.object(
            _output_queue_mod,
            "_play_audio",
            side_effect=_record_then_check,
        ):
            await q.play_immediate(_silent_chunk(duration_ms=10.0))  # type: ignore[arg-type]

        assert observed == [False, True]

    def test_defensive_against_non_queue_contextvar_value(self) -> None:
        """If a test doubles a non-queue object onto the contextvar,
        ``_is_interrupted`` must not raise (best-effort getattr)."""

        class _NotAQueue:
            pass

        token = _output_queue_mod._active_queue.set(_NotAQueue())  # type: ignore[arg-type]
        try:
            # No _interrupted attribute → defaults to False.
            assert _output_queue_mod._is_interrupted() is False
        finally:
            _output_queue_mod._active_queue.reset(token)


# ── Sanity: existing test patterns continue working ─────────────────────


class TestBackCompatPatching:
    """Pin the back-compat contract: existing tests patching
    ``_play_audio`` with an AsyncMock or a single-arg side_effect must
    keep working — ``play_immediate`` and ``drain`` still call
    ``_play_audio(chunk)`` with no extra positional or keyword args."""

    @pytest.mark.asyncio()
    async def test_async_mock_patch_still_called_once_per_play_immediate(
        self,
    ) -> None:
        q = AudioOutputQueue()
        chunk = _silent_chunk(duration_ms=10.0)
        mock = AsyncMock()
        with patch.object(_output_queue_mod, "_play_audio", mock):
            await q.play_immediate(chunk)  # type: ignore[arg-type]
        mock.assert_called_once()
        # Single positional arg only — confirms no kwargs leaked into
        # the call site (which would break legacy ``side_effect=_fn(chunk)``
        # patches in the wider suite).
        call_args = mock.call_args
        assert len(call_args.args) == 1
        assert call_args.kwargs == {}

    @pytest.mark.asyncio()
    async def test_single_arg_side_effect_works_in_drain(self) -> None:
        q = AudioOutputQueue()
        played: list[object] = []

        async def _legacy_signature(chunk: object) -> None:
            """Single-arg signature mirrors existing tests in
            test_output_queue_aec_wireup.py + test_pipeline.py."""
            played.append(chunk)

        chunks: Iterable[object] = [_silent_chunk(duration_ms=10.0) for _ in range(3)]
        for chunk in chunks:
            await q.enqueue(chunk)  # type: ignore[arg-type]

        with patch.object(
            _output_queue_mod,
            "_play_audio",
            side_effect=_legacy_signature,
        ):
            await q.drain()

        assert len(played) == 3  # noqa: PLR2004 — count pin
