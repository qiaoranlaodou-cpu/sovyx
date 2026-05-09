"""Cascade execution loop — pinned/store/cascade walk.

Split from the legacy ``cascade.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02.

Owns the core cascade entry points (:func:`run_cascade`,
:func:`run_cascade_for_candidates`), the per-attempt probe wrapper
(:func:`_try_combo`), the structured-log helpers
(:func:`_log_probe_call`, :func:`_log_probe_result`, :func:`_combo_tag`,
:func:`_truncate_detail`, :data:`_LOG_DETAIL_MAX_CHARS`), and the
:class:`ProbeCallable` Protocol that types the cascade's probe
dependency.

Composes:

* :mod:`._planner` — platform cascade tables + per-device tailoring.
* :mod:`._alignment` — pinned override / ComboStore fast-path lookups
  + L2.5 mixer-sanity helper.
* :mod:`._budget` — tuning constants + lifecycle locks +
  quarantine/record-winner helpers.

All public names re-exported from :mod:`sovyx.voice.health.cascade`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_cascade_attempt,
    record_combo_store_hit,
)
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    get_default_quarantine,
)
from sovyx.voice.health._user_remediation import (
    homogeneous_diagnosis_remediation,
)
from sovyx.voice.health.cascade._alignment import (
    _lookup_override,
    _lookup_store,
    _run_mixer_sanity,
)
from sovyx.voice.health.cascade._budget import (
    _DEFAULT_ATTEMPT_BUDGET_S,
    _DEFAULT_TOTAL_BUDGET_S,
    _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT,
    _default_locks,
    _quarantine_endpoint,
    _record_winner,
)
from sovyx.voice.health.cascade._planner import (
    _platform_cascade,
)
from sovyx.voice.health.contract import (
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.health.probe import (
    probe as _default_probe,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.engine._lock_dict import LRULockDict
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore


logger = get_logger(__name__)


__all__ = [
    "ProbeCallable",
    "run_cascade",
    "run_cascade_for_candidates",
]


# Phase 5.F.16 god-file split: probe-invocation surface (ProbeCallable
# Protocol + _call_probe + _try_combo + _PHYSICAL_CURE_DIAGNOSES, ~135 LOC)
# extracted to _executor_probe.py. Re-exported here so the public consumer
# at cascade/__init__.py + the in-parent call sites continue to resolve
# via standard module-namespace lookup. Anti-pattern #16 + #20.
from sovyx.voice.health.cascade._executor_probe import (  # noqa: E402  F401
    _PHYSICAL_CURE_DIAGNOSES,
    ProbeCallable,
    _call_probe,
    _try_combo,
)

# ── Entry point ─────────────────────────────────────────────────────────


async def run_cascade(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str = "",
    device_interface_name: str = "",
    device_class: str = "",
    endpoint_fxproperties_sha: str = "",
    detected_apos: Sequence[str] = (),
    physical_device_id: str = "",
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    cascade_override: Sequence[Combo] | None = None,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the L2 cascade for ``endpoint_guid`` and return the outcome.

    Ordered attempts (any HEALTHY short-circuits):

    1. :class:`CaptureOverrides` pinned combo, if any (source ``"pinned"``).
    2. :class:`ComboStore` fast path, if any (source ``"store"``).
    3. Platform cascade (source ``"cascade"``).

    The whole call holds a per-endpoint :class:`asyncio.Lock` from
    ``lifecycle_locks`` (created automatically if not supplied). A
    module-level fallback dict is used when the caller doesn't pass one
    so standalone ``run_cascade`` calls from tests remain race-safe.

    Args:
        endpoint_guid: Stable GUID of the capture endpoint (Windows
            MMDevice id, Linux ALSA card+device, macOS CoreAudio UID).
        device_index: PortAudio device index to pass to the probe.
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard or on first user interaction.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``. Picks
            the cascade table and is echoed back to the probe for
            combo construction.
        device_friendly_name, device_interface_name, device_class,
        endpoint_fxproperties_sha, detected_apos: Forwarded to
            :meth:`ComboStore.record_winning` on a successful run so
            the store entry contains the full fingerprint for the 13
            invalidation rules.
        physical_device_id: Canonical physical-device identity
            (:attr:`~sovyx.voice.device_enum.DeviceEntry.canonical_name`)
            of the microphone behind ``endpoint_guid``. Propagated into
            the §4.4.7 quarantine entry so
            :meth:`~sovyx.voice.health._quarantine.EndpointQuarantine.is_quarantined_physical`
            can reject every host-API alias of the same wedged driver
            during fail-over selection. Empty disables physical-scope
            guarding (legacy callers).
        combo_store: Persistent fast-path store. ``None`` disables
            both fast-path lookup and the post-cascade record-winning
            side-effect.
        capture_overrides: User-pinned combos. ``None`` disables
            pinned lookup.
        probe_fn: Probe entry point. Defaults to
            :func:`sovyx.voice.health.probe.probe`; tests inject a fake
            that doesn't touch PortAudio or ONNX.
        lifecycle_locks: Pre-existing per-endpoint lock dict. Created
            at ``maxsize=64`` if omitted.
        total_budget_s: Cascade wall-clock budget. On exhaustion the
            best attempt so far is returned with ``budget_exhausted=True``.
        attempt_budget_s: Per-probe hard timeout. Matches the probe's
            ``hard_timeout_s`` so a hung driver can't stall the cascade.
        voice_clarity_autofix: When ``False`` (user disabled the APO
            bypass), skip attempts 0..4 and start at shared-mode.
        cascade_override: Override the platform cascade for this call.
            Mainly for ``--aggressive`` mode where the caller wants to
            try every combo rather than short-circuit on first HEALTHY.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: §4.4.7 kernel-invalidated quarantine store. When
            ``None`` the process-wide default (via
            :func:`~sovyx.voice.health._quarantine.get_default_quarantine`)
            is used if the kill-switch is on, otherwise quarantine is
            skipped. Tests pass a fresh :class:`EndpointQuarantine` to
            avoid cross-test state bleed.
        kernel_invalidated_failover_enabled: Master toggle for the
            quarantine behaviour. ``None`` resolves to
            :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled`
            at call time. When ``False``, KERNEL_INVALIDATED results
            fall through to the next cascade combo as normal — preserves
            the pre-§4.4.7 behaviour for operators who want to opt out.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, the cascade runs
            :func:`~sovyx.voice.health._mixer_sanity.check_and_maybe_heal`
            between the ComboStore fast-path and the platform cascade
            walk. On ``HEALED`` the mixer is corrected and the
            subsequent platform walk validates a working combo; on any
            other decision the cascade proceeds unchanged. Default
            ``None`` preserves pre-L2.5 behaviour for every existing
            caller.
    """
    # `or` treats an empty `LRULockDict` as falsy (``__len__ == 0``) and
    # silently drops the caller's shared lock — use an identity check.
    locks = lifecycle_locks if lifecycle_locks is not None else _default_locks()
    lock = locks[endpoint_guid]

    resolved_failover = (
        _VoiceTuning().kernel_invalidated_failover_enabled
        if kernel_invalidated_failover_enabled is None
        else kernel_invalidated_failover_enabled
    )
    resolved_quarantine: EndpointQuarantine | None
    if quarantine is not None:
        resolved_quarantine = quarantine
    elif resolved_failover:
        resolved_quarantine = get_default_quarantine()
    else:
        resolved_quarantine = None

    async with lock:
        return await _run_cascade_locked(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            mode=mode,
            mixer_sanity=mixer_sanity,
            tuning=tuning,
            platform_key=platform_key,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
            physical_device_id=physical_device_id,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn or _default_probe,
            total_budget_s=total_budget_s,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=cascade_override,
            clock=clock,
            quarantine=resolved_quarantine,
        )


async def _run_cascade_locked(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str,
    device_interface_name: str,
    device_class: str,
    endpoint_fxproperties_sha: str,
    detected_apos: Sequence[str],
    physical_device_id: str,
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    probe_fn: ProbeCallable,
    total_budget_s: float,
    attempt_budget_s: float,
    voice_clarity_autofix: bool,
    cascade_override: Sequence[Combo] | None,
    clock: Callable[[], float],
    quarantine: EndpointQuarantine | None,
    mixer_sanity: MixerSanitySetup | None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    deadline = clock() + total_budget_s
    attempts: list[ProbeResult] = []
    attempts_count = 0

    # §4.4.7 / §4.4.8 short-circuit: a previously quarantined endpoint
    # is known to be in a state that no *boot-time* cascade can cure —
    # either kernel-invalidated (reason ``"probe_*"`` /
    # ``"watchdog_recheck"`` / ``"factory_integration"``) or APO-degraded
    # (reason ``"apo_degraded"``). Skip every attempt — the factory
    # integration layer will fail-over to the next viable
    # :class:`DeviceEntry` and the watchdog recheck loop retries after
    # the quarantine TTL. The log surfaces the live entry's ``reason``
    # token so operators can distinguish the two root causes without
    # reading two separate events.
    if quarantine is not None and quarantine.is_quarantined(endpoint_guid):
        entry = quarantine.get(endpoint_guid)
        logger.warning(
            "voice_cascade_skipped_quarantined",
            endpoint=endpoint_guid,
            friendly_name=device_friendly_name,
            reason=entry.reason if entry is not None else "unknown",
        )
        return _make_result(
            endpoint_guid=endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=attempts,
            attempts_count=attempts_count,
            budget_exhausted=False,
            source="quarantined",
        )

    # 1. Pinned override.
    pinned = _lookup_override(capture_overrides, endpoint_guid, platform_key)
    if pinned is not None:
        logger.info(
            "voice_cascade_pinned_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(pinned),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=pinned,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            result=result,
        )
        attempts.append(result)
        attempts_count += 1
        record_cascade_attempt(
            platform=platform_key,
            host_api=pinned.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="pinned",
        )
        if result.diagnosis is Diagnosis.HEALTHY:
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="pinned",
                attempts=1,
                combo_host_api=pinned.host_api,
                combo_sample_rate=pinned.sample_rate,
                combo_channels=pinned.channels,
                combo_exclusive=pinned.exclusive,
                combo_auto_convert=pinned.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=pinned,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="pinned",
            )
        # §4.4.7 + T6.9 — physical-cure diagnoses. Every host API will
        # fail equally; trying the ComboStore or the cascade loop just
        # wastes the user's time. KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT
        # share the same semantic (driver wedged at IAudioClient /
        # callback layer, no user-mode cure available) and route to the
        # same quarantine + short-circuit path.
        if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=pinned.host_api,
            platform_key=platform_key,
            reason="probe_pinned",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=pinned.host_api,
                source="pinned",
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        logger.warning(
            "voice_cascade_pinned_failed",
            endpoint=endpoint_guid,
            host_api=pinned.host_api,
            combo=_combo_tag(pinned),
            diagnosis=str(result.diagnosis),
        )

    # 2. ComboStore fast path.
    store_combo = _lookup_store(combo_store, endpoint_guid)
    if store_combo is None:
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="miss",
        )
    if store_combo is not None:
        if clock() >= deadline:
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        logger.info(
            "voice_cascade_store_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(store_combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=store_combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            result=result,
        )
        attempts.append(result)
        success = result.diagnosis is Diagnosis.HEALTHY
        record_cascade_attempt(
            platform=platform_key,
            host_api=store_combo.host_api,
            success=success,
            source="store",
        )
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="hit" if success else "needs_revalidation",
        )
        if success:
            # Fast-path hit: do NOT re-record (combo already in store).
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="store",
                attempts=1,
                combo_host_api=store_combo.host_api,
                combo_sample_rate=store_combo.sample_rate,
                combo_channels=store_combo.channels,
                combo_exclusive=store_combo.exclusive,
                combo_auto_convert=store_combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=store_combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="store",
            )
        # §4.4.7 + T6.9 — physical-cure state observed on the fast path.
        # Invalidate the (now misleading) store entry too, then quarantine
        # the endpoint and short-circuit the rest of the cascade.
        # KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT both route here.
        if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=store_combo.host_api,
            platform_key=platform_key,
            reason="probe_store",
            physical_device_id=physical_device_id,
        ):
            if combo_store is not None:
                combo_store.invalidate(endpoint_guid, reason=result.diagnosis.value)
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=store_combo.host_api,
                source="store",
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        # Invalidate the stale store entry so the next boot runs the
        # full cascade fresh rather than re-probing the known-bad combo.
        # The metric is emitted inside ``ComboStore.invalidate`` — single
        # source of truth for every invalidation path.
        if combo_store is not None:
            combo_store.invalidate(endpoint_guid, reason="fast_path_probe_failed")
            logger.warning(
                "voice_cascade_store_invalidated",
                endpoint=endpoint_guid,
                host_api=store_combo.host_api,
                combo=_combo_tag(store_combo),
                diagnosis=str(result.diagnosis),
            )

    # 2.5. L2.5 mixer sanity — runs only when the caller opts in via
    # ``mixer_sanity`` AND we are on Linux. Fire-and-forget from the
    # cascade's perspective: on HEALED the ALSA mixer is corrected and
    # the subsequent platform walk succeeds against the healed state;
    # on any other decision the cascade proceeds unchanged. L2.5 does
    # NOT pick a PortAudio combo (that's the platform cascade's
    # responsibility) — it only repairs the mixer state so the
    # platform walk has a chance. See ADR-voice-mixer-sanity-l2.5-
    # bidirectional + V2 Master Plan Part C.1.
    #
    # The ``try/except BaseException`` here is defence-in-depth:
    # ``_run_mixer_sanity`` already catches ``check_and_maybe_heal``
    # errors internally, but a failure in its setup code (e.g.,
    # ``CandidateEndpoint`` construction with malformed inputs) or a
    # misbehaving DI callable injected by the user would otherwise
    # abort the cascade — defeating the whole point of keeping L2.5
    # an opt-in, side-channel layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=endpoint_guid,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancellation must propagate —
            # the cascade loop may want to short-circuit.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue on non-cancel error
            logger.warning(
                "voice_cascade_mixer_sanity_helper_raised",
                endpoint=endpoint_guid,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # 3. Platform cascade.
    cascade = (
        tuple(cascade_override)
        if cascade_override is not None
        else _platform_cascade(platform_key)
    )
    start_idx = 0 if voice_clarity_autofix else _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT
    if platform_key != "win32":
        # voice_clarity_autofix is Windows-only; on Linux/macOS start at 0.
        start_idx = 0

    # T6.9 — set when EXCLUSIVE_MODE_NOT_AVAILABLE is observed on a
    # combo with ``exclusive=True``. Subsequent iterations skip every
    # remaining combo with ``exclusive=True`` because the endpoint
    # fundamentally doesn't permit exclusive mode — retrying other
    # exclusive combos for the same endpoint just burns the
    # per-attempt budget. Shared-mode combos (``exclusive=False``)
    # are still tried because they take a different driver code path.
    skip_remaining_exclusive = False

    for idx, combo in enumerate(cascade):
        if idx < start_idx:
            continue
        # T6.9 skip-remaining-exclusive optimisation.
        if skip_remaining_exclusive and combo.exclusive:
            logger.info(
                "voice_cascade_combo_skipped_exclusive_mode_not_available",
                endpoint=endpoint_guid,
                attempt=idx,
                combo=_combo_tag(combo),
            )
            continue
        if clock() >= deadline:
            logger.warning(
                "voice_cascade_budget_exhausted",
                endpoint=endpoint_guid,
                attempts_run=attempts_count,
                total_budget_s=total_budget_s,
                # T6.11 — diagnosis histogram for at-a-glance triage.
                # Empty when the deadline trips before any attempt
                # completes (first-iteration timeout). See
                # :func:`_compute_diagnosis_histogram` for shape.
                diagnosis_histogram=_compute_diagnosis_histogram(attempts),
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        attempts_count += 1
        logger.info(
            "voice_cascade_attempt",
            endpoint=endpoint_guid,
            attempt=idx,
            combo=_combo_tag(combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            result=result,
        )
        attempts.append(result)
        record_cascade_attempt(
            platform=platform_key,
            host_api=combo.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="cascade",
        )
        # §4.4.7 + T6.9 — physical-cure state. Every remaining host API
        # in the cascade table will fail identically because the failure
        # is at IAudioClient::Initialize / kernel callback layer, upstream
        # of the host-API layer. Quarantine + break the loop instead of
        # burning the per-attempt budget on combos we already know will
        # fail. KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT (T6.2) share
        # this semantic.
        if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=combo.host_api,
            platform_key=platform_key,
            reason="probe_cascade",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=combo.host_api,
                source="cascade",
                attempt=idx,
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        # T6.9 — once an exclusive-mode combo returns
        # EXCLUSIVE_MODE_NOT_AVAILABLE, the endpoint definitively
        # doesn't permit exclusive mode. Mark the rest of the loop
        # to skip exclusive combos (saves wall-clock budget for
        # shared-mode candidates that have a real chance). Other
        # diagnoses are routine fall-through to the next combo.
        if result.diagnosis is Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE and combo.exclusive:
            skip_remaining_exclusive = True
        if result.diagnosis is Diagnosis.HEALTHY:
            _record_winner(
                combo_store=combo_store,
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                device_interface_name=device_interface_name,
                device_class=device_class,
                endpoint_fxproperties_sha=endpoint_fxproperties_sha,
                detected_apos=detected_apos,
                combo=combo,
                probe=result,
                cascade_attempts_before_success=attempts_count,
            )
            # T1 — DoD #3 requires this event to be present in the log
            # after a successful cascade run. Future T3 will extend it
            # with ``winning_candidate`` / ``candidate_source`` fields
            # once the candidate-set refactor lands.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="cascade",
                attempts=attempts_count,
                combo_host_api=combo.host_api,
                combo_sample_rate=combo.sample_rate,
                combo_channels=combo.channels,
                combo_exclusive=combo.exclusive,
                combo_auto_convert=combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="cascade",
            )

    histogram = _compute_diagnosis_histogram(attempts)
    logger.error(
        "voice_cascade_exhausted",
        endpoint=endpoint_guid,
        attempts=attempts_count,
        # T6.11 — diagnosis histogram. Cascade-table-exhausted is the
        # critical case (every combo failed); the histogram surfaces
        # WHICH failure modes dominated so operators can route alerts:
        # ``device_busy`` heavy → another app holds the mic;
        # ``apo_degraded`` heavy → Voice Clarity / similar APO chain;
        # ``permission_denied`` → OS gate; etc.
        diagnosis_histogram=histogram,
    )
    # T6.12 — homogeneous-failure user-actionable signal. When EVERY
    # cascade attempt died with the same diagnosis AND that diagnosis
    # has a known user-facing remediation, emit the dedicated
    # ``voice_cascade_user_actionable`` event so the dashboard banner
    # can route on it WITHOUT scraping the histogram. Heterogeneous
    # exhaustions OR homogeneous exhaustions on diagnoses without a
    # remediation entry (HEALTHY, MIXER_*, UNKNOWN) skip the event.
    homogeneous = homogeneous_diagnosis_remediation(histogram)
    if homogeneous is not None:
        diagnosis_value, remediation = homogeneous
        logger.error(
            "voice_cascade_user_actionable",
            endpoint=endpoint_guid,
            attempts=attempts_count,
            diagnosis=diagnosis_value,
            remediation=remediation,
        )
    return _make_result(
        endpoint_guid=endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=attempts,
        attempts_count=attempts_count,
        budget_exhausted=False,
        source="none",
    )


# Phase 5.F.17 god-file split: run_cascade_for_candidates (~272 LOC)
# extracted to _executor_candidates.py. Re-exported here so the public
# consumer at cascade/__init__.py + the wire-up call site at
# voice/health/_factory_integration.py continue to resolve via standard
# module-namespace lookup. Anti-pattern #16 + #20.
from sovyx.voice.health.cascade._executor_candidates import (  # noqa: E402  F401
    run_cascade_for_candidates,
)

# Phase 5.F.7 god-file split: 6 internal helpers + _LOG_DETAIL_MAX_CHARS
# constant extracted to :mod:cascade._executor_helpers. Re-exported
# below so every internal call site (run_cascade + run_cascade_for_candidates +
# _try_combo) resolves the names via standard module-namespace lookup.
# Anti-pattern #16 + #20.
from sovyx.voice.health.cascade._executor_helpers import (  # noqa: E402  F401
    _LOG_DETAIL_MAX_CHARS,
    _combo_tag,
    _compute_diagnosis_histogram,
    _log_probe_call,
    _log_probe_result,
    _make_result,
    _truncate_detail,
)
