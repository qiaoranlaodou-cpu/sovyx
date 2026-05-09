"""Probe invocation surface for the cascade executor.

Phase 5.F.16 god-file extraction from
``voice/health/cascade/_executor.py`` (anti-pattern #16). Owns the
4 symbols that the cascade entry points use to invoke probes:

* :class:`ProbeCallable` — Protocol typing the structural shape of a
  cascade-driven probe (Tests inject a fake matching this shape;
  production calls ``sovyx.voice.health.probe.probe``).
* :func:`_call_probe` — thin wrapper that trims the probe's optional-
  kwarg surface to the four kwargs the cascade explicitly drives.
  Keeps test mocks small.
* :func:`_try_combo` — per-attempt probe invocation with the
  belt-and-braces ``Exception`` → ``Diagnosis`` classifier fallback
  (T6.5 / R2 HIGH). Synthesises a ``DRIVER_ERROR`` ``ProbeResult``
  for genuine probe-side bugs without aborting the cascade.
* :data:`_PHYSICAL_CURE_DIAGNOSES` — frozen set of diagnoses where
  the cascade short-circuits because the failure is below the
  host-API layer (KERNEL_INVALIDATED / STREAM_OPEN_TIMEOUT). Used by
  the cascade walk to quarantine + skip remaining combos.

Anti-pattern #20 covered: parent module
``voice/health/cascade/_executor.py`` re-exports every symbol so the
public consumer at ``cascade/__init__.py`` (which exports
``ProbeCallable``) and the in-parent call sites in ``run_cascade`` /
``run_cascade_for_candidates`` / ``_run_cascade_locked`` continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_probe_result
from sovyx.voice.health.cascade._executor_helpers import _combo_tag
from sovyx.voice.health.contract import (
    Diagnosis,
    ProbeResult,
)
from sovyx.voice.health.probe import (
    _classify_open_error,
)

if TYPE_CHECKING:
    from sovyx.voice.health.contract import (
        Combo,
        ProbeMode,
    )

logger = get_logger(__name__)


# T6.9 — diagnoses that share the "physical cure required" semantic
# with KERNEL_INVALIDATED. The cascade short-circuits on these
# (quarantine + return) instead of trying remaining combos because
# the failure is below the host-API layer — every alternative combo
# will fail identically until the user replugs / reboots.
#
# - KERNEL_INVALIDATED: IAudioClient::Initialize stuck, surfaces as
#   paInvalidDevice / AUDCLNT_E_DEVICE_INVALIDATED (Windows-canonical).
# - STREAM_OPEN_TIMEOUT (T6.2): driver accepted open + start but
#   never delivered audio in ≥ 5 s. Same root-cause family observed
#   via the callback-not-fired surface.
_PHYSICAL_CURE_DIAGNOSES: frozenset[Diagnosis] = frozenset(
    {
        Diagnosis.KERNEL_INVALIDATED,
        Diagnosis.STREAM_OPEN_TIMEOUT,
    },
)


class ProbeCallable(Protocol):
    """Structural type for the probe function used by the cascade.

    Tests inject a fake matching this shape; production calls
    :func:`sovyx.voice.health.probe.probe`.
    """

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,
        hard_timeout_s: float,
    ) -> ProbeResult: ...


async def _call_probe(
    probe_fn: ProbeCallable,
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    hard_timeout_s: float,
) -> ProbeResult:
    """Invoke the probe with just the cascade's required kwargs.

    Trims the interface so tests don't have to mock every optional
    keyword of :func:`sovyx.voice.health.probe.probe` — only the four
    that the cascade explicitly drives are forwarded.
    """
    return await probe_fn(
        combo=combo,
        mode=mode,
        device_index=device_index,
        hard_timeout_s=hard_timeout_s,
    )


async def _try_combo(
    *,
    probe_fn: ProbeCallable,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    attempt_budget_s: float,
) -> ProbeResult:
    """Invoke the probe and convert unexpected exceptions into DRIVER_ERROR results.

    The probe already classifies all known PortAudio failures into the
    :class:`Diagnosis` enum. This wrapper guards against a probe-side
    bug / test misconfiguration turning into a cascade abort — any
    exception becomes a synthetic DRIVER_ERROR so the cascade can
    still fall through.
    """
    try:
        return await _call_probe(
            probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            hard_timeout_s=attempt_budget_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Belt-and-braces: after v0.20.2 Phase 1, the probe classifies
        # stream.start() failures internally, so this path should only
        # fire for genuine probe-side bugs (numpy errors in analysis,
        # test misconfiguration). Still, running the classifier on the
        # raised exception recovers the correct Diagnosis when a future
        # probe-side bug re-introduces a leak (e.g. a kernel-invalidated
        # error escaping a new analysis phase), rather than silently
        # coarsening into DRIVER_ERROR.
        #
        # Gate the classifier on OSError (PortAudio surfaces failures as
        # ``sd.PortAudioError(OSError)``) so an unrelated coding-bug
        # ``TypeError("... format ...")`` or ``AttributeError`` whose
        # message accidentally contains a keyword like "format" / "in use"
        # / "access" cannot be misclassified as a structured Diagnosis.
        # Non-OSError stays DRIVER_ERROR — the original cascade contract.
        if isinstance(exc, OSError):
            # T6.5 — pass combo so a rate-only error with
            # auto_convert=False routes to the
            # INVALID_SAMPLE_RATE_NO_AUTO_CONVERT diagnosis instead
            # of FORMAT_MISMATCH.
            diagnosis = _classify_open_error(exc, combo=combo)
        else:
            diagnosis = Diagnosis.DRIVER_ERROR
        logger.error(
            "voice_cascade_probe_raised",
            host_api=combo.host_api,
            combo=_combo_tag(combo),
            diagnosis=str(diagnosis),
            error=repr(exc),
            exc_info=True,
        )
        synthetic = ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=f"probe raised: {exc!r}",
        )
        # Also emit the probe-result telemetry so synthetic results
        # appear in the same dashboards as first-class probe outcomes.
        record_probe_result(synthetic)
        return synthetic


__all__ = [
    "ProbeCallable",
    "_PHYSICAL_CURE_DIAGNOSES",
    "_call_probe",
    "_try_combo",
]
