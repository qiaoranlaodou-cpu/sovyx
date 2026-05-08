"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice._chaos import ChaosInjector, ChaosSite
from sovyx.voice._stage_metrics import VoiceStage, record_queue_depth

if TYPE_CHECKING:
    from sovyx.voice._aec import RenderPcmSink
    from sovyx.voice.tts_piper import AudioChunk

logger = get_logger(__name__)

_PLAYBACK_SLICE_MS = 50
"""Per-slice duration of the chunked PortAudio write inside ``_play_audio``.

Production playback writes the chunk in ``_PLAYBACK_SLICE_MS``-millisecond
slices and polls the calling queue's ``_interrupted`` flag between slices.
A 50 ms slice gives a worst-case barge-in latency of ~100-150 ms (the slice
in flight plus thread-pool dispatch overhead) which is below the perceptual
ceiling for "the assistant stopped talking right when I started" — the
fully-blocking pre-T1.2 path could take 5+ seconds on a long sentence.

Anti-pattern #17: this default is co-located here so the per-call cost is
zero (constant evaluated once at import) while leaving room for a future
``EngineConfig.tuning.voice.playback_slice_ms`` knob if operators ever need
to widen the slice for thread-pool-pressure reasons.
"""

# Per-task context carrying the queue currently dispatching a chunk into
# ``_play_audio``. Set by ``play_immediate`` / ``drain`` before the call,
# read by the real ``_play_audio`` body to poll ``_interrupted`` between
# slices. ``contextvars.ContextVar`` is used (not a module-level reference)
# so concurrent dispatch from independent queues stays isolated. The
# default ``None`` makes the slice-loop fall back to non-interruptible
# playback when invoked outside ``AudioOutputQueue`` (e.g. legacy callers
# wiring to the public re-export).
_active_queue: contextvars.ContextVar["AudioOutputQueue | None"] = contextvars.ContextVar(  # noqa: UP037 — class is forward-declared; bare reference would NameError at module load
    "_active_queue",
    default=None,
)


_DEFAULT_USAGE_CAPACITY_REFERENCE = 256
"""Operator-meaningful upper bound on healthy queue depth.

The underlying ``asyncio.Queue`` is unbounded (the orchestrator
enforces back-pressure via barge-in + drain timing). For M2 USE
saturation_pct to be meaningful we pin a *reference* capacity:
the depth at which we'd consider the pipeline 'genuinely
abnormal' (orchestrator over-pre-rendering, drain stalled,
playback thread starved).

256 chunks at typical TTS chunk size (~1 s of audio per chunk)
≈ 4 minutes of buffered speech. Anything deeper than that is a
bug — either barge-in is broken or the drain task is stuck. The
``record_queue_depth`` saturation overflow warning fires at >100%
(>256 chunks), which is exactly the right signal for operators.
"""


class AudioOutputQueue:
    """Queue-based audio output with interruption support.

    Manages a FIFO of :class:`AudioChunk` objects and plays them
    sequentially.  :meth:`interrupt` clears the queue and stops
    current playback (used for barge-in).
    """

    def __init__(
        self,
        *,
        usage_capacity_reference: int = _DEFAULT_USAGE_CAPACITY_REFERENCE,
        render_buffer: RenderPcmSink | None = None,
    ) -> None:
        """Construct an audio output queue.

        Args:
            usage_capacity_reference: Reference depth ceiling for the
                M2 ``voice.queue.saturation_pct`` metric. The actual
                queue is unbounded; this value defines what
                ``saturation_pct = 100`` means for the dashboard
                ("at the depth we'd consider abnormal"). Tuneable per
                deployment if a particular workload routinely runs
                deeper than the default 256.
            render_buffer: Optional Phase 4 / T4.4.c AEC reference
                sink. When provided, every ``AudioChunk`` is fed to
                the buffer's :meth:`feed` BEFORE the playback dispatch
                so the FrameNormalizer's AEC stage has a time-aligned
                reference for echo cancellation. Default ``None``
                preserves the pre-AEC playback contract bit-exactly.
        """
        self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self._playing = False
        self._interrupted = False
        # Tracks total audio_ms enqueued but not yet drained — gives the
        # dashboard a real "how much audio is queued" gauge instead of
        # just a chunk count (chunks may be tiny single-sentence TTS or
        # large multi-paragraph pre-renders).
        self._pending_audio_ms = 0.0
        self._usage_capacity_reference = usage_capacity_reference
        self._render_buffer: RenderPcmSink | None = render_buffer
        # TS3 chaos injector — opt-in saturation simulation at the
        # OUTPUT_QUEUE_DROP site. Disabled by default; chaos test
        # matrix sets the env vars to validate that the M2 USE
        # voice.queue.saturation_overflow WARN fires correctly when
        # depth exceeds the capacity reference.
        self._chaos = ChaosInjector(site_id=ChaosSite.OUTPUT_QUEUE_DROP.value)

    @property
    def is_playing(self) -> bool:
        """Whether audio is currently being played."""
        return self._playing

    async def enqueue(self, chunk: AudioChunk) -> None:
        """Add an audio chunk to the playback queue.

        Args:
            chunk: Audio data to play.
        """
        await self._queue.put(chunk)
        self._pending_audio_ms += float(chunk.duration_ms)
        depth = self._queue.qsize()
        # TS3 chaos: opt-in saturation injection at the
        # OUTPUT_QUEUE_DROP site. When chaos fires, REPORT a
        # synthetic depth at 2x capacity reference — exercises the
        # M2 voice.queue.saturation_overflow WARN path that
        # operators rely on to detect real over-pre-rendering /
        # stalled drains. The actual queue state is unchanged
        # (real depth still tracked via the normal record_queue_depth
        # call below), so chaos is observability-only here.
        if self._chaos.should_inject():
            record_queue_depth(
                VoiceStage.OUTPUT,
                depth=self._usage_capacity_reference * 2,
                capacity=self._usage_capacity_reference,
            )
        # Ring 6 USE — depth + saturation_pct via the M2 facade.
        # The capacity reference is the operator-meaningful upper
        # bound (default 256). Depths beyond that fire a structured
        # warning via record_queue_depth's internal overflow guard,
        # which is exactly the right operator signal.
        record_queue_depth(
            VoiceStage.OUTPUT,
            depth=depth,
            capacity=self._usage_capacity_reference,
        )
        logger.info(
            "voice.output_queue.enqueued",
            **{
                "voice.depth": depth,
                "voice.chunk_audio_ms": round(float(chunk.duration_ms), 1),
                "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
            },
        )
        logger.info(
            "voice.output_queue.depth",
            **{
                "voice.depth": depth,
                "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
                "voice.is_playing": self._playing,
            },
        )

    async def play_immediate(self, chunk: AudioChunk) -> None:
        """Play a single chunk immediately (blocking until done).

        T1.2 (CRITICAL CR2) — interruption-aware. Sets ``_active_queue``
        on the contextvar so the real ``_play_audio`` slice-loop can poll
        ``self._interrupted`` between slices. Without this wiring,
        ``OutputStream.write(buffer)`` blocks until the entire chunk is
        consumed by PortAudio (5+ s on long sentences) and operator
        barge-in fires the flag but never gets observed.

        The streaming ``drain`` path is interruptible per-chunk via the
        ``while not self._interrupted`` loop; ``play_immediate`` is the
        non-streaming counterpart and gains the same property here via
        the slice-loop in ``_play_audio``.

        Args:
            chunk: Audio data to play.
        """
        self._playing = True
        # Reset the interrupt flag at the start of a fresh playback so a
        # previous interrupt() call doesn't immediately short-circuit
        # this new chunk. drain() does the same on its entry path.
        self._interrupted = False
        token = _active_queue.set(self)
        try:
            self._feed_render_buffer(chunk)
            await _play_audio(chunk)
        finally:
            _active_queue.reset(token)
            self._playing = False

    async def drain(self) -> None:
        """Play all queued chunks sequentially until queue is empty.

        T1.2 (CRITICAL CR2) — every per-chunk dispatch sets
        ``_active_queue`` on the contextvar so the real ``_play_audio``
        slice-loop can short-circuit *within* a chunk if barge-in fires
        mid-playback. The pre-existing ``not self._interrupted`` check
        on the loop boundary still gates the *next* chunk; the slice
        loop adds the *intra-chunk* interruption that long sentences
        need.
        """
        self._playing = True
        self._interrupted = False
        chunks_drained = 0
        audio_ms_drained = 0.0
        token = _active_queue.set(self)
        try:
            while not self._queue.empty() and not self._interrupted:
                chunk = self._queue.get_nowait()
                self._pending_audio_ms = max(
                    0.0, self._pending_audio_ms - float(chunk.duration_ms)
                )
                self._feed_render_buffer(chunk)
                await _play_audio(chunk)
                chunks_drained += 1
                audio_ms_drained += float(chunk.duration_ms)
        finally:
            _active_queue.reset(token)
            self._playing = False
            interrupted = self._interrupted
            self._interrupted = False
            logger.info(
                "voice.output_queue.drained",
                **{
                    "voice.chunks_drained": chunks_drained,
                    "voice.audio_ms_drained": round(audio_ms_drained, 1),
                    "voice.depth_remaining": self._queue.qsize(),
                    "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
                    "voice.interrupted": interrupted,
                },
            )

    def interrupt(self) -> None:
        """Stop current playback and clear the queue (barge-in).

        T1.22 contract — infallible + idempotent + mute-flag-first.

        The first statement sets ``self._interrupted = True``
        unconditionally, which is the fallback mute mechanism: even
        if every subsequent operation in this method failed (the
        ``while not self._queue.empty()`` loop, the
        ``self._queue.get_nowait()`` calls, the
        ``self._pending_audio_ms`` assignment), the drain loop in
        :meth:`drain` would still observe ``_interrupted=True`` on
        its next iteration and short-circuit playback. Callers
        therefore do not need to wrap this method in defensive
        ``try/except`` blocks; the
        ``cancel_speech_chain`` step-1 ``except Exception`` shield in
        :mod:`_orchestrator` is paranoid-only and never fires at
        HEAD. Idempotent against repeated calls — the second call
        observes the queue already empty and the flag already set,
        and silently no-ops.
        """
        # Set the mute flag FIRST so a failure in any subsequent
        # operation can't leave the queue accepting playback.
        self._interrupted = True
        # Drain queue without awaiting. ``QueueEmpty`` can race in
        # if a concurrent ``enqueue`` raced this drain — break and
        # accept whatever we got.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending_audio_ms = 0.0

    def clear(self) -> None:
        """Clear pending chunks without interrupting current playback."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending_audio_ms = 0.0

    def set_render_buffer(self, buffer: RenderPcmSink | None) -> None:
        """Wire (or unwire) the AEC render-PCM sink at runtime.

        Mirrors the FrameNormalizer's :meth:`set_render_provider` —
        the orchestrator constructs a single
        :class:`~sovyx.voice._render_pcm_buffer.RenderPcmBuffer`
        instance and registers it on both sides (queue=sink,
        normalizer=provider) so render-PCM flows producer→consumer
        through the same ring.
        """
        self._render_buffer = buffer

    def _feed_render_buffer(self, chunk: AudioChunk) -> None:
        """Forward ``chunk.audio`` to the AEC render buffer if wired.

        Best-effort: render-buffer feed failures must NOT block
        playback. The buffer's contract guarantees no exceptions for
        well-formed chunks (validated dtype + sample_rate at the TTS
        engine boundary), but a malformed chunk reaching this site
        is logged and swallowed so ``_play_audio`` still runs.
        Anti-pattern #14 doesn't apply because feed is sync + fast
        (lock-protected ring write); no asyncio.to_thread needed.
        """
        if self._render_buffer is None:
            return
        try:
            self._render_buffer.feed(chunk.audio, chunk.sample_rate)
        except Exception:
            logger.exception(
                "voice.output_queue.render_buffer_feed_failed",
                **{
                    "voice.chunk_audio_ms": round(float(chunk.duration_ms), 1),
                    "voice.chunk_sample_rate": chunk.sample_rate,
                },
            )


def _is_interrupted() -> bool:
    """Read the active queue's ``_interrupted`` flag from the contextvar.

    Returns ``False`` when no queue context is set (legacy callers
    invoking ``_play_audio`` outside :class:`AudioOutputQueue` keep the
    pre-T1.2 non-interruptible semantics) or when the queue exists but
    has not been interrupted. Defensive against ``AttributeError`` if a
    test doubles in a non-queue object on the contextvar.
    """
    queue = _active_queue.get()
    if queue is None:
        return False
    return bool(getattr(queue, "_interrupted", False))


def _slice_audio(audio: object, sample_rate: int, slice_ms: int) -> list[object]:
    """Return ``audio`` partitioned into ``slice_ms``-millisecond chunks.

    Uses standard slicing (works on numpy arrays + sequences). Empty or
    zero-rate inputs collapse to a single-element list so callers always
    iterate at least once and the "no audio at all" case is preserved.
    The last slice may be shorter than ``slice_ms``.

    Args:
        audio: 1-D mono PCM buffer (numpy array). Typed as ``object``
            here to keep this module free of numpy at import time —
            slicing semantics are sequence-protocol only.
        sample_rate: Samples per second.
        slice_ms: Target slice duration in milliseconds.

    Returns:
        Ordered list of buffer slices. ``audio[i:j]``-style views; no
        copying.
    """
    samples_per_slice = int(sample_rate * slice_ms / 1000)
    length = len(audio)  # type: ignore[arg-type]
    if samples_per_slice <= 0 or length == 0:
        return [audio]
    slices: list[object] = []
    cursor = 0
    while cursor < length:
        slices.append(audio[cursor : cursor + samples_per_slice])  # type: ignore[index]
        cursor += samples_per_slice
    return slices


async def _play_audio(chunk: AudioChunk) -> None:
    """Play an audio chunk via sounddevice (or simulate in test).

    This is the low-level playback function. In production it slices
    ``chunk.audio`` into ``_PLAYBACK_SLICE_MS``-millisecond slices and
    pipes each slice through :func:`sovyx.voice._stream_opener.blocking_write_play`
    via :func:`asyncio.to_thread`. Between slices the interrupt flag on
    the calling :class:`AudioOutputQueue` (sourced from the
    ``_active_queue`` contextvar set by ``play_immediate`` / ``drain``)
    is polled — if barge-in has fired, playback bails out before the
    next slice is dispatched (T1.2 / CRITICAL CR2).

    Why slicing — pre-T1.2 the entire chunk was written in one
    ``OutputStream.write(buffer)`` call. PortAudio blocks until the
    whole buffer is consumed (5+ seconds on a long sentence). The
    ``interrupt()`` flag was set but never read, so operator barge-in
    didn't actually stop the audio until the sentence finished. The
    slice loop bounds the worst-case barge-in latency to
    ~100-150 ms (one slice in flight + thread-pool dispatch).

    Anti-pattern #14: every blocking PortAudio call is wrapped in
    :func:`asyncio.to_thread`. Anti-pattern #27: PortAudio errors
    during slice playback fall through ``contextlib.suppress``
    (the slice loop terminates and the caller observes a normal return,
    same contract as the legacy single-write path's
    ``except sd.PortAudioError`` branch).

    ``sd.play`` is deliberately avoided here: on Windows + WASAPI its
    callback engine requires COM on the calling thread, which
    :func:`asyncio.to_thread` workers do not have —
    :func:`blocking_write_play` uses the blocking WASAPI path that
    handles COM transitions internally.

    Args:
        chunk: The audio chunk to play.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError):
        # Headless / test environment — simulate playback duration.
        # OSError covers the "PortAudio library not found" case on
        # Linux + macOS CI runners where the Python ``sounddevice``
        # module imports cleanly but its native PortAudio C library
        # backing ``_libname_lookup`` is absent (sounddevice raises
        # ``OSError`` from its module init in that case). Without this
        # branch, a headless integration test that drives
        # ``play_immediate`` triggers an uncaught OSError and fails
        # with no meaningful path forward — same operator-grade
        # contract as the ``ImportError`` branch.
        #
        # T1.2: still honour interrupt mid-simulation so unit tests
        # exercising the slice-loop semantics on headless CI observe
        # the same barge-in behaviour as production.
        await _simulate_playback_interruptible(chunk.duration_ms)
        return

    from sovyx.voice._stream_opener import blocking_write_play

    slices = _slice_audio(chunk.audio, chunk.sample_rate, _PLAYBACK_SLICE_MS)
    try:
        for slice_buf in slices:
            if _is_interrupted():
                logger.debug(
                    "voice.output_queue.play_audio_interrupted",
                    **{"voice.chunk_audio_ms": round(float(chunk.duration_ms), 1)},
                )
                return
            await asyncio.to_thread(blocking_write_play, sd, slice_buf, chunk.sample_rate)
    except sd.PortAudioError:
        # Headless server / CI runner with no audio device — sounddevice
        # imports + libportaudio2 loads cleanly, but ``query_devices(-1)``
        # raises ``sounddevice.PortAudioError`` because there's no
        # default output device available. Same operator-grade
        # contract as the ImportError / OSError branches above:
        # simulate playback duration so the orchestrator's state
        # machine progresses normally in the absence of real audio
        # hardware. This makes integration tests + headless container
        # deployments work without requiring per-test mocking of
        # ``_play_audio``.
        await _simulate_playback_interruptible(chunk.duration_ms)


async def _simulate_playback_interruptible(duration_ms: float) -> None:
    """Sleep ``duration_ms`` total, polling the interrupt flag every slice.

    Mirrors the slice-loop semantics on the headless / no-PortAudio
    branches of :func:`_play_audio` so unit tests exercising
    ``play_immediate`` interruption behave identically on CI runners
    without an audio device and on production hosts with a real one.
    The slice cadence is governed by :data:`_PLAYBACK_SLICE_MS` so a
    future widen-the-slice tuning lands on the simulation path too.
    """
    if duration_ms <= 0:
        return
    remaining = duration_ms
    while remaining > 0:
        if _is_interrupted():
            return
        sleep_ms = min(remaining, _PLAYBACK_SLICE_MS)
        await asyncio.sleep(sleep_ms / 1000)
        remaining -= sleep_ms


# ---------------------------------------------------------------------------
# BargeInDetector
# ---------------------------------------------------------------------------
