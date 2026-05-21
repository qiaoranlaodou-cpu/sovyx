"""Mission B B-P0-2 — production producer for `record_exception_cohort`.

Mission anchor:
``docs-internal/MISSION-B-FINDINGS-REGISTER-2026-05-21.md`` §1 (B-P0-2) +
``docs-internal/MISSION-B-REMEDIATION-PLAN-2026-05-21.md`` §5 (B.1.P2).
Closes also Mission A.3 spec §A.3.P3 / F-022 — the structural-hardening
spec scheduled this wire-up for v0.50.1; Mission B B.1.P2 brings it
forward to v0.49.37 because A.1.P2's window-decay closure was
operationally vacuous without the producer wire (EXCEPTION_COHORT
cohort verdict was permanently HEALTHY by construction).

Design:

* Single chokepoint via :class:`ExceptionTreeProcessor` — captures
  every ``logger.exception(...)`` (59 sites across 30 files at HEAD)
  WITHOUT per-call-site touches.
* ``group_id`` follows the Mission A.3.P3 SSoT shape:
  ``f"{exception_type_name}@{int(time.monotonic())}"`` — 1-second
  monotonic-rounded so chain-walk duplicates within the same second
  collapse to one cohort observation. The registry's dedup logic
  (see :class:`_ExceptionCohortCounter` docstring) enforces this.
* ``retained_bytes_estimate`` uses the Mission A.3.P3 heuristic:
  ``sum(len(line.encode("utf-8")) for line in traceback.format_exception(...))``.
  Coarse but upper-bounded; consistent across runs of the same
  failure class.
* Gated by ``observability.features.exception_cohort_recording``
  read by the processor at construction time. Default False at
  v0.49.37 per `feedback_staged_adoption` foundation→adoption→flip;
  default-flip to True scheduled for v0.49.38.
* Failures absorbed — observability-of-observability rule (§27.4).
  A serializer error here MUST NOT crash the caller's
  ``logger.exception(...)`` call.
"""

from __future__ import annotations

import time
import traceback

from sovyx.observability._resource_registry import record_exception_cohort


def record_from_exception(exc: BaseException) -> None:
    """Record an exception observation into the cohort registry.

    Hot-path: called from inside :class:`ExceptionTreeProcessor.__call__`
    for every structlog record carrying ``exc_info``. The processor
    has already extracted the :class:`BaseException` — this helper
    derives the group_id + retained-bytes estimate and dispatches.

    Returns ``None`` on success OR on any internal error (observability
    paths MUST NEVER raise into the caller).
    """
    if exc is None:
        return
    try:
        exc_type = type(exc).__name__
        # 1-second monotonic-rounded — matches Mission A.3.P3 SSoT shape.
        # Within the same second, chain-walk duplicates collapse to ONE
        # cohort observation (the registry's dedup window also enforces
        # this at the lock layer — defence in depth).
        first_seen = int(time.monotonic())
        group_id = f"{exc_type}@{first_seen}"
        # Upper-bound estimate from the formatted traceback. We pass
        # the explicit (type, value, tb) form so the helper works even
        # when called outside an active ``except`` block (e.g. when a
        # structlog record carries a previously-captured exception via
        # ``exc_info=<instance>``).
        formatted = traceback.format_exception(type(exc), exc, exc.__traceback__)
        retained = sum(len(line.encode("utf-8")) for line in formatted)
        record_exception_cohort(
            group_id=group_id,
            sub_exception_count=1,
            retained_bytes_estimate=retained,
        )
    except Exception:  # noqa: BLE001 — observability-of-observability; never crash the caller
        # Defensive: a bug in this helper must not be able to crash the
        # caller's ``logger.exception(...)`` call. We do NOT re-emit a
        # log here because logging during the structlog pipeline would
        # invite recursion; the next governor tick will simply observe
        # one missing observation.
        return


__all__ = ["record_from_exception"]
