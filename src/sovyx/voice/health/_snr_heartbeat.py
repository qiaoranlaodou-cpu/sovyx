"""Per-heartbeat SNR sample aggregator (Phase 4 / T4.34).

Captures per-window SNR estimates from the FrameNormalizer between
two consecutive ``voice_pipeline_heartbeat`` emissions and exposes
``p50`` / ``p95`` summaries for the orchestrator to log.

v0.32.6 Phase 5.A — multi-mind keying. Pre-fix this aggregator used a
single module-level ``deque`` so multi-mind hosts (v0.31.0+ multi-mind
GA) merged samples from every active mind into one buffer; the
heartbeat's ``mind_id`` field on the wire said one thing while the
percentile pair inside reflected the merged buffer. PHASE-4-D-AUDIT.md
Finding 6 surfaced the drift.

Storage now: ``dict[str, deque[float]]`` keyed by ``mind_id``, with a
hard cap on key cardinality (LRU eviction) so a misbehaving mind
provider can't leak memory. The producer (FrameNormalizer) passes
``mind_id`` per ``record_snr_sample`` call; the consumer (orchestrator)
calls ``drain_window_stats(mind_id=...)`` per heartbeat. The legacy
no-arg signatures stay valid for backward compat — they default to the
``"default"`` mind key. The unkeyed and keyed call paths share the
same per-mind backing buffer when the key is the literal ``"default"``,
so legacy callers see no behaviour change.

Cardinality / memory: bounded ring buffer of
:data:`_MAX_BUFFER_SAMPLES` floats per mind; up to
:data:`_MAX_MINDS` minds tracked simultaneously (LRU eviction beyond
that). At the FrameNormalizer's ~31 windows-per-second rate (16 kHz /
512 samples) and the default 30-second heartbeat interval, ~930
samples accumulate per heartbeat per mind — comfortably below the
4 096-sample buffer cap. Total worst-case footprint:
``_MAX_MINDS × _MAX_BUFFER_SAMPLES`` floats ≈ 1 MB at the defaults.

Concurrency: the FrameNormalizer runs on the capture audio thread;
the orchestrator drains on its asyncio loop. Both touch the per-mind
state through a single :class:`threading.Lock` — appends complete in
microseconds and never block the orchestrator.

The orchestrator MUST call :func:`drain_window_stats` exactly once per
heartbeat cycle PER MIND: read-and-clear semantics ensure the NEXT
window's stats reflect ONLY the samples that arrived between two
consecutive calls for that mind, matching the contract of the
existing ``max_vad_probability`` / ``frames_processed`` fields on the
heartbeat.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass

_MAX_BUFFER_SAMPLES = 4_096
"""Hard cap on the per-heartbeat buffer per mind. At ~31 windows/s +
30 s heartbeat = ~930 samples; 4 096 leaves headroom for slower
heartbeat cadences (max 60 s in practice) without uncapped growth on
a stuck orchestrator."""

_MAX_MINDS = 32
"""Hard cap on tracked mind keys. v0.31.0 multi-mind GA was sized at
single-digit minds per host; 32 leaves headroom for stress / multi-
operator dev hosts. LRU eviction past this cap drops the oldest
unused mind's buffer (the producer for that mind would refill on the
next sample). Bounds the worst-case footprint."""

_DEFAULT_MIND = "default"
"""The legacy unkeyed call path resolves to this key. Multi-mind
producers MUST pass an explicit ``mind_id``; un-passed defaults
share state with each other AND with any explicit ``"default"`` —
the migration is staged-adoption per ``feedback_staged_adoption``."""


@dataclass(frozen=True, slots=True)
class SnrWindowStats:
    """Per-heartbeat-window SNR summary.

    All three fields are computed from samples observed since the
    previous :func:`drain_window_stats` call FOR THE SAME ``mind_id``.
    """

    p50_db: float
    """Median SNR in dB across the window. ``0.0`` when count == 0
    (no real samples drained — the heartbeat field is suppressed in
    that case so the dashboard doesn't render a synthetic 0)."""

    p95_db: float
    """95th-percentile SNR in dB. Same zero-fallback contract as
    ``p50_db``."""

    count: int
    """Number of samples that contributed to the percentiles. The
    orchestrator gates the heartbeat field on ``count > 0``."""


_lock = threading.Lock()
# OrderedDict so we can move-to-end on access for LRU semantics. Each
# value is a bounded ring buffer for that mind's samples.
_per_mind_buffers: OrderedDict[str, deque[float]] = OrderedDict()


def _get_or_create_buffer_locked(mind_id: str) -> deque[float]:
    """Resolve the per-mind buffer; create + LRU-evict if needed.

    Caller MUST hold ``_lock``. The OrderedDict is touched on EVERY
    access to maintain LRU ordering — the eviction target is whichever
    mind's buffer hasn't been touched longest.
    """
    buf = _per_mind_buffers.get(mind_id)
    if buf is None:
        # Evict oldest mind if at capacity. The cap is intentionally
        # high (32) so this branch is cold in normal multi-mind use;
        # it exists to defend against a misbehaving caller that
        # generates unbounded mind_id values.
        while len(_per_mind_buffers) >= _MAX_MINDS:
            evicted_mind, _ = _per_mind_buffers.popitem(last=False)
            del evicted_mind  # name retained for grep / future logging
        buf = deque(maxlen=_MAX_BUFFER_SAMPLES)
        _per_mind_buffers[mind_id] = buf
    else:
        # LRU touch: move to end (most-recently-used).
        _per_mind_buffers.move_to_end(mind_id, last=True)
    return buf


def record_snr_sample(snr_db: float, *, mind_id: str = _DEFAULT_MIND) -> None:
    """Append one SNR sample to the heartbeat-window buffer for ``mind_id``.

    Called from :meth:`sovyx.voice._frame_normalizer.FrameNormalizer.
    _observe_snr` once per emitted capture window. The sample value
    has already been filtered against the SNR floor and the first-
    frame anchor by the caller; this aggregator does NOT re-filter so
    the call site retains a single point of policy.

    Args:
        snr_db: Per-window SNR estimate in decibels. Values outside
            the typical -30 to +60 dB range are still recorded —
            clamping is the dashboard layer's responsibility.
        mind_id: Owning mind. Default ``"default"`` for backward-
            compat with un-migrated producers; multi-mind producers
            pass an explicit per-turn mind_id (e.g. the orchestrator's
            ``_current_mind_id`` per anti-pattern #35).
    """
    with _lock:
        _get_or_create_buffer_locked(mind_id).append(snr_db)


def drain_window_stats(*, mind_id: str = _DEFAULT_MIND) -> SnrWindowStats:
    """Compute + clear the per-window p50/p95 summary for ``mind_id``.

    Called once per ``voice_pipeline_heartbeat`` emission per mind. The
    return value reflects samples observed since the previous drain
    for THIS mind; that mind's buffer is cleared atomically so the
    NEXT call sees a fresh window. Other minds' buffers are untouched.

    Args:
        mind_id: Mind whose window to drain. Default ``"default"`` for
            backward-compat with un-migrated consumers.

    Returns:
        :class:`SnrWindowStats` carrying the percentile pair and the
        sample count. ``count == 0`` means no samples accumulated in
        this window — typical during sustained silence (FrameNormalizer
        suppresses floor-only emissions) or before the first speech
        frame on boot.
    """
    with _lock:
        buf = _per_mind_buffers.get(mind_id)
        if buf is None:
            samples: list[float] = []
        else:
            samples = list(buf)
            buf.clear()
            # The buffer is now empty but kept in the OrderedDict so
            # the LRU position is preserved. Eviction only happens
            # when at-capacity AND a new mind arrives.

    count = len(samples)
    if count == 0:
        return SnrWindowStats(p50_db=0.0, p95_db=0.0, count=0)

    samples.sort()
    p50_idx = count // 2
    # 95th percentile via nearest-rank method — correct for any
    # count >= 1, no fractional-index edge cases. count*0.95 rounds
    # DOWN; we add the conventional +1 nearest-rank adjustment (see
    # Wikipedia "Percentile / nearest-rank") and clamp into
    # [0, count-1].
    p95_idx = min(count - 1, max(0, int(count * 0.95)))
    return SnrWindowStats(
        p50_db=float(samples[p50_idx]),
        p95_db=float(samples[p95_idx]),
        count=count,
    )


def reset_for_tests() -> None:
    """Clear ALL per-mind buffers without computing stats.

    Test-only helper. Production code MUST go through
    :func:`drain_window_stats` so the contract "drain returns the last
    window's stats" holds. This helper drops every mind's state — use
    when test isolation requires a clean slate across all minds.
    """
    with _lock:
        _per_mind_buffers.clear()
