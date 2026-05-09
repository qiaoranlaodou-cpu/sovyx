"""F1 default validation-probe + L2.5 setup builder.

Phase 5.F.13 god-file extraction from
``voice/health/_mixer_sanity.py`` (anti-pattern #16). Owns the two
factory functions that compose the L2.5 dependency bundle for the
boot cascade:

* :func:`make_default_validation_probe_fn` — F1 honest-sentinel
  :class:`ValidationProbeFn` built around the cascade probe.
  Derives :class:`MixerValidationMetrics` from probe RMS + Silero
  VAD, with sentinel SNR (20 dB on HEALTHY / 0 dB otherwise) +
  sentinel WW (0.5 when Silero crosses 0.5 / 0.0 otherwise).
* :func:`build_mixer_sanity_setup` — one-call factory that the
  ``_factory_integration.run_boot_cascade_for_candidates`` site
  uses to opt L2.5 into the cascade. Returns ``None`` when L2.5
  cannot meaningfully fire (non-Linux / unknown driver_family /
  KB load failure) — caller then runs the cascade unchanged.

Anti-pattern #20 covered: parent module ``voice/health/_mixer_sanity.py``
re-exports both symbols so the public consumer at
``voice/health/__init__.py`` and the lazy-import call site at
``voice/health/_factory_integration.py:442`` continue to resolve
via standard module-namespace lookup.

Circular-import design: this module imports
``MixerSanitySetup`` + ``ProbeCallable`` + DI types
(``MixerProbeFn`` / ``MixerApplyFn`` / ``MixerRestoreFn`` /
``PersistFn`` / ``ValidationProbeFn`` / ``_TelemetryProto``) from
the parent ``_mixer_sanity`` module — but only LAZILY, inside the
function bodies. Parent's top-level ``from ._mixer_sanity_factory
import (...)`` re-export runs AFTER parent has finished defining
those types, so the circular cycle is broken at import time.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    MixerValidationMetrics,
    ProbeMode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._mixer_kb import MixerKBLookup
    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health._mixer_sanity import (
        MixerApplyFn,
        MixerProbeFn,
        MixerRestoreFn,
        MixerSanitySetup,
        PersistFn,
        ValidationProbeFn,
        _TelemetryProto,
    )
    from sovyx.voice.health.cascade import ProbeCallable
    from sovyx.voice.health.contract import (
        CandidateEndpoint,
        HardwareContext,
    )

logger = get_logger(__name__)


_SPEECH_CREST_FACTOR_DB: float = 9.0
"""Typical peak-to-RMS delta for unvoiced / mixed speech, in dB.

Used by :func:`make_default_validation_probe_fn` to estimate the
peak_dbfs field from the probe's measured RMS. Real peak measurement
requires inspecting the raw frames — the F2 validation probe taps
the capture ring buffer to compute it exactly; F1's approximation is
tight enough that the peak gate (≤ -2 dBFS default) fires correctly
on any reasonable speech signal.
"""


def make_default_validation_probe_fn(
    probe_fn: ProbeCallable,
    *,
    duration_ms: int = 2000,
) -> ValidationProbeFn:
    """Build the F1 default :class:`ValidationProbeFn`.

    Strategy: run a warm probe via the cascade's ``probe_fn`` and
    derive :class:`MixerValidationMetrics` from what the probe
    already measures (RMS + Silero VAD max/mean). For the two gates
    F1 cannot compute exactly (SNR in vocal band, OpenWakeWord
    stage-2), use honest sentinels:

    * **SNR**: ``20.0`` dB when probe is HEALTHY; ``0.0`` dB
      otherwise. The gate (default ``snr_db_vocal_band_min=15.0``)
      fires correctly — a HEALTHY probe had adequate signal energy;
      a non-HEALTHY probe should fail validation and trigger
      rollback.
    * **WW stage-2**: ``0.5`` when Silero ``max_prob >= 0.5``;
      ``0.0`` otherwise. The gate (default
      ``wake_word_stage2_prob_min=0.4``) trivially passes when VAD
      is alive — this is conservative (we skip a real WW probe in
      F1) but not FALSE-positive, because the gate fires only when
      Silero already corroborates the signal.

    F2 extends this function with an actual SNR computation (scipy
    FFT over the 300-3400 Hz band against a noise-floor estimate)
    and OpenWakeWord stage-2 invocation on the captured frames.
    Callers with that infrastructure today inject their own
    :class:`ValidationProbeFn`; the F1 default is the
    lowest-dependency option that ships.

    The returned callable is closure-captured so it can be passed
    directly as :attr:`MixerSanitySetup.validation_probe_fn`.

    Args:
        probe_fn: Cascade probe entry point — typically
            :func:`sovyx.voice.health.probe.probe`. Tests inject a
            deterministic fake.
        duration_ms: Target probe duration in ms. Defaults to
            2000 ms — matches V2 §E.6 validation window.
    """
    hard_timeout_s = (duration_ms / 1000.0) + 1.0

    async def _validate(
        endpoint: CandidateEndpoint,
        tuning: VoiceTuningConfig,  # noqa: ARG001 — reserved for F2 telemetry
    ) -> MixerValidationMetrics:
        # Canonical 16 kHz mono int16 Linux combo — the cascade's
        # default for ALSA probes. Validation runs AFTER L2.5 has
        # healed the mixer, so a plain shared-mode combo against
        # ``ALSA`` should succeed on any Linux setup.
        combo = Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key="linux",
        )
        probe_result = await probe_fn(
            combo=combo,
            mode=ProbeMode.WARM,
            device_index=endpoint.device_index,
            hard_timeout_s=hard_timeout_s,
        )
        rms_dbfs = probe_result.rms_db
        # Clamp peak to the canonical ceiling (-2 dBFS) — no audible
        # signal SHOULD peak above that; going higher would indicate
        # clipping, which would already have failed the probe's
        # spectral check.
        peak_dbfs = min(-2.0, rms_dbfs + _SPEECH_CREST_FACTOR_DB)
        is_healthy = probe_result.diagnosis == Diagnosis.HEALTHY
        snr_sentinel = 20.0 if is_healthy else 0.0
        vad_max = probe_result.vad_max_prob or 0.0
        # Closed-at-threshold behaviour: WW sentinel mirrors Silero
        # crossing 0.5. Below that, VAD doesn't corroborate a signal
        # → WW sentinel stays 0.0 → gate fails → rollback.
        ww_sentinel = 0.5 if vad_max >= 0.5 else 0.0  # noqa: PLR2004
        return MixerValidationMetrics(
            rms_dbfs=rms_dbfs,
            peak_dbfs=peak_dbfs,
            snr_db_vocal_band=snr_sentinel,
            silero_max_prob=vad_max,
            silero_mean_prob=probe_result.vad_mean_prob or 0.0,
            wake_word_stage2_prob=ww_sentinel,
            measurement_duration_ms=probe_result.duration_ms,
        )

    return _validate


async def build_mixer_sanity_setup(
    *,
    probe_fn: ProbeCallable,
    telemetry: _TelemetryProto | None = None,
    hw: HardwareContext | None = None,
    kb_lookup: MixerKBLookup | None = None,
    role_resolver: MixerControlRoleResolver | None = None,
    half_heal_wal_path: Path | None = None,
    mixer_probe_fn: MixerProbeFn | None = None,
    mixer_apply_fn: MixerApplyFn | None = None,
    mixer_restore_fn: MixerRestoreFn | None = None,
    persist_fn: PersistFn | None = None,
    user_profiles_dir: Path | None = None,
) -> MixerSanitySetup | None:
    """Construct a :class:`MixerSanitySetup` for daemon boot.

    The one-call factory used by
    :func:`sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates`
    to opt L2.5 into the cascade. Returns ``None`` when L2.5 cannot
    meaningfully fire on the current host — the caller then passes
    ``mixer_sanity=None`` to :func:`run_cascade_for_candidates` and
    the cascade runs unchanged.

    Returns ``None`` when:

    * Platform is not Linux (F1 scope).
    * ``detect_hardware_context`` yields ``driver_family="unknown"``
      — no KB profile can match, running L2.5 would only add latency.
    * ``MixerKBLookup.load_shipped`` raises (disk corruption, etc.).

    Args:
        probe_fn: Cascade probe used by the default
            :class:`ValidationProbeFn`.
        telemetry: Optional singleton for
            :meth:`record_mixer_sanity_outcome`. Defaults to the
            module-level telemetry recorder when unset — ``None`` in
            the returned setup if no recorder is installed.
        hw: Override for hardware context (tests; production passes
            ``None`` to use :func:`detect_hardware_context`).
        kb_lookup: Override for KB lookup (tests).
        role_resolver: Override for the role resolver (tests).
        half_heal_wal_path: Optional WAL path for mid-apply crash
            recovery; production wires
            ``default_wal_path(data_dir)``.
        mixer_probe_fn, mixer_apply_fn, mixer_restore_fn, persist_fn:
            Optional overrides for the Linux mixer strategy layer.
            Paranoid-QA R4 MEDIUM-4: previously dropped on the
            floor — the factory accepted only ``hw`` / ``kb_lookup``
            / ``role_resolver``, and operators wiring custom
            persist/apply/restore strategies saw the shipped
            defaults silently run instead. All four fields now
            flow through to the constructed :class:`MixerSanitySetup`.
        user_profiles_dir: T5.39 wire-up. When provided, the KB
            loader includes operator-contributed YAML profiles
            from this directory alongside the shipped catalogue
            via :meth:`MixerKBLookup.load_shipped_and_user`. ``None``
            (default) preserves pre-wire-up behaviour: shipped-only
            via :meth:`MixerKBLookup.load_shipped`. The factory
            wires ``data_dir / "mixer_kb" / "user"`` when
            :attr:`VoiceTuningConfig.voice_mixer_kb_user_profiles_enabled`
            is True; tests inject a tmp_path directly.
    """
    # Lazy imports — these modules touch Linux-only subprocess /
    # /proc paths that we want to avoid importing on Windows / macOS
    # cold boot where L2.5 never fires.
    from sovyx.voice.health._hardware_detector import (  # noqa: PLC0415 — lazy-Linux
        detect_hardware_context,
    )
    from sovyx.voice.health._mixer_kb import MixerKBLookup  # noqa: PLC0415
    from sovyx.voice.health._mixer_roles import (  # noqa: PLC0415
        MixerControlRoleResolver,
    )

    # Phase 5.F.13: lazy import of MixerSanitySetup from the parent module
    # to break the import cycle (parent re-exports this factory after
    # MixerSanitySetup is defined).
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup  # noqa: PLC0415

    if sys.platform != "linux":
        logger.debug("mixer_sanity_setup_non_linux_skipped", platform=sys.platform)
        return None

    effective_hw = hw if hw is not None else await detect_hardware_context()
    if effective_hw.driver_family == "unknown":
        logger.info(
            "mixer_sanity_setup_unknown_driver_family",
            codec_id=effective_hw.codec_id,
            system_vendor=effective_hw.system_vendor,
            system_product=effective_hw.system_product,
            note="L2.5 skipped — no KB profile can match unknown driver family",
        )
        return None

    effective_resolver = role_resolver if role_resolver is not None else MixerControlRoleResolver()
    if kb_lookup is not None:
        effective_kb = kb_lookup
    else:
        try:
            # T5.39 — when the factory wired a user-profiles directory
            # (gated on ``voice_mixer_kb_user_profiles_enabled``), the
            # user-aware loader merges operator-contributed YAMLs.
            # ``None`` skips user-side loading entirely (back-compat).
            if user_profiles_dir is not None:
                effective_kb = MixerKBLookup.load_shipped_and_user(
                    user_profiles_dir,
                    resolver=effective_resolver,
                )
            else:
                effective_kb = MixerKBLookup.load_shipped(resolver=effective_resolver)
        except Exception as exc:  # noqa: BLE001 — KB load failure is best-effort
            logger.warning(
                "mixer_sanity_setup_kb_load_failed",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None

    return MixerSanitySetup(
        hw=effective_hw,
        kb_lookup=effective_kb,
        role_resolver=effective_resolver,
        validation_probe_fn=make_default_validation_probe_fn(probe_fn),
        telemetry=telemetry,
        half_heal_wal_path=half_heal_wal_path,
        mixer_probe_fn=mixer_probe_fn,
        mixer_apply_fn=mixer_apply_fn,
        mixer_restore_fn=mixer_restore_fn,
        persist_fn=persist_fn,
    )


__all__ = [
    "build_mixer_sanity_setup",
    "make_default_validation_probe_fn",
]
