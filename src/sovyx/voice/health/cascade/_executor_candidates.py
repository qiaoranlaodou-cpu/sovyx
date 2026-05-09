"""Candidate-set cascade entry point — VLX-002 mission.

Phase 5.F.17 god-file extraction from
``voice/health/cascade/_executor.py`` (anti-pattern #16). Owns the
:func:`run_cascade_for_candidates` cross-endpoint entry point that
the ``voice-linux-cascade-root-fix`` mission introduced for
session-manager-escape paths at boot time on Linux (VLX-002).

Division of labour vs. ``run_cascade``:

* ``run_cascade`` (parent ``_executor.py``) — cross-combo, single
  endpoint. Pinned override → ComboStore fast-path → platform
  cascade table walk.
* :func:`run_cascade_for_candidates` (this module) — cross-endpoint,
  delegates to ``run_cascade`` per candidate. Source of truth for
  the session-manager-escape path (VLX-002).

Anti-pattern #20 covered: parent module
``voice/health/cascade/_executor.py`` re-exports
:func:`run_cascade_for_candidates` so the public consumer at
``cascade/__init__.py`` + the wire-up call site at
``voice/health/_factory_integration.py`` continue to resolve via
standard module-namespace lookup.

Circular-import design: this module imports ``run_cascade`` from the
parent ``_executor`` module ONLY lazily inside the function body —
parent's top-level re-export of this function runs AFTER
``run_cascade`` is defined, so the cycle is broken at import time.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health.cascade._alignment import (
    _run_mixer_sanity,
)
from sovyx.voice.health.cascade._budget import (
    _DEFAULT_ATTEMPT_BUDGET_S,
    _DEFAULT_TOTAL_BUDGET_S,
)
from sovyx.voice.health.cascade._planner import (
    LINUX_CASCADE,
    build_linux_cascade_for_device,
)
from sovyx.voice.health.contract import (
    CascadeResult,
    ProbeMode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.engine._lock_dict import LRULockDict
    from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health._quarantine import EndpointQuarantine
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.cascade._executor_probe import ProbeCallable
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        Combo,
        ProbeResult,
    )

logger = get_logger(__name__)


async def run_cascade_for_candidates(
    *,
    candidates: Sequence[CandidateEndpoint],
    mode: ProbeMode,
    platform_key: str,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the cascade against an ordered set of capture candidates.

    This is the candidate-set entry point introduced by the
    ``voice-linux-cascade-root-fix`` mission (VLX-002). It iterates the
    caller-supplied :class:`~sovyx.voice.health.contract.CandidateEndpoint`
    list in order, delegating each to :func:`run_cascade` with the
    candidate's per-endpoint identity. The first healthy winner wins.

    Division of labour vs. :func:`run_cascade`:

    * :func:`run_cascade` — cross-combo, single endpoint. Pinned →
      ComboStore fast-path → platform cascade table walk.
    * :func:`run_cascade_for_candidates` — cross-endpoint, delegates to
      :func:`run_cascade` per candidate. Source of truth for the
      session-manager-escape path at boot time on Linux (VLX-002).

    The total wall-clock ``total_budget_s`` is shared across all
    candidates. Each :func:`run_cascade` call gets the remaining budget,
    so the last candidate may get a shorter window than the first. This
    matches the pre-refactor behaviour (one endpoint, one budget) when
    called with ``len(candidates) == 1``.

    Args:
        candidates: Ordered list from
            :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`.
            Must be non-empty; the first candidate is the user-preferred
            one (``CandidateSource.USER_PREFERRED``).
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``.
        combo_store: Persistent fast-path store — forwarded verbatim to
            each :func:`run_cascade` invocation. Each candidate hits the
            store under its own ``endpoint_guid``, so a stored combo for
            ``pipewire`` is still consulted when the user-preferred
            hardware candidate's own fast-path is stale.
        capture_overrides: User-pinned combos — forwarded verbatim.
        probe_fn: Probe entry point. Defaults to
            :func:`~sovyx.voice.health.probe.probe`.
        lifecycle_locks: Per-endpoint lock dict. Each candidate gets its
            own lock; parallel invocations of this function against
            disjoint candidate sets do not serialize.
        total_budget_s: Shared wall-clock budget across all candidates.
            On exhaustion the function returns ``budget_exhausted=True``
            with attempts from candidates tried so far.
        attempt_budget_s: Per-probe hard timeout.
        voice_clarity_autofix: Forwarded to each :func:`run_cascade` call.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: Shared quarantine store. All candidates check the
            same instance — a quarantined ``pipewire`` endpoint does
            not re-probe even if ``hw:1,0`` just finished quarantining.
        kernel_invalidated_failover_enabled: Master toggle for the
            §4.4.7 quarantine behaviour.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, L2.5 runs ONCE for the
            whole candidate-set pass (using the first candidate's
            identity) before the per-candidate cascade loop. The
            inner :func:`run_cascade` invocations receive
            ``mixer_sanity=None`` so healing is not re-attempted
            for every candidate. Default ``None`` preserves pre-L2.5
            behaviour.

    Returns:
        :class:`CascadeResult` with:

        * ``winning_candidate`` populated when any candidate produced a
          healthy combo.
        * ``endpoint_guid`` set to the winning candidate's guid, or the
          first candidate's guid on exhaustion (log correlation).
        * ``attempts`` containing the concatenation of every attempt
          across all tried candidates, in iteration order.

    Raises:
        ValueError: ``candidates`` is empty.
    """
    if not candidates:
        msg = "candidates must be non-empty (build_capture_candidates contract)"
        raise ValueError(msg)

    # Phase 5.F.17 — lazy import of run_cascade from parent _executor module
    # to break the import cycle (parent re-exports this candidates entry
    # point; we must defer until parent has finished loading run_cascade).
    from sovyx.voice.health.cascade._executor import run_cascade  # noqa: PLC0415

    deadline = clock() + total_budget_s
    aggregated_attempts: list[ProbeResult] = []
    total_attempts_count = 0
    last_result: CascadeResult | None = None

    logger.info(
        "voice_cascade_candidate_set_started",
        platform=platform_key,
        candidate_count=len(candidates),
        candidate_kinds=[str(c.kind) for c in candidates],
        candidate_sources=[str(c.source) for c in candidates],
    )

    # 2.5 — L2.5 mixer sanity runs ONCE per candidate-set pass (the ALSA
    # mixer is system-wide state; healing per-candidate would repeat work).
    # Uses the first candidate's identity for telemetry / endpoint_guid
    # (by candidate-builder contract that's the user-preferred one). We
    # pass mixer_sanity=None to the inner run_cascade calls so L2.5 does
    # NOT fire again under each per-endpoint lock — the healing already
    # happened (or was skipped) at this layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=candidates[0].endpoint_guid,
                device_index=candidates[0].device_index,
                device_friendly_name=candidates[0].friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancel propagates.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue
            logger.warning(
                "voice_cascade_candidate_set_mixer_sanity_raised",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # T4 — defensive invariant: dedup by (device_index, host_api_name)
    # must already hold (build_capture_candidates guarantees this), but
    # an ill-behaved injected builder in tests or a future refactor could
    # re-introduce collisions. Log-warn + continue rather than raise; the
    # cascade loop is already O(n×m) and probe idempotency absorbs dupes.
    seen_candidate_keys: set[tuple[int, str]] = set()

    for candidate_idx, candidate in enumerate(candidates):
        remaining = max(0.0, deadline - clock())
        if remaining <= 0.0:
            logger.warning(
                "voice_cascade_candidate_set_budget_exhausted",
                tried=candidate_idx,
                remaining_candidates=len(candidates) - candidate_idx,
            )
            break

        dedup_key = (candidate.device_index, candidate.host_api_name)
        if dedup_key in seen_candidate_keys:
            logger.warning(
                "voice_cascade_candidate_duplicate",
                candidate_rank=candidate.preference_rank,
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
            )
        seen_candidate_keys.add(dedup_key)

        logger.info(
            "voice_cascade_candidate_started",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            candidate_kind=str(candidate.kind),
            device_index=candidate.device_index,
            host_api=candidate.host_api_name,
            friendly_name=candidate.friendly_name,
            endpoint_guid=candidate.endpoint_guid,
            remaining_budget_s=remaining,
        )

        # T5 — per-candidate native-rate cascade. Only prepends when
        # the candidate is HARDWARE and reports a non-canonical rate
        # that the default Linux cascade would waste attempts on.
        per_candidate_cascade: Sequence[Combo] | None = None
        if platform_key == "linux":
            tailored = build_linux_cascade_for_device(
                candidate.default_samplerate,
                str(candidate.kind),
            )
            if tailored is not LINUX_CASCADE:
                per_candidate_cascade = tailored
                logger.info(
                    "voice_cascade_native_rate_prepended",
                    candidate_rank=candidate.preference_rank,
                    device_index=candidate.device_index,
                    native_rate=candidate.default_samplerate,
                )

        per_candidate_result = await run_cascade(
            endpoint_guid=candidate.endpoint_guid,
            device_index=candidate.device_index,
            mode=mode,
            platform_key=platform_key,
            device_friendly_name=candidate.friendly_name,
            device_interface_name=candidate.canonical_name,
            physical_device_id=candidate.canonical_name,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn,
            lifecycle_locks=lifecycle_locks,
            total_budget_s=remaining,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=per_candidate_cascade,
            clock=clock,
            quarantine=quarantine,
            kernel_invalidated_failover_enabled=kernel_invalidated_failover_enabled,
        )
        aggregated_attempts.extend(per_candidate_result.attempts)
        total_attempts_count += per_candidate_result.attempts_count
        last_result = per_candidate_result

        if per_candidate_result.winning_combo is not None:
            logger.info(
                "voice_cascade_candidate_set_resolved",
                winning_rank=candidate.preference_rank,
                winning_source=str(candidate.source),
                winning_kind=str(candidate.kind),
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
                endpoint_guid=candidate.endpoint_guid,
                tried=candidate_idx + 1,
                total=len(candidates),
            )
            return CascadeResult(
                endpoint_guid=candidate.endpoint_guid,
                winning_combo=per_candidate_result.winning_combo,
                winning_probe=per_candidate_result.winning_probe,
                attempts=tuple(aggregated_attempts),
                attempts_count=total_attempts_count,
                budget_exhausted=False,
                source=per_candidate_result.source,
                winning_candidate=candidate,
            )

        # Non-healthy candidate — advance to the next one unless budget
        # is already exhausted (we'll break on the next iteration's
        # ``remaining <= 0`` guard).
        logger.info(
            "voice_cascade_candidate_failed",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            device_index=candidate.device_index,
            source_label=per_candidate_result.source,
            budget_exhausted=per_candidate_result.budget_exhausted,
        )

    # Exhausted — return aggregated result keyed on the first candidate
    # so log correlation is stable.
    logger.error(
        "voice_cascade_candidate_set_exhausted",
        candidate_count=len(candidates),
        attempts_total=total_attempts_count,
    )
    first = candidates[0]
    return CascadeResult(
        endpoint_guid=first.endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=tuple(aggregated_attempts),
        attempts_count=total_attempts_count,
        budget_exhausted=last_result.budget_exhausted if last_result else False,
        source="none",
        winning_candidate=None,
    )


__all__ = ["run_cascade_for_candidates"]
