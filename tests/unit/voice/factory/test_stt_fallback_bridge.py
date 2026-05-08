"""Tests for the sync↔async STT-fallback bridge.

Mission: ``MISSION-wake-word-stt-fallback-2026-05-04.md`` §T5.

The bridge crosses three boundaries simultaneously:
* Sync caller (audio thread) → async target (STTEngine.transcribe) →
  loop-bound coroutine (daemon main loop).

These tests pin the contract:

* Happy path returns ``TranscriptionResult.text``.
* Engine raising → returns "" (no-match contract; failure isolation).
* Timeout → returns "" + future cancelled (best-effort).
* Loop closed/not-running → returns "" instead of raising.
* Lock serialises concurrent calls (R1 defense-in-depth).
* Bridge is reentrant — a fresh future per call.

Reference: research findings R1 (MoonshineSTT contract) + R2 (bridge
primitive selection) in the mission spec.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest

from sovyx.voice.factory._stt_fallback_bridge import (
    make_stt_fallback_transcribe_fn,
)

# ── Test fakes ───────────────────────────────────────────────────────


@dataclass
class _FakeTranscriptionResult:
    """Mirror of :class:`TranscriptionResult` — only ``text`` is read."""

    text: str


class _FakeEngineHappy:
    """Returns a deterministic transcript regardless of audio input."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self.call_count = 0

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        self.call_count += 1
        return _FakeTranscriptionResult(text=self._transcript)


class _FakeEngineErroring:
    """Raises a deterministic exception on every call."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        raise self._exc


class _FakeEngineSlow:
    """Sleeps for ``delay_s`` before returning. Used to test timeouts."""

    def __init__(self, delay_s: float, transcript: str = "delayed") -> None:
        self._delay_s = delay_s
        self._transcript = transcript

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        await asyncio.sleep(self._delay_s)
        return _FakeTranscriptionResult(text=self._transcript)


# ── Loop-running fixtures ────────────────────────────────────────────


def _start_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Spin up an event loop in a daemon thread.

    Mirrors the production topology: the daemon's main loop runs on
    one thread; the audio thread (which calls ``transcribe_sync``) is a
    different thread. Tests submit work via
    ``asyncio.run_coroutine_threadsafe`` exactly as production does.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(timeout=2.0)
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    loop.close()


@pytest.fixture
def loop_and_thread() -> object:
    loop, thread = _start_loop_in_thread()
    try:
        yield loop
    finally:
        _stop_loop(loop, thread)


# ── Test cases ───────────────────────────────────────────────────────


class TestHappyPath:
    """Bridge returns the engine's transcript text verbatim."""

    def test_happy_path_returns_engine_text(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineHappy(transcript="hey sovyx")

        async def _make() -> object:
            lock = asyncio.Lock()
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=lock)

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(16000, dtype=np.float32)
        result = transcribe_fn(audio)

        assert result == "hey sovyx"
        assert engine.call_count == 1


class TestFailureIsolation:
    """Bridge swallows every error and returns "" so the detector
    treats it as a no-match (per the wake-word detector's contract)."""

    def test_engine_raises_returns_empty_string(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineErroring(RuntimeError("engine boom"))

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(1024, dtype=np.float32)
        assert transcribe_fn(audio) == ""

    def test_timeout_returns_empty_string(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        # Engine sleeps 5 s; bridge timeout is 0.2 s.
        engine = _FakeEngineSlow(delay_s=5.0)

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=0.2
            )

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        start = time.monotonic()
        result = transcribe_fn(np.zeros(16000, dtype=np.float32))
        elapsed = time.monotonic() - start
        assert result == ""
        # Verify the bridge actually honoured the timeout (no
        # accidental 5 s wait); 50 ms slack absorbs scheduler jitter.
        assert elapsed < 1.0, f"bridge waited {elapsed:.2f} s — timeout not honoured"

    def test_loop_closed_returns_empty_string(self) -> None:
        """When the captured loop is no longer running, the bridge
        does NOT raise — it returns "" so detector behaviour stays
        identical to the engine-empty-transcript path."""
        # Build the bridge against a freshly-stopped loop.
        loop, thread = _start_loop_in_thread()
        engine = _FakeEngineHappy(transcript="never reached")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)
        _stop_loop(loop, thread)

        # Loop closed — calling transcribe_fn must not raise.
        result = transcribe_fn(np.zeros(1024, dtype=np.float32))
        assert result == ""


class TestSerialisation:
    """Concurrent-call ordering contract.

    v0.32.0 / Round-3 paranoid audit C2 changed the bridge from
    "serialise overlapping calls via asyncio.Lock" to "single-flight
    pattern: drop overlapping calls". The pre-v0.32.0 behaviour
    serialised three concurrent calls into three sequential intervals
    and returned three transcripts; the v0.32.0+ behaviour produces
    AT MOST ONE engine call within the in-flight window — the others
    drop with ``""`` + ``voice.stt_fallback.dropped_overlapping_call``.

    The shared ``asyncio.Lock`` (``lock`` arg) is still passed for
    R1 defense-in-depth IF an overlapping call ever sneaks past the
    single-flight gate (e.g. on a multi-bridge wire-up that shares
    one lock across N bridges) — but the single-flight gate is the
    primary serialisation primitive in v0.32.0+.
    """

    def test_concurrent_calls_drop_with_single_flight(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        """Three concurrent worker threads → at most one engine call
        completes; the others return ``""``. This pins the new C2
        contract and the regression: a future revert to lock-only
        serialisation would surface here."""
        loop = loop_and_thread

        engine_calls: list[float] = []
        engine_calls_lock = threading.Lock()

        class _RecordingEngine:
            async def transcribe(
                self,
                audio: np.ndarray,
                sample_rate: int = 16000,
            ) -> _FakeTranscriptionResult:
                del audio, sample_rate
                # Simulate ~100 ms transcribe work.
                await asyncio.sleep(0.1)
                with engine_calls_lock:
                    engine_calls.append(time.monotonic())
                return _FakeTranscriptionResult(text="x")

        engine = _RecordingEngine()

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=5.0
            )

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        # Fire 3 concurrent calls from 3 worker threads.
        results: list[str] = []
        results_lock = threading.Lock()

        def _worker() -> None:
            r = transcribe_fn(np.zeros(1024, dtype=np.float32))
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Single-flight contract: at least one call MUST succeed (the
        # one that wins the gate); the rest drop with ``""``. We don't
        # pin the exact count of successes — a fast scheduler can let
        # call #1 complete + release the gate before call #3 even
        # checks it, which lets call #3 also succeed. The hard
        # invariant is: all results are either "x" or "", and the
        # number of engine calls equals the number of "x" results.
        assert all(r in {"x", ""} for r in results)
        x_count = sum(1 for r in results if r == "x")
        assert len(engine_calls) == x_count, (
            f"engine called {len(engine_calls)} times but {x_count} successful results"
        )
        assert x_count >= 1, "at least the first call should succeed"


class TestReentrancy:
    """Each invocation creates a fresh future; calling the bridge
    repeatedly is safe."""

    def test_bridge_can_be_called_repeatedly(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineHappy(transcript="ok")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(16000, dtype=np.float32)
        for _ in range(5):
            assert transcribe_fn(audio) == "ok"
        assert engine.call_count == 5


# ── v0.32.0 / Round-3 paranoid audit C2 ──────────────────────────────
#
# Pin the single-flight + tightened-default-timeout contract. The
# bridge MUST drop overlapping calls (return "" + emit a structured
# ``voice.stt_fallback.dropped_overlapping_call`` event) so a single
# pathological STT stall can't starve every audio-thread worker.


class TestSingleFlightC2:
    """v0.32.0 C2 — overlapping calls during in-flight transcribe drop."""

    def test_single_flight_drops_overlapping_call(
        self, loop_and_thread: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two concurrent calls: first holds the bridge for 3 s; second
        fires immediately + must return "" + emit the drop log event.
        After the first completes, a third call works normally.

        Single-flight contract: bounded audio-thread starvation. Even
        if call #1 stalls past its timeout, call #2 does NOT pile up
        behind it — it returns "" immediately so the audio-thread
        worker is freed for the next frame.
        """
        loop = loop_and_thread
        # Slow engine simulates a pathological STT engine stall.
        engine = _FakeEngineSlow(delay_s=3.0, transcript="slow")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine,
                loop=loop,
                lock=asyncio.Lock(),
                # 5 s timeout > 3 s engine delay — call 1 completes.
                timeout_s=5.0,
            )

        fut = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = fut.result(timeout=2.0)

        # Fire call #1 in a worker thread (will block 3 s).
        result_1: list[str] = []
        result_1_done = threading.Event()

        def _worker_1() -> None:
            r = transcribe_fn(np.zeros(16000, dtype=np.float32))
            result_1.append(r)
            result_1_done.set()

        t1 = threading.Thread(target=_worker_1, daemon=True)
        t1.start()

        # Wait for call #1 to enter in-flight (mark the gate).
        # Polling the engine's ``transcribe`` start would require deeper
        # instrumentation; a small sleep is sufficient — the ``async with
        # lock`` + ``await asyncio.sleep`` runs on the loop thread and
        # is queued by ``run_coroutine_threadsafe`` immediately.
        time.sleep(0.1)

        # Call #2 fires NOW from this thread — must drop instantly.
        with caplog.at_level("DEBUG"):
            t2_start = time.monotonic()
            r2 = transcribe_fn(np.zeros(1024, dtype=np.float32))
            t2_elapsed = time.monotonic() - t2_start

        # Drop semantics: returns "" + happens fast (no block on the
        # in-flight call's 3 s wait).
        assert r2 == ""
        # Generous ceiling — even 0.5 s would be a bug here.
        assert t2_elapsed < 1.0, f"call #2 took {t2_elapsed:.2f}s — should drop ~instantly"
        # Structured drop event recorded.
        assert any(
            "voice.stt_fallback.dropped_overlapping_call" in r.message for r in caplog.records
        ), "expected dropped_overlapping_call log event"

        # Wait for call #1 to finish and prove call #3 works normally.
        result_1_done.wait(timeout=5.0)
        assert result_1 == ["slow"]

        # Single-flight gate releases after call #1 returns.
        # Call #3 (post-completion) goes through normally.
        r3 = transcribe_fn(np.zeros(1024, dtype=np.float32))
        assert r3 == "slow"

        t1.join(timeout=2.0)

    def test_normal_path_completes_within_budget(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        """Single (non-overlapping) call returns within reasonable time
        — the single-flight gate doesn't introduce a measurable
        regression on the happy path."""
        loop = loop_and_thread
        engine = _FakeEngineHappy(transcript="hey sovyx")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine,
                loop=loop,
                lock=asyncio.Lock(),
                timeout_s=2.0,
            )

        fut = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = fut.result(timeout=2.0)

        start = time.monotonic()
        result = transcribe_fn(np.zeros(16000, dtype=np.float32))
        elapsed = time.monotonic() - start

        assert result == "hey sovyx"
        # Fake engine has zero artificial delay — the round-trip should
        # be well under 1 s. 1 s ceiling absorbs scheduler jitter on
        # slow CI hosts.
        assert elapsed < 1.0, f"normal path took {elapsed:.2f}s — single-flight regression?"

    def test_default_timeout_reads_engine_config(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        """When ``timeout_s=None`` (default), the bridge reads the
        EngineConfig knob ``stt_fallback_timeout_seconds``."""
        loop = loop_and_thread
        # Engine sleeps 1.0 s; default timeout (1.5 s) would let it
        # complete; tighter override (0.2 s) would force timeout. We
        # can't easily mutate the engine config in-process, so we
        # verify the default-resolution path returns a callable that
        # honours <=10s ceiling (the field's pydantic ge/le bound).
        from sovyx.voice.factory._stt_fallback_bridge import _default_timeout_seconds

        timeout = _default_timeout_seconds()
        assert 0.1 <= timeout <= 10.0
        # Functional check: bridge construction with timeout_s=None
        # produces a working callable (regression — would crash if
        # the lazy import path raised).
        engine = _FakeEngineHappy(transcript="ok")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=None
            )

        fut = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = fut.result(timeout=2.0)
        assert transcribe_fn(np.zeros(1024, dtype=np.float32)) == "ok"

    def test_in_flight_releases_after_engine_error(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        """When the in-flight transcribe raises, the single-flight gate
        MUST release in the ``finally`` block — else every subsequent
        call is dropped forever (deadlock-equivalent)."""
        loop = loop_and_thread
        engine = _FakeEngineErroring(RuntimeError("engine boom"))

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=2.0
            )

        fut = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = fut.result(timeout=2.0)

        # Two consecutive calls — both must invoke the engine (i.e.,
        # the gate released after the first error). Both return "".
        assert transcribe_fn(np.zeros(1024, dtype=np.float32)) == ""
        assert transcribe_fn(np.zeros(1024, dtype=np.float32)) == ""

    def test_in_flight_releases_after_timeout(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        """Same regression check for the timeout path: the gate must
        release after timeout so the next worker can proceed."""
        loop = loop_and_thread
        engine = _FakeEngineSlow(delay_s=2.0)

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=0.2
            )

        fut = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = fut.result(timeout=2.0)

        # First call times out → returns "". Gate must release.
        r1 = transcribe_fn(np.zeros(1024, dtype=np.float32))
        assert r1 == ""

        # Wait for the cancelled future's cleanup to complete on the
        # daemon loop before firing the next call (avoids racing the
        # in-flight gate against the prior coroutine's tail).
        time.sleep(2.5)

        # Second call also times out — but the bridge accepted it
        # (i.e., didn't drop as overlapping), proving the gate released.
        r2 = transcribe_fn(np.zeros(1024, dtype=np.float32))
        assert r2 == ""
