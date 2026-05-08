"""Sync ↔ async bridge for the STT-fallback wake-word detector.

Mission ``MISSION-wake-word-stt-fallback-2026-05-04.md`` §PART 2 / T2.

The :class:`~sovyx.voice._wake_word_stt_fallback.STTWakeWordDetector`
contract is intentionally synchronous (matches the duck-type surface of
:class:`~sovyx.voice.wake_word.WakeWordDetector` so the
:class:`~sovyx.voice._wake_word_router.WakeWordRouter` fans audio
frames to both detector classes uniformly). The
:class:`~sovyx.voice.stt.STTEngine`, however, is async + bound to the
daemon's event loop. The audio thread is decoupled from any loop.

Three boundaries to cross at once:

* **sync caller**: ``STTWakeWordDetector.process_frame`` runs on the
  audio thread; receives a numpy buffer; needs a string back.
* **async target**: ``STTEngine.transcribe`` is an ``async def``;
  internally calls ``asyncio.get_running_loop()`` (verified at
  ``voice/stt.py:804``) and registers C-callback continuations via
  ``loop.call_soon_threadsafe``.
* **other thread**: the engine is bound to the daemon's main event
  loop, NOT to the audio thread.

The only stdlib primitive that cleanly crosses all three is
:func:`asyncio.run_coroutine_threadsafe`. The docstring of
``_wake_word_stt_fallback.py`` (line 64-73) suggests
``asyncio.run(...)`` as a quick-path; that pattern raises
``RuntimeError`` inside a running event loop and creates a fresh loop
per call when it does work — wasteful + breaks integration with the
daemon's loop. ``run_coroutine_threadsafe`` returns a
:class:`concurrent.futures.Future` whose ``.result(timeout)`` blocks
the calling thread until the coroutine completes on the target loop —
exactly the contract we need.

v0.32.0 / Round-3 paranoid audit C2 — single-flight pattern + tightened
timeout
-------
Pre-v0.32.0 the bridge used a hardcoded 5.0 s timeout, which exceeded
the STT-fallback inter-call spacing of ~2 s (25 frames × 80 ms). A
pathological STT engine stall blocked the audio-thread worker for up
to 5 s — exceeding the spacing budget by 2.5×. Two fixes ship in
v0.32.0:

1. **Tightened default timeout** — moved to
   ``EngineConfig.tuning.voice.stt_fallback_timeout_seconds`` (default
   1.5 s, < 2 s spacing). Floor 0.1 s, ceiling 10.0 s; operators can
   opt back into the legacy 5.0 s via env override if their cloud STT
   needs the headroom, accepting that the worker may stall up to that
   ceiling on a single pathological call.

2. **Single-flight pattern** — when a transcribe call is already in
   flight, subsequent calls during the in-flight window return ``""``
   immediately + emit a structured ``voice.stt_fallback.dropped_overlapping_call``
   event. The audio-thread worker is therefore bounded at most one
   timeout per spacing window: even if a single call exceeds the 1.5 s
   timeout, no second worker is starved waiting behind it. The drop is
   safe because the detector's per-frame match logic treats ``""`` as
   a no-match (see ``_wake_word_stt_fallback.py:322-325``).

The single-flight flag is a ``threading.Lock``-guarded boolean (NOT
``asyncio.Lock`` — the contention is across audio-thread workers, not
loop-bound coroutines). The shared ``asyncio.Lock`` (``lock`` arg)
still serialises in-flight transcribe coroutines on the daemon loop
for R1 defense-in-depth against undocumented C-library concurrency.

Failure isolation: every error path returns an empty string so the
detector's per-frame match logic treats it as a no-match (see
``_wake_word_stt_fallback.py:322-325``: the detector wraps
``self._transcribe(combined)`` in a blanket ``except Exception``
specifically to keep STT engine failures from deafening the wake-word
path). Returning ``""`` skips the detector's exception path entirely
and lets the no-match counter increment normally; the result is the
same behaviour but through the engine-success-with-empty-transcript
codepath.

Reference: master mission §Phase 8 / T8.17-T8.19; mission spec
research artefacts R1 (MoonshineSTT contract) + R2 (bridge primitive
survey); v0.32.0 Round-3 paranoid audit C2.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice.stt import STTEngine

logger = get_logger(__name__)


def _default_timeout_seconds() -> float:
    """Read the bridge timeout from ``EngineConfig.tuning.voice``.

    Lazy resolution at builder-construction time: the env-driven value
    (``SOVYX_TUNING__VOICE__STT_FALLBACK_TIMEOUT_SECONDS``) is read
    once when ``make_stt_fallback_transcribe_fn`` is called without an
    explicit ``timeout_s`` override. Wrapped in a try/except so test
    code that constructs the bridge without a full ``EngineConfig``
    available falls back to the safe in-spacing-budget default.
    """
    try:
        from sovyx.engine.config import VoiceTuningConfig  # noqa: PLC0415

        return float(VoiceTuningConfig().stt_fallback_timeout_seconds)
    except Exception:  # noqa: BLE001 — defensive: never fail the bridge construction
        # In-spacing-budget safe default. Mirrors the EngineConfig
        # field default (1.5 s, < 25 × 80 ms = 2 s spacing).
        return 1.5


def make_stt_fallback_transcribe_fn(
    *,
    engine: STTEngine,
    loop: asyncio.AbstractEventLoop,
    lock: asyncio.Lock,
    timeout_s: float | None = None,
) -> Callable[[npt.NDArray[np.float32]], str]:
    """Build the sync transcribe wrapper expected by STTWakeWordDetector.

    Args:
        engine: Initialised STT engine (typically ``MoonshineSTT``).
            Must be in a usable state (``initialize()`` already
            awaited); the bridge does NOT initialise.
        loop: The daemon's main event loop. Captured at factory wire-up
            time; MUST be running when the audio thread invokes the
            returned callable. Closed-loop = the bridge returns empty
            string + the detector treats it as no-match (failure
            isolation).
        lock: Shared :class:`asyncio.Lock` that serialises concurrent
            transcribe calls across multiple registered minds. One lock
            per builder invocation; all NONE-strategy minds share it.
            R1 verified that ``moonshine_voice.Transcriber.create_stream``
            is per-call-fresh but the underlying C library doesn't
            document concurrency; the lock is defense-in-depth.
        timeout_s: Per-call hard deadline. ``None`` (default) reads
            ``EngineConfig.tuning.voice.stt_fallback_timeout_seconds``
            (default 1.5 s, < 2 s spacing). Operators tune via env
            ``SOVYX_TUNING__VOICE__STT_FALLBACK_TIMEOUT_SECONDS=...``;
            tests pass an explicit float. Timeouts cancel the
            underlying future (best-effort) and return "".

    Returns:
        A synchronous ``Callable[[NDArray[np.float32]], str]`` matching
        the contract at ``_wake_word_stt_fallback.py:74``. Pass this
        directly to :meth:`WakeWordRouter.register_mind_stt_fallback`.

    The returned callable implements a single-flight pattern: when one
    call is in flight, concurrent calls from other audio-thread workers
    return ``""`` immediately + emit a structured
    ``voice.stt_fallback.dropped_overlapping_call`` event. The drop is
    safe because the detector treats ``""`` as no-match. This bounds
    the audio-thread starvation surface at most one timeout per
    spacing window — see module docstring for the v0.32.0 / Round-3
    paranoid audit C2 rationale.
    """
    effective_timeout = timeout_s if timeout_s is not None else _default_timeout_seconds()

    # Single-flight gate. ``threading.Lock`` (NOT ``asyncio.Lock``)
    # because the contention is across audio-thread workers running
    # ``transcribe_sync`` on different threads, not loop-bound
    # coroutines. The boolean flag is the SOLE single-flight signal —
    # the lock just serialises flag mutations.
    in_flight_lock = threading.Lock()
    in_flight = {"value": False}

    async def _do_transcribe(audio: npt.NDArray[np.float32]) -> str:
        """Engine-side coroutine. Runs on the daemon's main loop."""
        async with lock:
            result = await engine.transcribe(audio, sample_rate=16_000)
        # ``TranscriptionResult.text`` is the canonical text field
        # (verified via the abstract STTEngine.transcribe signature at
        # voice/stt.py:338). Empty / rejected transcripts have ``text=""``
        # already; we pass through verbatim so the detector's variant
        # match handles the comparison.
        return result.text

    def transcribe_sync(audio: npt.NDArray[np.float32]) -> str:
        """Sync wrapper — runs on the audio thread.

        Submits the engine coroutine to the daemon loop via
        :func:`asyncio.run_coroutine_threadsafe`, blocks on
        ``.result(timeout)``. Failure isolation: every error path
        returns ``""`` so the detector treats it as a no-match
        instead of bubbling up + deafening the wake-word path.

        Single-flight: when another worker is already waiting on a
        transcribe future, returns ``""`` immediately + emits a
        structured drop event.
        """
        # ── single-flight gate ───────────────────────────────────────
        with in_flight_lock:
            if in_flight["value"]:
                # Another worker is mid-transcribe. Drop this call
                # immediately so we don't pile up audio-thread
                # workers behind a single pathological STT stall.
                logger.debug(
                    "voice.stt_fallback.dropped_overlapping_call",
                    **{
                        "voice.action": "no-match",
                        "voice.reason": (
                            "in-flight transcribe; single-flight bridge dropped overlap"
                        ),
                    },
                )
                return ""
            in_flight["value"] = True

        try:
            coro = _do_transcribe(audio)
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                # Loop is closed / not running; treat as no-match.
                # Close the orphaned coroutine to avoid a
                # RuntimeWarning at GC time ("coroutine was never
                # awaited") — clean cleanup matches the
                # failure-isolation contract.
                coro.close()
                logger.debug(
                    "voice.stt_fallback.bridge.loop_unavailable",
                    **{"voice.action": "no-match"},
                )
                return ""

            try:
                return future.result(timeout=effective_timeout)
            except concurrent.futures.TimeoutError:
                # Try to cancel the future best-effort; the detector
                # treats no-match the same regardless.
                future.cancel()
                logger.warning(
                    "voice.stt_fallback.bridge.timeout",
                    **{
                        "voice.timeout_s": effective_timeout,
                        "voice.action": "no-match",
                    },
                )
                return ""
            except concurrent.futures.CancelledError:
                logger.debug(
                    "voice.stt_fallback.bridge.cancelled",
                    **{"voice.action": "no-match"},
                )
                return ""
            except Exception as exc:  # noqa: BLE001 — failure isolation by design
                # Engine raised (e.g. RuntimeError from _ensure_ready
                # when the engine got closed mid-call, OSError on a
                # transient device failure, etc.). Detector contract
                # is "transcribe may raise; we treat as no-match" —
                # but we go one step further and absorb the
                # exception INSIDE the bridge so the detector's
                # wider blanket-except never fires for bridge-related
                # issues.
                logger.warning(
                    "voice.stt_fallback.bridge.engine_error",
                    **{
                        "voice.error": str(exc),
                        "voice.error_type": type(exc).__name__,
                        "voice.action": "no-match",
                    },
                )
                return ""
        finally:
            with in_flight_lock:
                in_flight["value"] = False

    return transcribe_sync


__all__ = [
    "make_stt_fallback_transcribe_fn",
]
