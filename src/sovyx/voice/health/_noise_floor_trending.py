"""Rolling-window noise-floor trend tracker (Phase 4 / T4.38).

Tracks the capture noise floor (in dBFS) over a long enough window
that a *sustained* room-noise increase surfaces while short-lived
spikes (door slam, mic bump, single keystroke) are smoothed out.
Pairs with the T4.34 / T4.35 SNR heartbeat pipeline: SNR p50 measures
the speech-vs-noise ratio per window, this module measures the noise
floor itself drifting.

Architecture mirrors :mod:`._snr_heartbeat`:

* FrameNormalizer feeds samples via :func:`record_noise_floor_sample`
  once per emitted capture window.
* The orchestrator's ``_track_vad_for_heartbeat`` calls
  :func:`compute_drift` to read (without clearing) the current
  short-window vs long-window mean delta, and the per-mind alert
  latch fires WARN / CLEARED on sustained drift.

Unlike the SNR drain, the noise-floor sampler is **read-only** on
each heartbeat: the rolling buffer keeps accumulating across
heartbeats so the trend computation has a stable horizon. The buffer
wraps at the long-window cap; old samples drop FIFO via
``deque(maxlen=...)``.

v0.32.6 Phase 5.A — multi-mind keying. Pre-fix this aggregator used a
single module-level ``deque`` so multi-mind hosts (v0.31.0+) merged
samples from every active mind into one rolling buffer; sustained
drift in one mind would falsely inflate / mask drift in another.
PHASE-4-D-AUDIT.md Finding 6 surfaced the drift.

Storage now: ``dict[str, deque[float]]`` keyed by ``mind_id`` with
LRU eviction past :data:`_MAX_MINDS` keys (defends against
unbounded mind_id producers).

Cardinality / memory: at the FrameNormalizer's ~31 windows-per-second
rate, a 5-minute buffer holds ~9 300 samples per mind. ``deque`` with
bounded ``maxlen`` keeps that under 80 KB per mind and trims O(1) on
overflow. The drift computation is O(N) per heartbeat per mind (one
walk over each window's slice); at the 30 s heartbeat interval that
is ≤ 0.5 ms of CPU per heartbeat per mind — negligible.

Concurrency: same lock contract as :mod:`._snr_heartbeat` — producer
(capture audio thread) and consumer (orchestrator asyncio loop) both
touch the per-mind state under one ``threading.Lock``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass

_SHORT_WINDOW_SAMPLES = 1_800
"""Samples in the short ("now") window. At ~31 windows/s that is ~60
s of capture — long enough to absorb single-frame noise bursts but
short enough to react to a fan turning on within a minute."""

_LONG_WINDOW_SAMPLES = 9_300
"""Samples in the long ("baseline") window. ~5 minutes at the
FrameNormalizer's 31 windows/s rate, matching the master mission's
§Phase 4 / T4.38 contract: "moving average of background RMS over 5
min window; alert if floor raised >10 dB". The long window IS the
rolling baseline; sustained drift re-baselines automatically after
one full window of the new noise floor."""

_MAX_MINDS = 32
"""Hard cap on tracked mind keys. v0.31.0 multi-mind GA was sized at
single-digit minds per host; 32 leaves headroom for stress / multi-
operator dev hosts. LRU eviction past this cap drops the oldest
unused mind's buffer. Bounds the worst-case footprint to
``_MAX_MINDS × _LONG_WINDOW_SAMPLES`` floats ≈ 2.4 MB."""

_DEFAULT_MIND = "default"
"""Legacy unkeyed call path resolves to this key. See
``_snr_heartbeat._DEFAULT_MIND`` for the staged-adoption rationale."""


@dataclass(frozen=True, slots=True)
class NoiseFloorDrift:
    """Per-heartbeat noise-floor drift summary.

    ``drift_db = short_avg_db - long_avg_db``. Positive means the
    floor rose; negative means it fell (room got quieter). The
    orchestrator alerts on the positive-direction crossing of the
    configured threshold (default 10 dB).
    """

    short_avg_db: float
    """Mean noise-floor dBFS across the most recent ~60 s. ``0.0``
    when ``short_count == 0``."""

    long_avg_db: float
    """Mean noise-floor dBFS across the rolling ~5 min baseline.
    ``0.0`` when ``long_count == 0``."""

    drift_db: float
    """Signed delta short_avg_db − long_avg_db. ``0.0`` when either
    window lacks samples (the orchestrator gates the alert on
    ``ready=True``)."""

    short_count: int
    """Sample count in the short window. The orchestrator gates the
    heartbeat field on ``short_count > 0``."""

    long_count: int
    """Sample count in the long window. ``ready=True`` requires this
    to equal :data:`_LONG_WINDOW_SAMPLES` so the baseline has had
    time to settle."""

    ready: bool
    """``True`` iff both windows are full enough that drift is
    meaningful — short window has at least
    :data:`_SHORT_WINDOW_SAMPLES // 4` samples (≈15 s) AND the long
    window is fully populated. Pre-``ready`` heartbeats skip the
    alert path so a cold-boot transient doesn't misfire."""


_lock = threading.Lock()
_per_mind_buffers: OrderedDict[str, deque[float]] = OrderedDict()


def _get_or_create_buffer_locked(mind_id: str) -> deque[float]:
    """Resolve the per-mind buffer; create + LRU-evict if needed.

    Caller MUST hold ``_lock``. Same LRU contract as
    :mod:`._snr_heartbeat`.
    """
    buf = _per_mind_buffers.get(mind_id)
    if buf is None:
        while len(_per_mind_buffers) >= _MAX_MINDS:
            evicted_mind, _ = _per_mind_buffers.popitem(last=False)
            del evicted_mind  # name retained for grep / future logging
        buf = deque(maxlen=_LONG_WINDOW_SAMPLES)
        _per_mind_buffers[mind_id] = buf
    else:
        _per_mind_buffers.move_to_end(mind_id, last=True)
    return buf


def record_noise_floor_sample(
    noise_floor_db: float, *, mind_id: str = _DEFAULT_MIND
) -> None:
    """Append one noise-floor dBFS sample to the rolling buffer.

    Called from :meth:`sovyx.voice._frame_normalizer.FrameNormalizer.
    _observe_snr` once per emitted capture window. The caller converts
    the SnrEstimator's linear noise-power tracker to dBFS using the
    int16 full-scale reference; this aggregator does NOT re-derive
    units.

    Args:
        noise_floor_db: Current noise-floor estimate in dBFS. Typical
            range ``[-90, -30]`` — anything outside is still recorded
            (clamping is the dashboard layer's responsibility, not
            the aggregator's).
        mind_id: Owning mind. Default ``"default"`` for backward-compat
            with un-migrated producers.
    """
    with _lock:
        _get_or_create_buffer_locked(mind_id).append(noise_floor_db)


def compute_drift(*, mind_id: str = _DEFAULT_MIND) -> NoiseFloorDrift:
    """Read short-vs-long noise-floor drift WITHOUT clearing for ``mind_id``.

    Unlike :func:`._snr_heartbeat.drain_window_stats`, this function
    does NOT modify the buffer — the rolling window keeps accumulating
    across heartbeats so the trend horizon is stable. Each heartbeat
    sees a fresh "short vs long" snapshot derived from the same
    long-running buffer FOR THIS MIND.

    Args:
        mind_id: Mind whose drift to compute. Default ``"default"``
            for backward-compat.

    Returns:
        :class:`NoiseFloorDrift` with the two window averages, their
        difference, and a ``ready`` flag the orchestrator uses to
        gate the alert path.
    """
    with _lock:
        buf = _per_mind_buffers.get(mind_id)
        snapshot: list[float] = list(buf) if buf is not None else []

    long_count = len(snapshot)
    short_count = min(long_count, _SHORT_WINDOW_SAMPLES)

    if short_count == 0:
        return NoiseFloorDrift(
            short_avg_db=0.0,
            long_avg_db=0.0,
            drift_db=0.0,
            short_count=0,
            long_count=0,
            ready=False,
        )

    # Short window = the most recent _SHORT_WINDOW_SAMPLES samples.
    short_slice = snapshot[-short_count:]
    short_avg = sum(short_slice) / short_count
    long_avg = sum(snapshot) / long_count

    # Ready gate: long window must be fully populated AND short window
    # has at least 25 % of its capacity. Below the 25 % floor the
    # short avg is too noisy to compare.
    ready = long_count >= _LONG_WINDOW_SAMPLES and short_count >= _SHORT_WINDOW_SAMPLES // 4

    return NoiseFloorDrift(
        short_avg_db=float(short_avg),
        long_avg_db=float(long_avg),
        drift_db=float(short_avg - long_avg),
        short_count=short_count,
        long_count=long_count,
        ready=ready,
    )


def reset_for_tests() -> None:
    """Clear ALL per-mind rolling buffers.

    Test-only helper. Production code does NOT clear the buffers —
    the long window's purpose is to span pipeline lifetimes; clearing
    it from the heartbeat would defeat the trend detection.
    """
    with _lock:
        _per_mind_buffers.clear()
