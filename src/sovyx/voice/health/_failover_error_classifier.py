"""PortAudio open-error → failover dispatch policy classifier.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.3.

Maps the raw PortAudio numeric codes + HRESULT mnemonics + opener
final-code mnemonics into a coarse-grained
:class:`FailoverErrorClass` enum that drives the loop-in-place
candidate-skip / candidate-retry decisions inside
:func:`sovyx.voice.health._runtime_failover._try_runtime_failover`
(Mission C3 §T1.1) and the probe-result-cache short-circuit at
:meth:`sovyx.voice.health._probe_result_cache.ProbeResultCache.is_known_unopenable`
(Mission C3 §T2.1).

Decision policy (ADR-D4):

* ``UNOPENABLE_PERMANENT`` — the candidate is structurally invalid
  for the remainder of this boot cycle. ``-9996 paInvalidDevice``
  is the canonical case (the OS device index has been invalidated;
  USB-unplug, kernel device-list mutation, etc.). The ladder skips
  this candidate via ``select_alternative_endpoint`` exclusion.
* ``UNOPENABLE_THIS_BOOT`` — the candidate's driver state currently
  rejects opens but may recover later in this boot cycle (e.g.
  ``-9985 paDeviceUnavailable`` from PipeWire session-manager
  contention, ``-9988 paBadIODeviceCombination`` from a host-API
  combo rejection). The ladder skips it for THIS ladder run; future
  runs (after the next deaf-signal heartbeat past the outer cooldown)
  re-probe.
* ``FORMAT_RETRYABLE_SAME_DEVICE`` — the opener's sample-rate /
  channel permutation pyramid handles this without help from the
  ladder. ``-9986 paInvalidSampleRate``,
  ``AUDCLNT_E_UNSUPPORTED_FORMAT``. The ladder DOES NOT skip on this
  class — the opener already retries.
* ``TRANSIENT_RETRYABLE_SAME_DEVICE`` — another app holds an
  exclusive lock OR a one-off host-API hiccup. The ladder retries
  the same device with shared-mode / host-API rotation handled by
  the opener; the failover layer DOES NOT skip.
* ``UNKNOWN`` — opaque code; conservative default. The cache treats
  this as "do not skip" to avoid false negatives (preserving the
  pre-Mission-C3 behaviour where every candidate gets tried).

Note: this classifier is the FAILOVER-layer policy lens. The
SAME PortAudio code maps to a different action at the
:mod:`sovyx.voice._error_messages` (opener) layer (e.g. ``-9985`` is
``DEVICE_DISCONNECTED`` at the opener layer because the opener can't
distinguish "USB unplugged" from "session-manager grabbed"; the
failover layer DOES make that distinction via repeated-occurrence
tracking inside the probe-result cache).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class FailoverErrorClass(StrEnum):
    """Failover-layer dispatch policy for a PortAudio open verdict.

    See module docstring for the per-class semantics.
    """

    TRANSIENT_RETRYABLE_SAME_DEVICE = "transient_retryable_same_device"
    FORMAT_RETRYABLE_SAME_DEVICE = "format_retryable_same_device"
    UNOPENABLE_THIS_BOOT = "unopenable_this_boot"
    UNOPENABLE_PERMANENT = "unopenable_permanent"
    UNKNOWN = "unknown"


# Canonical code tables. Each token is matched after lowercasing
# + stripping. The sets are immutable; a future contributor adding
# a new code MUST add a unit test under
# ``tests/unit/voice/health/test_failover_error_classifier.py``.

_PERMANENT_CODES: Final[frozenset[str]] = frozenset(
    {
        "-9996",
        "painvaliddevice",
    },
)

_THIS_BOOT_CODES: Final[frozenset[str]] = frozenset(
    {
        "-9985",
        "padeviceunavailable",
        "-9988",
        "pabadiodevicecombination",
    },
)

_FORMAT_RETRY_CODES: Final[frozenset[str]] = frozenset(
    {
        "-9986",
        "painvalidsamplerate",
        "audclnt_e_unsupported_format",
        "0x88890008",
        "-2004287480",
    },
)

_TRANSIENT_CODES: Final[frozenset[str]] = frozenset(
    {
        "-9999",
        "paunanticipatedhosterror",
        "audclnt_e_device_in_use",
        "0x8889000a",
        "-2004287478",
    },
)

# Final-code mnemonics emitted by the opener at the
# ``voice_stream_open_failed final_code=...`` event. These are coarser
# than the raw PortAudio codes (the opener aggregates the per-attempt
# raw codes into a single final-code label). They map at the failover
# layer to:
#
#   device_not_found  → UNOPENABLE_THIS_BOOT (the opener exhausted
#                       every permutation; ladder skip for this boot)
#   device_busy       → TRANSIENT_RETRYABLE_SAME_DEVICE
#   device_unavailable → UNOPENABLE_THIS_BOOT
#   permission_denied → UNOPENABLE_THIS_BOOT (cannot recover without
#                       operator OS-level intervention)
_FINAL_CODE_PERMANENT: Final[frozenset[str]] = frozenset(
    {
        "device_disconnected",
    },
)
_FINAL_CODE_THIS_BOOT: Final[frozenset[str]] = frozenset(
    {
        "device_not_found",
        "device_unavailable",
        "permission_denied",
        "service_not_running",
    },
)
_FINAL_CODE_TRANSIENT: Final[frozenset[str]] = frozenset(
    {
        "device_in_use",
        "device_busy",
        "driver_failure",
    },
)
_FINAL_CODE_FORMAT_RETRY: Final[frozenset[str]] = frozenset(
    {
        "unsupported_format",
        "buffer_size_error",
        "exclusive_mode_denied",
    },
)


def classify_error_code(
    error_code: str,
    error_detail: str = "",
) -> FailoverErrorClass:
    """Classify a single open-failure code into a failover dispatch policy.

    Mission C3 §T2.3 — pure function, no side effects, no I/O. Safe
    to call from inside a hot loop, deterministic across calls.

    Args:
        error_code: Raw PortAudio code (``"-9985"``), HRESULT mnemonic
            (``"audclnt_e_device_in_use"`` / ``"0x8889000a"``), or
            opener final-code mnemonic (``"device_not_found"``). Any
            of the three forms is accepted; the classifier
            disambiguates by token lookup. Empty / ``None`` yields
            :attr:`FailoverErrorClass.UNKNOWN`.
        error_detail: Optional free-text detail (e.g. PortAudio
            ``"Expression 'AlsaOpen' failed in pa_linux_alsa.c:1904"``
            stderr message, or the ``DeviceChangeRestartResult.detail``
            free-text). Used ONLY as a fallback when ``error_code``
            does not match any known token — protects against opaque
            host-API drivers that surface human-readable strings
            instead of canonical codes.

    Returns:
        A :class:`FailoverErrorClass` member. Total function; never
        raises.
    """
    code = (error_code or "").strip().lower()
    detail = (error_detail or "").lower()

    # Code-table lookups (most specific first — PortAudio numerics
    # are the canonical form, HRESULT mnemonics are next, opener
    # final-code mnemonics are last). Final-code mnemonics overlap
    # with PortAudio token names in lowercase (e.g.
    # ``"device_disconnected"`` is BOTH an AudioErrorClass name AND a
    # plausible classifier-input string); the tables here are
    # explicit so the disambiguation is documented.
    if code in _PERMANENT_CODES or code in _FINAL_CODE_PERMANENT:
        return FailoverErrorClass.UNOPENABLE_PERMANENT
    if code in _THIS_BOOT_CODES or code in _FINAL_CODE_THIS_BOOT:
        return FailoverErrorClass.UNOPENABLE_THIS_BOOT
    if code in _FORMAT_RETRY_CODES or code in _FINAL_CODE_FORMAT_RETRY:
        return FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE
    if code in _TRANSIENT_CODES or code in _FINAL_CODE_TRANSIENT:
        return FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE

    # Detail-string fallback for opaque PortAudio errors. Match
    # operator-readable strings the opener may surface when no
    # canonical code is available. Order matters — most-specific
    # phrases first.
    if "invalid device" in detail:
        return FailoverErrorClass.UNOPENABLE_PERMANENT
    if "device unavailable" in detail or "device disconnected" in detail:
        return FailoverErrorClass.UNOPENABLE_THIS_BOOT
    if "invalid sample rate" in detail or "format not supported" in detail:
        return FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE
    if (
        "device is busy" in detail
        or "device in use" in detail
        or "device or resource busy" in detail
    ):
        return FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE

    return FailoverErrorClass.UNKNOWN


def is_skip_candidate_class(cls: FailoverErrorClass) -> bool:
    """Return ``True`` if the class means "skip this candidate now".

    Mission C3 §T2.4 — convenience predicate used by the failover
    loop body and the probe-result cache. Encapsulates ADR-D4 in
    one place so future tuning of the skip policy is a single-line
    change.

    Returns ``True`` for :attr:`UNOPENABLE_PERMANENT` and
    :attr:`UNOPENABLE_THIS_BOOT`; ``False`` for the retryable
    classes and ``UNKNOWN``. ``UNKNOWN`` deliberately falls into
    the "don't skip" bucket — the conservative default preserves
    pre-Mission-C3 behaviour (every candidate gets tried) when
    the classifier cannot make a confident call.
    """
    return cls in (
        FailoverErrorClass.UNOPENABLE_PERMANENT,
        FailoverErrorClass.UNOPENABLE_THIS_BOOT,
    )


__all__ = [
    "FailoverErrorClass",
    "classify_error_code",
    "is_skip_candidate_class",
]
