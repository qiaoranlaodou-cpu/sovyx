"""Voice Windows Paranoid Mission bypass-tier metrics.

Phase 5.F.6 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the v0.24.0 → v0.26.0 Voice Windows Paranoid
Mission counter vocabulary — 7 record helpers + 7 metric-name
constants spanning:

* **Cold-probe silence rejection** (Furo W-1):
  :func:`record_cold_silence_rejected` — strict-vs-lenient mode
  calibration knob.
* **Tier 1 RAW + Communications bypass** (Windows only):
  :func:`record_tier1_raw_attempted` + :func:`record_tier1_raw_outcome`.
* **Tier 2 host-API rotate-then-exclusive bypass** (Windows only):
  :func:`record_tier2_host_api_rotate_attempted` +
  :func:`record_tier2_host_api_rotate_outcome`.
* **Cascade ↔ runtime opener alignment SLI** (Furo W-4):
  :func:`record_opener_host_api_alignment`.
* **IMMNotificationClient registration health**:
  :func:`record_hotplug_listener_registered`.

Tier 1/2 recorders also mirror to ``_bypass_tier_state`` (the
in-memory counter store the dashboard reads at
``GET /api/voice/bypass-tier-status``) so the OTel histogram + the
dashboard counter pair stay in sync without a second emit path.

Anti-pattern #20 covered: parent module ``voice/health/_metrics.py``
re-exports every symbol so production callers (the bypass strategy
classes + the cascade opener) and test references at the original
``sovyx.voice.health._metrics.<name>`` path continue to resolve.
"""

from __future__ import annotations

from sovyx.observability.metrics import get_metrics
from sovyx.voice.health import _bypass_tier_state

# ── Stable name constants (Voice Windows Paranoid Mission) ───────────
# v0.24.0 → v0.26.0 counter vocabulary. Names are stable wire contracts
# — downstream dashboards / Grafana / Prometheus alerts depend on them.

METRIC_PROBE_COLD_SILENCE_REJECTED = "sovyx.voice.health.probe.cold_silence_rejected"
METRIC_BYPASS_TIER1_RAW_ATTEMPTED = "sovyx.voice.health.bypass.tier1_raw.attempted"
METRIC_BYPASS_TIER1_RAW_OUTCOME = "sovyx.voice.health.bypass.tier1_raw.outcome"
METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED = (
    "sovyx.voice.health.bypass.tier2_host_api_rotate.attempted"
)
METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME = (
    "sovyx.voice.health.bypass.tier2_host_api_rotate.outcome"
)
METRIC_OPENER_HOST_API_ALIGNMENT = "sovyx.voice.opener.host_api_alignment"
METRIC_HOTPLUG_LISTENER_REGISTERED = "sovyx.voice.hotplug.listener.registered"


# ── Record helpers ──────────────────────────────────────────────────


def record_cold_silence_rejected(*, mode: str, host_api: str) -> None:
    """Record a cold-probe silence-rejection event (Furo W-1 telemetry).

    Args:
        mode: ``"strict_reject"`` (post-fix path returning NO_SIGNAL) or
            ``"lenient_passthrough"`` (legacy v0.23.x acceptance kept for
            calibration during the foundation phase).
        host_api: combo's host_api (``"Windows WASAPI"``, ``"MME"``, etc).

    The lenient counter is the operator's calibration knob — when its rate
    matches the predicted silent-combo population on Voice Clarity rigs
    (mission §F1 promotion gate), flipping
    ``probe_cold_strict_validation_enabled`` to True is safe.
    """
    counter = getattr(get_metrics(), "voice_health_probe_cold_silence_rejected", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "mode": mode,
            "host_api": host_api or "unknown",
        },
    )


def record_tier1_raw_attempted(*, host_api: str, raw_supported: bool) -> None:
    """Record a Tier 1 RAW + Communications bypass attempt (Windows only).

    Fired by ``WindowsRawCommunicationsBypass.apply`` before the
    ``IAudioClient3::SetClientProperties`` call. Pairs with
    :func:`record_tier1_raw_outcome` for an attempts-vs-success ratio.
    """
    _bypass_tier_state.mark_tier1_raw_attempted()
    counter = getattr(get_metrics(), "voice_health_bypass_tier1_raw_attempted", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "host_api": host_api or "unknown",
            "raw_supported": "true" if raw_supported else "false",
        },
    )


def record_tier1_raw_outcome(*, verdict: str, host_api: str) -> None:
    """Record a Tier 1 RAW bypass outcome (Windows only).

    Args:
        verdict: ``RawCommunicationsRestartVerdict`` value
            (``raw_engaged``, ``property_rejected_by_driver``,
            ``open_failed_no_stream``, etc.).
        host_api: post-apply host_api (informational — the strategy
            doesn't mutate it; included for slice-by-host_api dashboards).
    """
    _bypass_tier_state.mark_tier1_raw_outcome(verdict)
    counter = getattr(get_metrics(), "voice_health_bypass_tier1_raw_outcome", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "verdict": verdict,
            "host_api": host_api or "unknown",
        },
    )


def record_tier2_host_api_rotate_attempted(*, source_host_api: str, target_host_api: str) -> None:
    """Record a Tier 2 host-API rotate-then-exclusive attempt (Windows only).

    Fired by ``WindowsHostApiRotateThenExclusiveBypass.apply`` Phase A
    (rotate) before ``request_host_api_rotate``.
    """
    _bypass_tier_state.mark_tier2_host_api_rotate_attempted()
    counter = getattr(get_metrics(), "voice_health_bypass_tier2_host_api_rotate_attempted", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "source_host_api": source_host_api or "unknown",
            "target_host_api": target_host_api or "unknown",
        },
    )


def record_tier2_host_api_rotate_outcome(
    *,
    phase_a_verdict: str,
    phase_b_verdict: str,
    resulting_host_api: str = "",
) -> None:
    """Record a Tier 2 rotate+exclusive outcome (Windows only).

    Args:
        phase_a_verdict: ``HostApiRotateVerdict`` value
            (``rotated_success``, ``no_target_sibling``, etc.).
        phase_b_verdict: ``ExclusiveRestartVerdict`` value when
            Phase A succeeded; ``"skipped"`` when Phase A failed.
        resulting_host_api: the host_api the stream actually ended on
            after both phases — may differ from the target if the
            opener pyramid drifted.
    """
    _bypass_tier_state.mark_tier2_host_api_rotate_outcome(
        phase_a_verdict=phase_a_verdict,
        phase_b_verdict=phase_b_verdict,
    )
    counter = getattr(get_metrics(), "voice_health_bypass_tier2_host_api_rotate_outcome", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "phase_a_verdict": phase_a_verdict,
            "phase_b_verdict": phase_b_verdict,
            "resulting_host_api": resulting_host_api or "unknown",
        },
    )


def record_opener_host_api_alignment(
    *,
    aligned: bool,
    cascade_winner_host_api: str = "",
    runtime_chain_head_host_api: str = "",
) -> None:
    """Record a cascade-↔-runtime opener alignment SLI sample (Furo W-4).

    Fired on every ``_device_chain`` invocation. ``aligned=True`` when
    ``runtime_chain_head_host_api == cascade_winner_host_api``;
    ``aligned=False`` is the bug signature (the opener drifted off
    the cascade winner).

    Target SLI: 100 % aligned. Any drift is a bug, not a tunable.
    """
    counter = getattr(get_metrics(), "voice_opener_host_api_alignment", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "aligned": "true" if aligned else "false",
            "cascade_winner_host_api": cascade_winner_host_api or "unknown",
            "runtime_chain_head_host_api": runtime_chain_head_host_api or "unknown",
        },
    )


def record_hotplug_listener_registered(*, registered: bool, error: str = "") -> None:
    """Record an IMMNotificationClient registration health sample.

    Fired once at AudioCaptureTask.start when
    ``mm_notification_listener_enabled=True``. Promotion gate:
    registration success ≥99 % on Win10/11 is the threshold for
    flipping the listener flag default to True in v0.26.0.
    """
    counter = getattr(get_metrics(), "voice_hotplug_listener_registered", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "registered": "true" if registered else "false",
            "error": error or "none",
        },
    )


__all__ = [
    "METRIC_BYPASS_TIER1_RAW_ATTEMPTED",
    "METRIC_BYPASS_TIER1_RAW_OUTCOME",
    "METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED",
    "METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME",
    "METRIC_HOTPLUG_LISTENER_REGISTERED",
    "METRIC_OPENER_HOST_API_ALIGNMENT",
    "METRIC_PROBE_COLD_SILENCE_REJECTED",
    "record_cold_silence_rejected",
    "record_hotplug_listener_registered",
    "record_opener_host_api_alignment",
    "record_tier1_raw_attempted",
    "record_tier1_raw_outcome",
    "record_tier2_host_api_rotate_attempted",
    "record_tier2_host_api_rotate_outcome",
]
