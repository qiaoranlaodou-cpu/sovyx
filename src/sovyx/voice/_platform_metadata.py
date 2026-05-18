"""Cross-platform audio capture-chain family taxonomy + platform-token resolver.

Companion to :mod:`sovyx.voice._event_names` (Mission H2 §T1.2). Resolves
the *family* of capture-chain processing involved in a bypass cascade
from the platform-prefixed strategy names emitted by the coordinator,
plus the current process platform token.

The family is the metadata field downstream consumers (dashboards,
OTel exporters, operator runbooks) filter by for cross-platform queries;
the strategy list is detail. By splitting "what kind of processing" from
"which OS we are on" the H2 mission removes the structural cause of
anti-pattern #39(b) drift.

Anti-pattern #9 + #16: ``StrEnum`` re-exported from ``sovyx.voice``.
Anti-pattern #14: pure-function resolvers; no I/O, no async, safe to
call from any event-loop context. Anti-pattern #15: ``functools.cache``
on the platform-token resolver — ``sys.platform`` does not change at
runtime.
"""

from __future__ import annotations

import functools
import sys
from collections import Counter
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class PlatformAudioFamily(StrEnum):
    """Canonical capture-chain processing families across all supported
    platforms.

    The value strings are operator-facing — they surface in structured
    log payloads (``voice.bypass_family`` attribute), OTel exporters,
    and dashboard rendering. Stable across minor versions so downstream
    queries can pin on them.
    """

    VOICE_CLARITY = "voice_clarity"
    """Windows Voice Clarity APO + VocaEffectPack family. The canonical
    Windows-specific capture-chain processing that anti-pattern #21
    documents.
    """

    VOICE_ISOLATION = "voice_isolation"
    """macOS Voice Isolation system-level capture effect. Surfaced by
    System Settings → Microphone → Microphone Mode.
    """

    MODULE_ECHO_CANCEL = "module_echo_cancel"
    """Linux PulseAudio / PipeWire ``module-echo-cancel`` family.
    Equivalent in scope to the Windows APO chain but lives in user-space
    sound-server modules rather than kernel APOs.
    """

    PIPEWIRE_FILTER_CHAIN = "pipewire_filter_chain"
    """Linux PipeWire filter-chain node — operator-applied DSP between
    the kernel device and the PulseAudio compat layer.
    """

    WIREPLUMBER_DEFAULT_SOURCE = "wireplumber_default_source"
    """Linux WirePlumber session-manager default-source policy — the
    PolicyKit-aware default-input routing layer.
    """

    ALSA_CAPTURE_CHAIN = "alsa_capture_chain"
    """Linux ALSA mixer / element capture chain — the lowest-level
    pre-PipeWire / pre-PulseAudio Linux capture path.
    """

    COREAUDIO_VOICE_PROCESSING = "coreaudio_voice_processing"
    """macOS CoreAudio VoiceProcessing Audio Unit — the lower-level
    macOS sibling of Voice Isolation, exposed at the AUv3 layer.
    """

    NOOP = "noop"
    """No capture-chain processing was attempted, or the strategy list
    did not map to any known family. The bypass cascade may still have
    run; this value signals that the cascade itself was either empty or
    the strategy-name prefixes were unrecognised by the resolver.
    """


# Strategy-name prefix → family mapping. The resolver scans this dict
# in iteration order; the FIRST matching prefix wins. Ordering matters
# for cases where a longer prefix is a superset of a shorter one — list
# longer prefixes first (defensive; today no such overlap exists).
_STRATEGY_PREFIX_TO_FAMILY: Final[Mapping[str, PlatformAudioFamily]] = {
    "linux.alsa_": PlatformAudioFamily.ALSA_CAPTURE_CHAIN,
    "linux.pipewire_": PlatformAudioFamily.PIPEWIRE_FILTER_CHAIN,
    "linux.wireplumber_": PlatformAudioFamily.WIREPLUMBER_DEFAULT_SOURCE,
    "linux.session_manager_": PlatformAudioFamily.WIREPLUMBER_DEFAULT_SOURCE,
    "linux.module_echo_cancel_": PlatformAudioFamily.MODULE_ECHO_CANCEL,
    "win.voice_clarity_": PlatformAudioFamily.VOICE_CLARITY,
    "win.wasapi_exclusive_": PlatformAudioFamily.VOICE_CLARITY,
    "win.": PlatformAudioFamily.VOICE_CLARITY,
    "darwin.voice_isolation_": PlatformAudioFamily.VOICE_ISOLATION,
    "darwin.coreaudio_": PlatformAudioFamily.COREAUDIO_VOICE_PROCESSING,
    "darwin.": PlatformAudioFamily.VOICE_ISOLATION,
}


PlatformToken = Literal["linux", "windows", "darwin", "other"]
"""Public type alias for the platform token surfaced by
:func:`current_platform_token` and the ``voice.platform`` event
attribute. ``"other"`` is the catch-all for unrecognised
``sys.platform`` values (FreeBSD, WSL on niche kernels, etc.).
"""


def resolve_family_from_strategy_name(name: str) -> PlatformAudioFamily:
    """Map a single strategy name to its capture-chain family.

    Returns :attr:`PlatformAudioFamily.NOOP` for an unrecognised
    prefix; the resolver is total + never raises. Callers MAY emit a
    structured WARN on a high NOOP rate but the resolver itself is
    pure.
    """
    for prefix, family in _STRATEGY_PREFIX_TO_FAMILY.items():
        if name.startswith(prefix):
            return family
    return PlatformAudioFamily.NOOP


def resolve_family_from_strategies(names: Sequence[str]) -> PlatformAudioFamily:
    """Reduce a strategy list to a single family via majority vote.

    Empty input → :attr:`PlatformAudioFamily.NOOP`. Ties resolve by
    first-match per :class:`collections.Counter.most_common` insertion-
    order semantics — deterministic across runs.

    Mixed-platform strategy lists (e.g. one ``linux.*`` and one
    ``win.*`` in the same cascade) would be a structural bug elsewhere;
    this resolver returns whichever family wins the majority vote and
    leaves the bug-flagging to the wrapper helper which emits a
    ``voice.capture_integrity.mixed_platform_strategies`` WARN when it
    detects > 1 distinct family with non-zero counts and the second
    family share exceeds ``_MIXED_PLATFORM_THRESHOLD_RATIO``.
    """
    if not names:
        return PlatformAudioFamily.NOOP
    counts: Counter[PlatformAudioFamily] = Counter()
    for n in names:
        counts[resolve_family_from_strategy_name(n)] += 1
    # ``most_common(1)`` is well-defined for non-empty counters; the
    # NOOP-only case (every strategy unrecognised) returns NOOP cleanly.
    return counts.most_common(1)[0][0]


_PLATFORM_PREFIXES: Final[frozenset[str]] = frozenset({"linux.", "win.", "darwin."})
"""Recognised strategy-name platform prefixes — used by the
mixed-platform detector to distinguish "different families within one
platform" (legitimate Linux cascade chains often mix ALSA + PipeWire +
WirePlumber families on the same OS) from "different platforms in one
cascade" (a structural bug).
"""


def _strategy_platform(name: str) -> str | None:
    """Return the platform-prefix token (e.g. ``"linux"``) or None if
    the strategy name doesn't carry a recognised platform prefix.
    """
    for prefix in _PLATFORM_PREFIXES:
        if name.startswith(prefix):
            return prefix.rstrip(".")
    return None


def is_mixed_platform_strategy_list(names: Sequence[str]) -> bool:
    """True if the strategy list spans MORE THAN ONE recognised platform.

    Different families within the same platform (e.g. ALSA + PipeWire
    on Linux) are NOT mixed — the bypass cascade legitimately exercises
    multiple per-platform mitigation surfaces. Mixed-platform means the
    strategy list contains, say, both ``linux.alsa_*`` AND ``win.voice_clarity_*``
    in the same cascade, which would be a structural bug elsewhere.

    Used by the dual-emission wrapper to emit a structured WARN when
    the input cascade looks structurally wrong.
    """
    if len(names) < 2:
        return False
    platforms: set[str] = set()
    for n in names:
        plat = _strategy_platform(n)
        if plat is not None:
            platforms.add(plat)
    return len(platforms) > 1


@functools.cache
def current_platform_token() -> PlatformToken:
    """Resolve the current process platform to one of the 4 stable
    tokens.

    ``functools.cache`` guarantees the resolution happens once per
    process; ``sys.platform`` is process-immutable so this is safe.
    Tests that monkeypatch ``sys.platform`` MUST call
    :func:`current_platform_token.cache_clear` between cases to bypass
    the cache.
    """
    plat = sys.platform
    if plat == "win32":
        return "windows"
    if plat == "darwin":
        return "darwin"
    if plat.startswith("linux"):
        return "linux"
    return "other"
