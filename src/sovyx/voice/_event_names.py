"""Canonical platform-neutral capture-integrity event names.

Single source of truth for the 5 cross-platform capture-integrity event
names emitted by the bypass coordinator. Each member carries the dotted-
namespace neutral name; the module-level ``LEGACY_TWIN_MAP`` records the
legacy event-name twin that dual-emission preserves through the staged-
adoption window per ADR-D14.

Mission anchor: ``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T1.1. Closes the canonical instance of CLAUDE.md anti-pattern #39(b)
(cross-platform event-name drift) — pre-H2 the bypass coordinator
emitted ``audio.apo.bypassed`` / ``voice_apo_bypass_activated`` etc. on
Linux where ``voice_clarity_active=False`` and every cascade strategy was
``linux.alsa_*`` / ``linux.pipewire_*`` / ``linux.wireplumber_*``. The
event-name embedded Windows-platform terminology in cross-platform code.

Anti-pattern #9: ``StrEnum`` (not plain ``Enum``) so value-based
comparison + xdist namespace safety hold. Anti-pattern #16:
``__init__.py`` re-exports keep ``from sovyx.voice import
CaptureIntegrityEvent`` working post-split.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping


class CaptureIntegrityEvent(StrEnum):
    """Five neutral event names emitted by the bypass coordinator dispatch.

    Iteration order = canonical emission order from
    :mod:`sovyx.voice.pipeline._bypass_coordinator_mixin`'s three
    logical branches (success / callback exception / exhausted) plus
    the OTel parent ``voice.capture_integrity.bypass`` referenced from
    :file:`docs/observability.md` table row.
    """

    BYPASS = "voice.capture_integrity.bypass"
    """OTel semconv parent — the canonical event name for any
    capture-integrity bypass dispatch. Replaces legacy ``voice.apo.bypass``.
    """

    BYPASSED = "voice.capture_integrity.bypassed"
    """Verdict-tagged terminal event emitted from the success / failure /
    exhausted branches of :meth:`_invoke_deaf_signal`. Carries the
    ``voice.verdict`` attribute (``"success"`` / ``"failure"`` /
    ``"partial"``). Replaces legacy ``audio.apo.bypassed``.
    """

    BYPASS_ACTIVATED = "voice.capture_integrity.bypass_activated"
    """Emitted on ``BypassVerdict.APPLIED_HEALTHY`` outcome — the bypass
    cascade succeeded. Replaces legacy ``voice_apo_bypass_activated``.
    """

    BYPASS_INEFFECTIVE = "voice.capture_integrity.bypass_ineffective"
    """Emitted when the coordinator exhausted every eligible strategy
    without recovery. Replaces legacy ``voice_apo_bypass_ineffective``.
    """

    BYPASS_FAILED = "voice.capture_integrity.bypass_failed"
    """Emitted when the coordinator callback itself raised. Replaces
    legacy ``voice_apo_bypass_failed``.
    """


LEGACY_TWIN_MAP: Final[Mapping[CaptureIntegrityEvent, str]] = {
    CaptureIntegrityEvent.BYPASS: "voice.apo.bypass",
    CaptureIntegrityEvent.BYPASSED: "audio.apo.bypassed",
    CaptureIntegrityEvent.BYPASS_ACTIVATED: "voice_apo_bypass_activated",
    CaptureIntegrityEvent.BYPASS_INEFFECTIVE: "voice_apo_bypass_ineffective",
    CaptureIntegrityEvent.BYPASS_FAILED: "voice_apo_bypass_failed",
}
"""1:1 mapping from neutral event name to its legacy twin.

The dual-emission wrapper :func:`emit_capture_integrity_event` looks up
the legacy name here so callers never hand-write the legacy string
literal. Phase 3 STRICT (v0.51.0) drops the legacy emission block + this
map; consumers grep for this module name to find the removal site.

Module-level ``Final`` declaration so mypy strict + downstream tooling
treat this as immutable; the registry test asserts that every
:class:`CaptureIntegrityEvent` member appears as a key.
"""


CAPTURE_INTEGRITY_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {e.value for e in CaptureIntegrityEvent}
)
"""Frozenset of all neutral event-name string values.

Used by the AST scanner (``scripts/dev/check_platform_neutral_event_names.py``)
to short-circuit emit-site detection on canonical neutral names and by
the dual-emission wrapper to validate inputs.
"""


LEGACY_EVENT_NAMES: Final[frozenset[str]] = frozenset(LEGACY_TWIN_MAP.values())
"""Frozenset of all legacy event-name string values.

Used by tests asserting dual-emission ratio (F2 telemetry gate) and by
the Phase 3 STRICT removal checker.
"""
