"""Resolve the operator's active mic to an ALSA card index.

v0.31.5 LE-1 closure: complete the GAP 5 wire-up. v0.31.4 added the
``active_mic_card_index`` parameter to :class:`CalibrationApplier`
and :func:`capture_measurements` but no production caller passed it;
in production every site got ``None`` → fallback ``candidates[0]`` →
R10 still boosted the wrong physical mic on multi-mic homes.

This helper bridges the gap: callers feed it the operator's persisted
:class:`MindConfig` and it returns the ALSA card index that owns the
operator's active capture device, by parsing ``arecord -l`` output
and substring-matching against ``MindConfig.voice_input_device_name``.

Returns ``None`` defensively when:
- ``mind_config`` is None or has no ``voice_input_device_name`` field;
- ``voice_input_device_name`` is empty (operator hasn't completed the
  setup wizard yet);
- ``arecord`` is not installed (non-Linux, missing alsa-utils);
- no ALSA card name matches the persisted device name.

The ``None`` return is ALWAYS safe — ``CalibrationApplier`` and
``capture_measurements`` both interpret it as "no preference", which
preserves the v0.31.3 first-attenuated-card behaviour. The new
behaviour activates only when this resolver finds a real match.

History: introduced in v0.31.5 to complete v0.31.4 GAP 5.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_ARECORD_TIMEOUT_S = 5.0

# Each ``arecord -l`` line for a card looks like:
#   ``card 2: Pro [Razer BlackShark V2 Pro], device 0: USB Audio [USB Audio]``
# Capture (a) the integer card index and (b) the bracketed display
# name. Match is case-insensitive on the bracketed name only — the
# card prefix (``"Pro "`` above) is just an ALSA short-id and is not
# what the operator's wizard persisted.
_CARD_LINE_RE = re.compile(
    r"^card\s+(?P<index>\d+):\s+\S+\s+\[(?P<name>[^\]]+)\]",
    re.IGNORECASE | re.MULTILINE,
)


def resolve_active_mic_card(*, mind_config: Any) -> int | None:  # noqa: ANN401
    """Map ``MindConfig.voice_input_device_name`` to an ALSA card index.

    Args:
        mind_config: The mind whose persisted mic to resolve. May be
            ``None`` (CLI doctor invocations without mind context);
            return ``None`` defensively in that case.

    Returns:
        The integer ALSA card index whose name matches the operator's
        persisted ``voice_input_device_name`` (substring match,
        case-insensitive), or ``None`` when the mapping cannot be
        established. Callers MUST treat ``None`` as "no preference"
        and preserve their pre-v0.31.4 fallback behaviour.

    Side effects:
        Emits structured ``voice.calibration.active_mic_unresolved``
        log at INFO level when ``None`` is returned, with a closed-
        enum ``reason`` so operators correlating logs can see WHY the
        active-mic preference didn't apply (no_persisted_name /
        arecord_unavailable / no_match).
    """
    if mind_config is None:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="no_mind_config",
        )
        return None
    persisted_name = getattr(mind_config, "voice_input_device_name", "") or ""
    if not persisted_name.strip():
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="no_persisted_name",
        )
        return None
    if shutil.which("arecord") is None:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_unavailable",
        )
        return None
    try:
        completed = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=_ARECORD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_failed",
            detail=str(exc)[:200],
        )
        return None
    if completed.returncode != 0:
        logger.info(
            "voice.calibration.active_mic_unresolved",
            reason="arecord_nonzero_exit",
            exit_code=completed.returncode,
        )
        return None

    # Substring-match the persisted mic name against each card's
    # bracketed display name. The match is case-insensitive so
    # operator's "Razer" matches "Razer BlackShark V2 Pro".
    needle = persisted_name.lower()
    for match in _CARD_LINE_RE.finditer(completed.stdout):
        card_name = match.group("name").lower()
        if needle in card_name or card_name in needle:
            return int(match.group("index"))

    logger.info(
        "voice.calibration.active_mic_unresolved",
        reason="no_match",
        persisted_name_hash=_short_name_hash(persisted_name),
    )
    return None


def _short_name_hash(value: str) -> str:
    """Stable 16-hex prefix of SHA256(value) — for log correlation
    without leaking the operator's mic name verbatim.

    Mirrors :func:`sovyx.observability.privacy.short_hash` — kept as
    a local helper so this module has no inter-package dependency
    chain (the caller chain in calibration is already deep).
    """
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
