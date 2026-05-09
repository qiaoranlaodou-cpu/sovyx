"""Mixer-sanity orchestrator state machine — F1 7-step heal flow.

Phase 5.F.14 god-file extraction from
``voice/health/_mixer_sanity.py`` (anti-pattern #16). Owns the
state-machine TYPES (``_StepName`` / ``_OrchestratorContext`` /
``_StepResult``) and the orchestrator CLASS (``_SanityOrchestrator``)
that drives the 7-step heal flow:

  probe → classify → detect_customization → apply → validate → persist
  → rollback → done

The state machine is opaque to callers — only ``_check_and_maybe_heal_impl``
(still in parent ``_mixer_sanity.py``) constructs the
``_OrchestratorContext`` + invokes ``_SanityOrchestrator(ctx).run()``.

Anti-pattern #20 covered: parent module ``voice/health/_mixer_sanity.py``
re-exports every symbol so the in-parent call site at
``_check_and_maybe_heal_impl`` continues to resolve via standard
module-namespace lookup.

Circular-import design (paranoid-grade):
  This module imports parent-defined DI types
  (``MixerProbeFn`` / ``MixerApplyFn`` / ``MixerRestoreFn`` /
  ``ValidationProbeFn`` / ``PersistFn`` / ``_TelemetryProto``) ONLY
  via TYPE_CHECKING — they appear in dataclass field annotations
  + ``__init__`` signatures, never at runtime resolution.
  Runtime dependencies (logger, contract types, half-heal recovery,
  KB matcher, mixer probe/apply, customization heuristic, pure
  helpers) are imported eagerly because they live in independent
  modules with no back-reference to the parent.
"""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 — fixed-argv subprocess via injected probe/apply callables
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from sovyx.observability.logging import get_logger
from sovyx.voice.health._half_heal_recovery import (
    clear_wal as _clear_half_heal_wal,
)
from sovyx.voice.health._half_heal_recovery import (
    write_wal as _write_half_heal_wal,
)
from sovyx.voice.health._mixer_kb.matcher import _match_factory_signature
from sovyx.voice.health._mixer_sanity_customization import (
    _UserCustomizationReport,
    detect_user_customization,
)
from sovyx.voice.health._mixer_sanity_helpers import (
    _check_validation_gates,
    _classify_regime_heuristically,
    _diagnosis_for_regime,
)
from sovyx.voice.health.contract import (
    Diagnosis,
    MixerControlRole,
    MixerSanityDecision,
    MixerSanityResult,
    MixerValidationMetrics,
    RemediationHint,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._mixer_kb import MixerKBLookup, MixerKBMatch
    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health._mixer_sanity import (
        MixerApplyFn,
        MixerProbeFn,
        MixerRestoreFn,
        PersistFn,
        ValidationProbeFn,
        _TelemetryProto,
    )
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        HardwareContext,
        MixerApplySnapshot,
        MixerCardSnapshot,
        MixerControlSnapshot,
        MixerPresetSpec,
    )

logger = get_logger(__name__)




_StepName: TypeAlias = Literal[
    "probe",
    "classify",
    "detect_customization",
    "apply",
    "validate",
    "persist",
    "rollback",
    "done",
]


@dataclass(slots=True)
class _OrchestratorContext:
    """Mutable state passed between steps. Private to the orchestrator.

    Every field starts ``None``/empty and fills in as the state
    machine advances. The final :meth:`build_result` reads this state
    into an immutable :class:`MixerSanityResult`.
    """

    endpoint: CandidateEndpoint
    hw: HardwareContext
    tuning: VoiceTuningConfig
    start_time_s: float

    # Injected callables
    mixer_probe_fn: MixerProbeFn
    mixer_apply_fn: MixerApplyFn
    mixer_restore_fn: MixerRestoreFn
    kb_lookup: MixerKBLookup
    role_resolver: MixerControlRoleResolver
    validation_probe_fn: ValidationProbeFn
    persist_fn: PersistFn
    telemetry: _TelemetryProto
    # Paranoid-QA R2 CRITICAL #4 — distinguishes "caller passed None"
    # (fall back to module-level singleton at record time) from
    # "caller explicitly injected a _NoopTelemetry or real recorder".
    # Without this flag, an explicit ``telemetry=_NoopTelemetry()`` is
    # indistinguishable from the default, and the late-bind logic
    # silently swaps in the module singleton — overriding the test's
    # explicit choice to disable telemetry.
    telemetry_was_provided: bool = False
    combo_store: ComboStore | None = None
    capture_overrides: CaptureOverrides | None = None
    # Paranoid-QA R2 HIGH #3: when set, orchestrator writes a
    # write-ahead log around _step_apply and attempts recovery at
    # the top of the state machine. See :mod:`_half_heal_recovery`.
    half_heal_wal_path: Path | None = None

    # Filled as the state machine progresses
    mixer_snapshot: tuple[MixerCardSnapshot, ...] = ()
    kb_match: MixerKBMatch | None = None
    customization: _UserCustomizationReport | None = None
    apply_snapshot: MixerApplySnapshot | None = None
    validation_metrics: MixerValidationMetrics | None = None
    validation_passed: bool | None = None
    probe_duration_ms: int = 0
    apply_duration_ms: int | None = None
    diagnosis_before: Diagnosis = Diagnosis.UNKNOWN
    diagnosis_after: Diagnosis | None = None
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"] = "unknown"
    decision: MixerSanityDecision | None = None
    error_token: str | None = None
    remediation: RemediationHint | None = None
    # Persist outcome — False/None means the preset applied but
    # survives only until reboot.
    persist_succeeded: bool | None = None
    # Paranoid-QA R2 HIGH #4: guard against double-rollback. The
    # orchestrator's rollback path can be entered twice on a
    # validation-failure → cancel chain (``_step_rollback`` runs, then
    # the top-level CancelledError handler runs ``rollback_if_needed``
    # again). ``restore_mixer_snapshot`` is semantically idempotent
    # but re-applying the same snapshot wastes amixer round-trips +
    # reopens the race window for a concurrent user tweak to get
    # silently reverted.
    rollback_performed: bool = False
    # Paranoid-QA R3 CRIT-3: distinguish "rollback completed" from
    # "rollback ATTEMPTED but restore_fn raised and we logged +
    # swallowed". Previous code set ``rollback_performed=True`` on
    # both paths, which silently advertised
    # ``decision=ROLLED_BACK, rollback_snapshot=X`` to the dashboard
    # when the mixer was still stuck in the failing-validation
    # applied state. Surfaced via ``error_token`` + consulted by
    # the top-level impl to decide whether to clear the WAL
    # (failed rollback MUST leave the WAL on disk so cross-boot
    # recovery can retry).
    rollback_failed: bool = False

    def controls_modified(self) -> tuple[str, ...]:
        """Names of controls actually mutated, as a flat tuple."""
        if self.apply_snapshot is None:
            return ()
        return tuple(name for name, _ in self.apply_snapshot.applied_controls)

    def cards_probed(self) -> tuple[int, ...]:
        return tuple(card.card_index for card in self.mixer_snapshot)

    def budget_exceeded(self) -> bool:
        # ``>=`` rather than ``>``: the budget is a hard cap — the
        # orchestrator must terminate BY that wall-clock, not AT or
        # beyond. Also makes ``budget_s=0`` a deterministic
        # "fail-fast" for tests (under low-resolution monotonic
        # clocks the strict ``>`` yields 0 > 0 on the very first
        # check, which would never fire).
        elapsed = time.monotonic() - self.start_time_s
        return elapsed >= self.tuning.linux_mixer_sanity_budget_s


@dataclass(frozen=True, slots=True)
class _StepResult:
    """Outcome of one step — names the next step."""

    next_step: _StepName



class _SanityOrchestrator:
    """Drives the 7-step state machine.

    Each step reads ``self._ctx``, makes one decision, and returns a
    :class:`_StepResult` naming the next step. :meth:`run` is the
    dispatch loop.
    """

    def __init__(self, ctx: _OrchestratorContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        """Run the state machine from entry to done."""
        step = "probe"
        while step != "done":
            if self._ctx.budget_exceeded():
                logger.warning(
                    "mixer_sanity_budget_exceeded",
                    endpoint_guid=self._ctx.endpoint.endpoint_guid,
                    step=step,
                )
                # Paranoid-QA CRITICAL #6/#7: if budget trips AFTER
                # apply has committed (apply_snapshot set) but BEFORE
                # validate/persist finished, the terminal record would
                # otherwise carry ``diagnosis_after=HEALTHY`` +
                # ``validation_passed=True`` set by the earlier step
                # while ``decision=ERROR`` — a self-contradictory
                # shape. Normalise: when we're ROLLING BACK an
                # apply-in-flight, surface it as ROLLED_BACK;
                # otherwise ERROR.
                if self._ctx.apply_snapshot is not None:
                    self._ctx.decision = MixerSanityDecision.ROLLED_BACK
                    self._ctx.diagnosis_after = self._ctx.diagnosis_before
                    # Paranoid-QA R2 HIGH #10: preserve validation
                    # truth. The earlier hard-coded ``= False`` lied
                    # when validation had already passed and the
                    # budget tripped during persist — the audit
                    # record then read "apply succeeded, validation
                    # failed, rolled back" when reality was "apply
                    # succeeded, validation PASSED, persist starved
                    # the budget, rolled back anyway". Only stamp
                    # ``False`` when validation hadn't decided yet.
                    if self._ctx.validation_passed is None:
                        self._ctx.validation_passed = False
                else:
                    self._ctx.decision = MixerSanityDecision.ERROR
                self._ctx.error_token = "MIXER_SANITY_BUDGET_EXCEEDED"
                # Paranoid-QA R4 HIGH-4: cap the budget-branch
                # rollback too — without the wrap, a rollback of N
                # controls (each ``linux_mixer_subprocess_timeout_s``
                # ceiling) could run ~N*3s past the budget that
                # supposedly already tripped. Only wrap in wait_for
                # when there's ACTUALLY something to roll back
                # (``apply_snapshot is not None``); otherwise
                # ``rollback_if_needed`` short-circuits synchronously
                # and a 0.0-second wait_for would spuriously fire.
                if self._ctx.apply_snapshot is not None:
                    # Use subprocess_timeout × entry-count as the
                    # cap — lets a legitimate rollback complete even
                    # when budget is tight; only trips on genuine
                    # pathological wall-clock blowout.
                    entries = len(self._ctx.apply_snapshot.reverted_controls) + len(
                        self._ctx.apply_snapshot.reverted_enum_controls
                    )
                    rollback_timeout = max(
                        entries * self._ctx.tuning.linux_mixer_subprocess_timeout_s * 1.25,
                        self._ctx.tuning.linux_mixer_sanity_budget_s,
                    )
                    try:
                        await asyncio.wait_for(
                            self.rollback_if_needed(),
                            timeout=rollback_timeout,
                        )
                    except TimeoutError:
                        logger.warning(
                            "mixer_sanity_rollback_budget_timeout",
                            endpoint_guid=self._ctx.endpoint.endpoint_guid,
                            rollback_timeout_s=rollback_timeout,
                            budget_s=self._ctx.tuning.linux_mixer_sanity_budget_s,
                        )
                        self._ctx.rollback_failed = True
                else:
                    await self.rollback_if_needed()
                return
            match step:
                case "probe":
                    result = await self._step_probe()
                case "classify":
                    result = await self._step_classify()
                case "detect_customization":
                    result = await self._step_detect_customization()
                case "apply":
                    result = await self._step_apply()
                case "validate":
                    result = await self._step_validate()
                case "persist":
                    result = await self._step_persist()
                case "rollback":
                    result = await self._step_rollback()
                case _:  # pragma: no cover — exhaustiveness
                    msg = f"unexpected step {step!r}"
                    raise RuntimeError(msg)
            step = result.next_step

    def build_result(self) -> MixerSanityResult:
        """Freeze current context into the terminal
        :class:`MixerSanityResult` record.

        Paranoid-QA R2 HIGH #9: enforces the shape invariant
        ``apply_snapshot is not None ⇒ apply_duration_ms is not None``.
        The two fields are set atomically inside ``_step_apply`` (no
        await between them), so the invariant is true by construction
        in the happy path. This assertion catches any future edit
        that moves ``apply_duration_ms`` assignment past an await
        point — at which point the builder produces an impossible
        shape (``rollback_snapshot`` populated but ``apply_duration_ms``
        None) that downstream dashboards would silently render as
        "apply took 0 ms".
        """
        c = self._ctx
        decision = c.decision if c.decision is not None else MixerSanityDecision.ERROR
        match = c.kb_match
        if c.apply_snapshot is not None and c.apply_duration_ms is None:
            # Impossible in the happy path — defensive stamp + log
            # so the invariant violation surfaces in observability
            # without poisoning the result.
            logger.error(
                "mixer_sanity_impossible_shape_apply_duration_missing",
                endpoint_guid=c.endpoint.endpoint_guid,
                decision=decision.value,
            )
            c.apply_duration_ms = 0
        # Paranoid-QA R3 CRIT-3 + R4 CRIT-1: surface rollback
        # failure unconditionally. The earlier allow-list
        # (``VALIDATION_FAILED`` | ``BUDGET_EXCEEDED``) silently
        # skipped composition for every other upstream token
        # (APPLY_FAILED, PERSIST_FAILED, future tokens) — leaving
        # ``decision=ERROR`` paired with the upstream token and no
        # indication the rollback itself failed. Observers reading
        # ``error=MIXER_SANITY_PERSIST_FAILED`` couldn't tell from
        # the result whether the mixer had been restored or was
        # stuck in the applied state.
        #
        # New rule: whenever ``rollback_failed`` is set, compose the
        # ``ROLLBACK_FAILED_AFTER_<trigger>`` token. Callers that
        # want the raw upstream token can still recover it by
        # splitting on the ``_AFTER_`` suffix. No allow-list.
        if c.rollback_failed:
            decision = MixerSanityDecision.ERROR
            if c.error_token:
                upstream = c.error_token.removeprefix("MIXER_SANITY_")
                c.error_token = f"MIXER_SANITY_ROLLBACK_FAILED_AFTER_{upstream}"
            else:
                c.error_token = "MIXER_SANITY_ROLLBACK_FAILED"
        return MixerSanityResult(
            decision=decision,
            diagnosis_before=c.diagnosis_before,
            diagnosis_after=c.diagnosis_after,
            regime=c.regime,
            matched_kb_profile=match.profile.profile_id if match is not None else None,
            kb_match_score=match.score if match is not None else 0.0,
            user_customization_score=(
                c.customization.score if c.customization is not None else 0.0
            ),
            cards_probed=c.cards_probed(),
            controls_modified=c.controls_modified(),
            rollback_snapshot=c.apply_snapshot,
            probe_duration_ms=c.probe_duration_ms,
            apply_duration_ms=c.apply_duration_ms,
            validation_passed=c.validation_passed,
            validation_metrics=c.validation_metrics,
            remediation=c.remediation,
            error=c.error_token,
        )

    async def rollback_if_needed(self) -> None:
        """Invoke ``mixer_restore_fn`` if we have an apply snapshot.

        Best-effort for ``Exception`` subclasses only — rollback
        failures are logged and swallowed so the caller's exception
        semantics are preserved.

        Paranoid-QA HIGH #2: ``CancelledError`` (BaseException in
        Python 3.8+) IS re-raised. Earlier implementations used
        ``except BaseException`` which silently swallowed the
        cancellation delivered while restore was running mid-await,
        leaving the caller's shutdown path hanging on a coroutine
        that never propagated the cancel.

        Paranoid-QA R2 HIGH #4: idempotent — second call is a no-op.
        Rollback can be triggered from ``_step_rollback`` AND from a
        top-level exception handler on the same invocation
        (validation-fail → rollback step → caller cancels the
        already-failing run mid-restore). The flag keeps the mixer
        from being re-restored and also prevents the second call
        from competing for the amixer lock with the first.
        """
        if self._ctx.apply_snapshot is None:
            return
        if self._ctx.rollback_performed:
            logger.debug(
                "mixer_sanity_rollback_skipped_already_done",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
            )
            return
        try:
            await self._ctx.mixer_restore_fn(
                self._ctx.apply_snapshot,
                tuning=self._ctx.tuning,
            )
        except asyncio.CancelledError:
            logger.warning(
                "mixer_sanity_rollback_cancelled_mid_restore",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
            )
            # Do NOT set rollback_performed=True — cancellation mid-
            # restore means the mixer is in an unknown state (part of
            # the snapshot may have restored, part may not). A
            # subsequent rollback attempt from the caller's handler
            # should retry. Callers that can't tolerate that (e.g.,
            # daemon shutdown) use ``contextlib.suppress`` at the
            # outer frame.
            raise
        except Exception as exc:  # noqa: BLE001 — Exception-only; BaseException propagates
            logger.warning(
                "mixer_sanity_rollback_failed",
                endpoint_guid=self._ctx.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            # Paranoid-QA R3 CRIT-3: DO NOT set ``rollback_performed=True``
            # when the restore raised. The earlier code set it to
            # suppress the re-entry retry in the caller's handler
            # — but that conflated "rollback complete" with
            # "rollback gave up", causing
            # ``decision=ROLLED_BACK, rollback_snapshot=X`` to be
            # surfaced as success to the dashboard while the mixer
            # was actually stuck in the applied state.
            #
            # The correct signal: ``rollback_failed=True`` so
            # :meth:`build_result` can downgrade the decision to
            # ERROR + set ``error_token=MIXER_SANITY_ROLLBACK_FAILED``,
            # AND the top-level ``_check_and_maybe_heal_impl``
            # preserves the WAL on disk so the NEXT boot's recovery
            # path retries the restore via the same ``restore_fn``.
            # Re-entry from the caller's handler is blocked by the
            # existing ``rollback_performed`` flag (still False here)
            # — wait, that's the opposite of what we want. We need
            # to distinguish "first attempt raised → stop, surface,
            # preserve WAL" from "two handlers both tried → first
            # was fine, skip". Use BOTH flags:
            #   rollback_performed = False  → we never completed
            #   rollback_failed    = True   → we tried and failed
            # The caller's handler sees ``performed=False`` and
            # would normally retry; but we want to prevent retry
            # in the SAME orchestrator run because the failure mode
            # is sticky. Set performed=True only as a retry guard,
            # and use rollback_failed as the observability signal.
            self._ctx.rollback_performed = True
            self._ctx.rollback_failed = True
            return
        self._ctx.rollback_performed = True

    # ── Individual steps ────────────────────────────────────────────

    async def _step_probe(self) -> _StepResult:
        """Read current mixer state. Failure → ERROR (no rollback)."""
        c = self._ctx
        probe_start = time.monotonic()
        try:
            snapshots = await asyncio.to_thread(c.mixer_probe_fn)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "mixer_sanity_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_PROBE_FAILED"
            return _StepResult(next_step="done")
        c.mixer_snapshot = tuple(snapshots)
        c.probe_duration_ms = int((time.monotonic() - probe_start) * 1000)
        if not c.mixer_snapshot:
            # No cards → nothing to heal. Deferring cleanly means the
            # cascade proceeds — user may be on a USB-only / BT-only
            # setup that doesn't expose the HDA-style mixer surface.
            logger.debug(
                "mixer_sanity_no_cards",
                endpoint_guid=c.endpoint.endpoint_guid,
            )
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.regime = "unknown"
            c.error_token = "MIXER_SANITY_NO_CARDS"
            return _StepResult(next_step="done")
        return _StepResult(next_step="classify")

    async def _step_classify(self) -> _StepResult:
        """Match KB + assign regime + decide next step."""
        c = self._ctx
        match = c.kb_lookup.match(
            c.hw,
            c.mixer_snapshot,
            min_score=c.tuning.linux_mixer_sanity_kb_match_threshold,
        )
        c.kb_match = match
        if match is None:
            # No profile matched above threshold OR ambiguous.
            # Distinguish via the lookup's own logging; for the result
            # we keep the aggregated "no actionable KB" bucket.
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
            if c.regime == "healthy":
                c.decision = MixerSanityDecision.SKIPPED_HEALTHY
                c.diagnosis_before = Diagnosis.HEALTHY
                return _StepResult(next_step="done")
            c.decision = MixerSanityDecision.DEFERRED_NO_KB
            c.diagnosis_before = Diagnosis.MIXER_UNKNOWN_PATTERN
            c.error_token = "MIXER_SANITY_NO_KB_MATCH"
            c.remediation = RemediationHint(
                code="remediation.mixer_unknown",
                severity="info",
            )
            return _StepResult(next_step="done")
        # We have a KB profile. Assign regime from the profile and
        # the observed state; diagnosis is MIXER_ZEROED /
        # MIXER_SATURATED based on match.profile.factory_regime.
        regime = match.profile.factory_regime
        # `"either"` is a KB-author hint that this profile targets both
        # regimes; at classification time we still need a concrete
        # label, so fall back to the probe-based heuristic.
        if regime == "attenuation":
            c.regime = "attenuation"
        elif regime == "saturation":
            c.regime = "saturation"
        elif regime == "mixed":
            c.regime = "mixed"
        else:  # "either"
            c.regime = _classify_regime_heuristically(c.mixer_snapshot)
        c.diagnosis_before = _diagnosis_for_regime(c.regime)
        return _StepResult(next_step="detect_customization")

    async def _step_detect_customization(self) -> _StepResult:
        """Run the 7-signal heuristic + branch APPLY / DEFER / SKIP."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        # Compute the factory-signature score separately so the heuristic
        # can score signal A accurately. (The kb_lookup's composite score
        # mixes in other fields.)
        factory_result = _match_factory_signature(
            c.kb_match.profile.factory_signature,
            c.mixer_snapshot,
            c.role_resolver,
            c.hw,
        )
        c.customization = detect_user_customization(
            factory_signature_score=factory_result.score,
            hw=c.hw,
            combo_store=c.combo_store,
            capture_overrides=c.capture_overrides,
            endpoint_guid=c.endpoint.endpoint_guid,
        )
        score = c.customization.score
        # Paranoid-QA HIGH #9: both thresholds use ``>=`` so the
        # boundary lives at the apply-threshold (inclusive). With
        # apply=0.5 and skip=0.75 a score of exactly 0.5 is the first
        # "defer" value, 0.75 is the first "skip" value. VoiceTuningConfig
        # validates apply <= skip so the bands never invert.
        if score >= c.tuning.linux_mixer_user_customization_threshold_skip:
            c.decision = MixerSanityDecision.SKIPPED_CUSTOMIZED
            c.diagnosis_before = Diagnosis.MIXER_CUSTOMIZED
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_skipped_customized",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
                signals=list(c.customization.signals_fired),
            )
            return _StepResult(next_step="done")
        if score >= c.tuning.linux_mixer_user_customization_threshold_apply:
            # Ambiguous zone — defer; dashboard card (F1.I) offers choice.
            c.decision = MixerSanityDecision.DEFERRED_AMBIGUOUS
            c.error_token = "MIXER_SANITY_USER_CUSTOMIZED_AMBIGUOUS"
            c.remediation = RemediationHint(
                code="remediation.mixer_customized",
                severity="info",
            )
            logger.info(
                "mixer_sanity_deferred_customization_ambiguous",
                endpoint_guid=c.endpoint.endpoint_guid,
                customization_score=score,
            )
            return _StepResult(next_step="done")
        return _StepResult(next_step="apply")

    async def _step_apply(self) -> _StepResult:
        """Apply the KB preset to every probed card."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        apply_start = time.monotonic()
        # Apply to the card whose controls best fit the profile.
        # Multi-card systems are rare on laptops; F1 targets the
        # first card — F2 can extend to multi-card selection by
        # scoring each card independently.
        target_card = c.mixer_snapshot[0]
        role_mapping = c.role_resolver.resolve_card(target_card, c.hw)

        # Paranoid-QA R2 HIGH #3 — write the WAL BEFORE the first
        # mutation so a crash mid-apply leaves enough state on disk
        # for the next boot to restore. The preset's target controls
        # are the set that WILL be touched; we serialise their
        # pre-apply raw values as (name, raw) pairs. If anything in
        # this step fails (rollback, exception, cancel), the WAL is
        # cleared before return so the next boot doesn't
        # double-restore. The WAL is intentionally a conservative
        # superset — if apply_mixer_preset skips a control because
        # current_raw already equals target_raw, restoring that
        # control is a no-op, so inclusion is harmless.
        pre_apply_controls = self._build_half_heal_wal_plan(
            target_card, role_mapping, c.kb_match.profile.recommended_preset
        )
        # Paranoid-QA R3 CRIT-1: pre-read the Auto-Mute Mode label
        # when the preset is going to toggle it, so the WAL carries
        # the pre-apply enum state too. Without this, a mid-apply
        # crash between the numeric loop and ``_apply_auto_mute``
        # would be recovered with numerics restored but Auto-Mute
        # stuck in the applied (``Disabled``/``Enabled``) state.
        pre_apply_enum_controls = await self._build_half_heal_wal_enum_plan(
            target_card.card_index,
            role_mapping,
            c.kb_match.profile.recommended_preset,
            timeout_s=c.tuning.linux_mixer_subprocess_timeout_s,
        )
        if c.half_heal_wal_path is not None and (pre_apply_controls or pre_apply_enum_controls):
            wal_written = _write_half_heal_wal(
                card_index=target_card.card_index,
                reverted_controls=pre_apply_controls,
                reverted_enum_controls=pre_apply_enum_controls,
                path=c.half_heal_wal_path,
            )
            if not wal_written:
                # Surface the degradation as a WARNING but proceed —
                # aborting the cascade on a transient disk hiccup
                # would be a worse outcome than running apply
                # without crash recovery for this one pass.
                logger.warning(
                    "mixer_sanity_wal_write_failed_proceeding",
                    endpoint_guid=c.endpoint.endpoint_guid,
                    wal_path=str(c.half_heal_wal_path),
                    note=(
                        "proceeding with apply without WAL protection — "
                        "a mid-apply process death will not self-heal "
                        "on next boot for this single attempt"
                    ),
                )

        try:
            c.apply_snapshot = await c.mixer_apply_fn(
                target_card.card_index,
                c.kb_match.profile.recommended_preset,
                role_mapping,
                tuning=c.tuning,
            )
        except Exception as exc:  # noqa: BLE001 — translate to ERROR decision
            logger.warning(
                "mixer_sanity_apply_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                profile_id=c.kb_match.profile.profile_id,
                detail=str(exc)[:200],
            )
            c.decision = MixerSanityDecision.ERROR
            c.error_token = "MIXER_SANITY_APPLY_FAILED"
            # apply_mixer_preset already rolled back internally; our
            # snapshot stays None. Clear the WAL so the next cascade
            # doesn't attempt recovery on a state that was already
            # reverted in-process.
            if c.half_heal_wal_path is not None:
                _clear_half_heal_wal(c.half_heal_wal_path)
            return _StepResult(next_step="done")
        c.apply_duration_ms = int((time.monotonic() - apply_start) * 1000)
        return _StepResult(next_step="validate")

    @staticmethod
    def _build_half_heal_wal_plan(
        target_card: MixerCardSnapshot,
        role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
        preset: MixerPresetSpec,
    ) -> tuple[tuple[str, int], ...]:
        """Pre-compute the (name, pre_apply_raw) set for the WAL.

        Walks the preset's roles, looks up each role's resolved
        controls on the target card, and emits (control.name,
        control.current_raw) entries. The WAL intentionally over-
        covers relative to the actual amixer_set calls
        (apply_mixer_preset skips controls whose current_raw already
        equals the target); an over-restore during recovery is a
        no-op, whereas an under-restore would miss a control.
        """
        entries: list[tuple[str, int]] = []
        seen: set[str] = set()
        for pc in preset.controls:
            for control in role_mapping.get(pc.role, ()):
                if control.name in seen:
                    continue
                seen.add(control.name)
                entries.append((control.name, control.current_raw))
        # card_index comes from the outer caller — here we only
        # return the control tuples.
        del target_card
        return tuple(entries)

    @staticmethod
    async def _build_half_heal_wal_enum_plan(
        card_index: int,
        role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
        preset: MixerPresetSpec,
        *,
        timeout_s: float,
    ) -> tuple[tuple[str, str], ...]:
        """Pre-compute the (name, pre_apply_enum_label) set for the WAL.

        Paranoid-QA R3 CRIT-1: covers the enum mutation path that
        the numeric WAL plan misses. Reads the current
        ``Auto-Mute Mode`` label when the preset is going to toggle
        it. Uses :func:`_amixer_get_enum` via :func:`asyncio.to_thread`
        so the subprocess doesn't block the event loop (CLAUDE.md
        anti-pattern #14).

        Returns empty tuple when:

        * The preset's ``auto_mute_mode`` is ``"leave"`` (no-op).
        * The resolver doesn't expose a resolved
          ``MixerControlRole.AUTO_MUTE`` AND the default
          ``"Auto-Mute Mode"`` control isn't on the card.
        * The amixer read fails for any reason — we treat absence
          as "nothing to record"; if apply later succeeds, no WAL
          entry is the correct state.
        """
        # Only touch enum controls when the preset actually toggles them.
        if preset.auto_mute_mode == "leave":
            return ()
        # Resolve the control name the same way ``_apply_auto_mute``
        # does: role-mapped control first, canonical fallback second.
        # Import lazily to avoid a top-level cycle with
        # ``_linux_mixer_apply`` (which imports from contract.py too).
        from sovyx.voice.health._linux_mixer_apply import (  # noqa: PLC0415
            _AUTO_MUTE_MODE_CONTROL_NAME,
            _amixer_get_enum,
        )

        auto_mute_snapshots = role_mapping.get(MixerControlRole.AUTO_MUTE, ())
        control_name = (
            auto_mute_snapshots[0].name if auto_mute_snapshots else _AUTO_MUTE_MODE_CONTROL_NAME
        )
        try:
            current_label = await asyncio.to_thread(
                _amixer_get_enum,
                card_index,
                control_name,
                timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — WAL pre-read is best-effort
            logger.debug(
                "mixer_sanity_auto_mute_pre_read_failed",
                card_index=card_index,
                control_name=control_name,
                detail=str(exc)[:200],
            )
            return ()
        if current_label is None:
            # Control absent on this card — apply will also no-op
            # on it, so no WAL entry is correct.
            return ()
        # Paranoid-QA R4 HIGH-3: strip whitespace. Some amixer
        # versions emit the enum label with padding (e.g.,
        # ``"Item0: 'Enabled  '"``) and ``_amixer_get_enum`` only
        # strips outer quotes, not internal whitespace. A WAL that
        # later replays ``amixer set <ctrl> "Enabled  "`` fails
        # because amixer doesn't recognise the padded label; the
        # replay's BypassApplyError is logged-and-swallowed,
        # leaving the mixer stuck. Stripping here keeps the WAL
        # round-trip deterministic.
        stripped = current_label.strip()
        if not stripped:
            # Empty after strip — treat as absent.
            return ()
        return ((control_name, stripped),)

    async def _step_validate(self) -> _StepResult:
        """Run post-apply validation; gates pass → persist, fail → rollback."""
        c = self._ctx
        assert c.kb_match is not None  # noqa: S101 — state-machine invariant
        try:
            metrics = await c.validation_probe_fn(c.endpoint, c.tuning)
        except Exception as exc:  # noqa: BLE001 — validation failure ⇒ rollback
            logger.warning(
                "mixer_sanity_validation_probe_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.validation_passed = False
            c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
            return _StepResult(next_step="rollback")
        c.validation_metrics = metrics
        gates = c.kb_match.profile.validation_gates
        passed = _check_validation_gates(metrics, gates)
        c.validation_passed = passed
        if passed:
            c.diagnosis_after = Diagnosis.HEALTHY
            return _StepResult(next_step="persist")
        c.error_token = "MIXER_SANITY_VALIDATION_FAILED"
        # Paranoid-QA R2 HIGH #6: emit explicit numeric fields rather
        # than letting structlog ``repr()`` the whole dataclass.
        # ``repr(dataclass)`` is an unbounded surface — a future
        # field addition (e.g., a raw buffer sample, a device name,
        # a file path) would be logged verbatim by accident.
        # Explicit fields put the log schema under review.
        logger.info(
            "mixer_sanity_validation_gates_failed",
            endpoint_guid=c.endpoint.endpoint_guid,
            rms_dbfs=metrics.rms_dbfs,
            peak_dbfs=metrics.peak_dbfs,
            snr_db_vocal_band=metrics.snr_db_vocal_band,
            silero_max_prob=metrics.silero_max_prob,
            silero_mean_prob=metrics.silero_mean_prob,
            wake_word_stage2_prob=metrics.wake_word_stage2_prob,
            measurement_duration_ms=metrics.measurement_duration_ms,
        )
        return _StepResult(next_step="rollback")

    async def _step_persist(self) -> _StepResult:
        """alsactl store — best-effort, HEALED either way."""
        c = self._ctx
        try:
            c.persist_succeeded = await c.persist_fn(c.cards_probed(), c.tuning)
        except Exception as exc:  # noqa: BLE001 — Exception-only (Paranoid-QA R3 CRIT-2)
            # Paranoid-QA R3 CRIT-2: previously ``except BaseException``
            # which contradicted :meth:`rollback_if_needed`'s narrower
            # form (post-R2 HIGH #1) and swallowed ``CancelledError``,
            # ``KeyboardInterrupt``, ``SystemExit``. A cancel delivered
            # during ``systemctl start --no-block`` (several-second
            # subprocess on a loaded system) would be silently
            # swallowed, ``persist_succeeded=False`` set, and the
            # state machine would march on to ``HEALED`` — leaving
            # the caller's shutdown sequence hanging on a
            # cancellation that never propagated. Exception-only
            # semantics restore the R2-level rigour.
            logger.warning(
                "mixer_sanity_persist_failed",
                endpoint_guid=c.endpoint.endpoint_guid,
                detail=str(exc)[:200],
            )
            c.persist_succeeded = False
        if c.persist_succeeded is False:
            c.error_token = "MIXER_SANITY_PERSIST_FAILED"
        c.decision = MixerSanityDecision.HEALED
        c.remediation = RemediationHint(
            code=(
                "remediation.mixer_zeroed"
                if c.regime == "attenuation"
                else "remediation.mixer_saturated"
            ),
            severity="info",
        )
        return _StepResult(next_step="done")

    async def _step_rollback(self) -> _StepResult:
        """Explicit rollback after validation failure."""
        c = self._ctx
        await self.rollback_if_needed()
        c.decision = MixerSanityDecision.ROLLED_BACK
        c.diagnosis_after = c.diagnosis_before
        return _StepResult(next_step="done")



__all__ = [
    "_OrchestratorContext",
    "_SanityOrchestrator",
    "_StepName",
    "_StepResult",
]
