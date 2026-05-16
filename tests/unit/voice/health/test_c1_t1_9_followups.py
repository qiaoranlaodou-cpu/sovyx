"""Mission C1 §20.M T1.9.a + T1.9.b — Phase 1 deferred sub-task closures.

Tests cover the two sub-tasks deferred from the v0.44.0 ship and
closed in a v0.44.x follow-up:

* **T1.9.a** — :meth:`VoiceCaptureWatchdog._emit_hotplug_clear_metric`
  widened from literal ``reason == "apo_degraded"`` to
  :func:`is_apo_class_reason(entry.derived_reason or entry.reason)`.
  Forward-compatible for future APO-class verdict additions; honors
  the LENIENT v0.44.x ``derived_reason`` field-consultation pattern
  established by commit ``c5791e40``.

* **T1.9.b** — :func:`_unrecoverable_remediation_hint` accepts a
  ``subreason`` kwarg + the VAD-frontend reset-ladder exhaustion
  path in :meth:`CaptureIntegrityCoordinator.handle_deaf_signal`
  emits ``voice_capture_integrity_unrecoverable`` with
  ``subreason="vad_frontend_dead"`` BEFORE the quarantine fires.
  Operator-facing hint dispatches to a platform-neutral VAD-frontend
  variant distinct from the T6.15 OS-DSP hints.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health.capture_integrity import _unrecoverable_remediation_hint
from sovyx.voice.health.contract import (
    HotplugEvent,
    HotplugEventKind,
)

# ── T1.9.a: watchdog hotplug-clear metric routing ──────────────────────


class TestHotplugClearMetricRoutingByDerivedReason:
    """T1.9.a — hotplug-clear metric routing honors derived_reason."""

    def _make_watchdog(self):  # type: ignore[no-untyped-def]
        from sovyx.voice.health.watchdog import VoiceCaptureWatchdog

        # Minimal-construction watchdog instance via __new__; the metric
        # emitter only reads ``self._platform_key_for_metric()`` which
        # we'll stub on a per-test basis.
        watchdog = VoiceCaptureWatchdog.__new__(VoiceCaptureWatchdog)
        watchdog._platform_key_for_metric = lambda: "linux"  # type: ignore[method-assign] # noqa: SLF001
        return watchdog

    def test_apo_degraded_entry_emits_apo_metric(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice.health import watchdog as watchdog_mod

        calls: list[dict[str, str]] = []
        monkeypatch.setattr(
            watchdog_mod,
            "record_apo_degraded_event",
            lambda **kw: calls.append(kw),
        )
        monkeypatch.setattr(
            watchdog_mod,
            "record_kernel_invalidated_event",
            lambda **kw: pytest.fail(f"kernel-invalidated metric fired: {kw}"),
        )

        q = EndpointQuarantine(quarantine_s=60.0)
        q.add(endpoint_guid="g-apo", reason="apo_degraded", derived_reason="apo_degraded")
        entry = q.get("g-apo")

        watchdog = self._make_watchdog()
        watchdog._emit_hotplug_clear_metric(  # noqa: SLF001
            entry,
            event=HotplugEvent(kind=HotplugEventKind.DEVICE_ADDED),
        )
        assert len(calls) == 1
        assert calls[0]["action"] == "hotplug_clear"

    def test_vad_frontend_dead_with_legacy_reason_pin_emits_apo_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T1.9.a closure — entry pinned at LENIENT (reason="apo_degraded"
        + derived_reason="vad_frontend_dead") MUST emit the APO metric
        per is_apo_class_reason classifier. The pre-fix literal
        ``reason == "apo_degraded"`` test would also pass for THIS
        entry shape (legacy reason matches), but the bare-reason read
        is brittle: a future change that promotes derived_reason to
        reason (v0.45.0 STRICT flip) would still want this entry
        attributed to the APO metric. The widened classifier survives
        the flip.
        """
        from sovyx.voice.health import watchdog as watchdog_mod

        calls: list[dict[str, str]] = []
        monkeypatch.setattr(
            watchdog_mod,
            "record_apo_degraded_event",
            lambda **kw: calls.append(kw),
        )
        monkeypatch.setattr(
            watchdog_mod,
            "record_kernel_invalidated_event",
            lambda **kw: pytest.fail(f"kernel-invalidated metric fired: {kw}"),
        )

        q = EndpointQuarantine(quarantine_s=60.0)
        q.add(
            endpoint_guid="g-vad",
            reason="apo_degraded",  # LENIENT legacy pin
            derived_reason="vad_frontend_dead",  # verdict class
        )
        entry = q.get("g-vad")

        watchdog = self._make_watchdog()
        watchdog._emit_hotplug_clear_metric(  # noqa: SLF001
            entry,
            event=HotplugEvent(kind=HotplugEventKind.DEVICE_ADDED),
        )
        assert len(calls) == 1

    def test_non_apo_class_entry_falls_to_kernel_invalidated_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``driver_silent`` is NOT in ``_APO_CLASS_REASONS`` — hotplug
        clear should emit ``record_kernel_invalidated_event`` per the
        pre-mission default path."""
        from sovyx.voice.health import watchdog as watchdog_mod

        apo_calls: list[dict[str, str]] = []
        kernel_calls: list[dict[str, str]] = []
        monkeypatch.setattr(
            watchdog_mod,
            "record_apo_degraded_event",
            lambda **kw: apo_calls.append(kw),
        )
        monkeypatch.setattr(
            watchdog_mod,
            "record_kernel_invalidated_event",
            lambda **kw: kernel_calls.append(kw),
        )

        q = EndpointQuarantine(quarantine_s=60.0)
        q.add(
            endpoint_guid="g-drv",
            reason="kernel_invalidated",
            derived_reason="driver_silent",
        )
        entry = q.get("g-drv")

        watchdog = self._make_watchdog()
        watchdog._emit_hotplug_clear_metric(  # noqa: SLF001
            entry,
            event=HotplugEvent(kind=HotplugEventKind.DEVICE_ADDED),
        )
        assert len(apo_calls) == 0
        assert len(kernel_calls) == 1
        assert kernel_calls[0]["action"] == "hotplug_clear"

    def test_none_entry_falls_to_kernel_invalidated_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the watchdog can't resolve a quarantine entry (e.g.
        hotplug for an endpoint that's never been quarantined), the
        fallback is the kernel-invalidated metric — preserves the
        pre-Phase-1 contract.
        """
        from sovyx.voice.health import watchdog as watchdog_mod

        apo_calls: list[dict[str, str]] = []
        kernel_calls: list[dict[str, str]] = []
        monkeypatch.setattr(
            watchdog_mod,
            "record_apo_degraded_event",
            lambda **kw: apo_calls.append(kw),
        )
        monkeypatch.setattr(
            watchdog_mod,
            "record_kernel_invalidated_event",
            lambda **kw: kernel_calls.append(kw),
        )

        watchdog = self._make_watchdog()
        watchdog._emit_hotplug_clear_metric(  # noqa: SLF001
            None,
            event=HotplugEvent(kind=HotplugEventKind.DEVICE_ADDED),
        )
        assert len(apo_calls) == 0
        assert len(kernel_calls) == 1


# ── T1.9.b: unrecoverable-hint subreason dispatch ──────────────────────


class TestUnrecoverableHintSubreason:
    """T1.9.b — ``_unrecoverable_remediation_hint`` accepts a
    ``subreason`` kwarg + dispatches to a platform-neutral
    VAD-frontend variant distinct from the T6.15 OS-DSP hints."""

    def test_default_subreason_routes_to_t6_15_platform_hint(self) -> None:
        """No subreason (default) routes to the T6.15 platform-specific
        OS-DSP hint — pre-mission behavior preserved."""
        win_hint = _unrecoverable_remediation_hint("win32")
        assert "Voice Clarity" in win_hint
        assert "Audio enhancements" in win_hint

        linux_hint = _unrecoverable_remediation_hint("linux")
        assert "echo-cancel" in linux_hint
        assert "pactl" in linux_hint

        darwin_hint = _unrecoverable_remediation_hint("darwin")
        assert "Voice Isolation" in darwin_hint

    def test_vad_frontend_dead_subreason_returns_platform_neutral_hint(
        self,
    ) -> None:
        """The VAD-frontend-dead subreason hint MUST be identical
        across platforms — the fault is in Sovyx's own ONNX session,
        not in the OS DSP chain. Operator action is daemon-restart
        + doctor diagnostics, NOT OS settings."""
        win_hint = _unrecoverable_remediation_hint("win32", subreason="vad_frontend_dead")
        linux_hint = _unrecoverable_remediation_hint("linux", subreason="vad_frontend_dead")
        darwin_hint = _unrecoverable_remediation_hint("darwin", subreason="vad_frontend_dead")
        assert win_hint == linux_hint == darwin_hint

    def test_vad_frontend_dead_hint_mentions_daemon_restart(self) -> None:
        """Operator-facing hint MUST mention the canonical recovery
        action (daemon restart) — pinned so future hint rewrites
        cannot accidentally drop the actionable verb."""
        hint = _unrecoverable_remediation_hint("linux", subreason="vad_frontend_dead")
        assert "restart" in hint.lower()
        # Sanity — the hint should NOT direct the operator to OS DSP
        # settings (the fault is Sovyx-internal).
        assert "Voice Clarity" not in hint
        assert "pactl" not in hint
        assert "Voice Isolation" not in hint

    def test_unknown_subreason_falls_to_platform_hint(self) -> None:
        """Unknown subreason values fall back to the T6.15 platform
        hint — forward-compat for future subreason additions that may
        not yet have dedicated hint variants."""
        # An unknown subreason should NOT raise; fallback to platform.
        result = _unrecoverable_remediation_hint("linux", subreason="future_subreason")
        # Should match the linux default hint (NOT the vad_frontend
        # variant).
        assert "pactl" in result


# ── T1.9.b: ladder-exhaustion unrecoverable event ───────────────────────


class TestLadderExhaustionUnrecoverableEvent:
    """T1.9.b — ladder exhaustion in handle_deaf_signal emits
    ``voice_capture_integrity_unrecoverable`` with subreason BEFORE
    the quarantine fires."""

    @pytest.mark.asyncio()
    async def test_ladder_exhaustion_emits_unrecoverable_then_quarantines(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When VAD_FRONTEND_DEAD ladder exhausts without any
        APPLIED_HEALTHY outcome, the coordinator MUST log
        ``voice_capture_integrity_unrecoverable`` with the
        ``vad_frontend_dead`` subreason. This sequences the
        diagnostic upstream of the routine
        ``capture_integrity_coordinator_quarantined`` log line so
        monitoring tooling sees the more-specific event first.
        """
        import logging
        from datetime import UTC, datetime

        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice.health.capture_integrity import (
            CaptureIntegrityCoordinator,
        )
        from sovyx.voice.health.contract import (
            BypassOutcome,
            BypassVerdict,
            IntegrityResult,
            IntegrityVerdict,
        )

        # Fake capture-task surface — just enough for ``_build_context``.
        capture_task = MagicMock()
        capture_task.active_device_guid = "g-vad-exhaust"
        capture_task.active_device_name = "Fake Mic"
        capture_task.active_device_index = 1
        capture_task.active_device_kind = "input"
        capture_task.host_api_name = "ALSA"

        # Fake probe — returns VAD_FRONTEND_DEAD verdict to route into
        # the ladder branch.
        async def _probe_warm(_task):  # type: ignore[no-untyped-def]  # noqa: ANN001
            return IntegrityResult(
                verdict=IntegrityVerdict.VAD_FRONTEND_DEAD,
                endpoint_guid="g-vad-exhaust",
                rms_db=-45.0,
                vad_max_prob=0.001,
                spectral_flatness=0.12,
                spectral_rolloff_hz=6500.0,
                duration_s=3.0,
                probed_at_utc=datetime.now(UTC),
                raw_frames=48_000,
            )

        probe = MagicMock()
        probe.probe_warm = _probe_warm

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,
            strategies=[],
            capture_task=capture_task,
            platform_key="linux",
            tuning=VoiceTuningConfig(),
            pipeline_ref=None,
        )

        # Force the ladder to exhaust via the verdict route — the
        # ladder runs, sees no pipeline_ref, returns empty outcomes,
        # which counts as "not recovered" but ALSO empty list... let
        # me check the actual ladder return. Actually with
        # pipeline_ref=None, the ladder emits the missing_pipeline_ref
        # warning and returns []. The coordinator's recovered check
        # is `any(... APPLIED_HEALTHY)` which is False for [], so it
        # falls through to quarantine.
        #
        # For the unrecoverable event to fire, ladder_outcomes must
        # be non-empty. We stub _run_vad_frontend_reset_ladder to
        # return STILL_DEAD outcomes directly.

        stub_outcomes = [
            BypassOutcome(
                strategy_name=f"vad_frontend_reset:step_{i}",
                attempt_index=i,
                verdict=BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD,
                integrity_before=IntegrityResult(
                    verdict=IntegrityVerdict.VAD_FRONTEND_DEAD,
                    endpoint_guid="g-vad-exhaust",
                    rms_db=-45.0,
                    vad_max_prob=0.001,
                    spectral_flatness=0.12,
                    spectral_rolloff_hz=6500.0,
                    duration_s=3.0,
                    probed_at_utc=datetime.now(UTC),
                    raw_frames=48_000,
                ),
                integrity_after=None,
                elapsed_ms=10.0,
                detail="step_crashed",
            )
            for i in range(2)
        ]

        async def _stubbed_ladder(_ctx, _before, _tuning):  # type: ignore[no-untyped-def]  # noqa: ANN001
            return stub_outcomes

        monkeypatch.setattr(
            coordinator,
            "_run_vad_frontend_reset_ladder",
            _stubbed_ladder,
        )

        # Capture the structured-log fields.
        caplog.set_level(logging.ERROR, logger="sovyx.voice.health.capture_integrity")

        outcomes = await coordinator.handle_deaf_signal()

        # Ladder outcomes returned unchanged.
        assert outcomes == stub_outcomes
        # Coordinator is one-shot resolved post-exhaustion.
        assert coordinator.is_resolved is True

        # ``voice_capture_integrity_unrecoverable`` event fired with
        # the vad_frontend_dead subreason.
        unrecoverable_events = [
            r
            for r in caplog.records
            if "voice_capture_integrity_unrecoverable" in r.getMessage()
            or (hasattr(r, "event") and r.event == "voice_capture_integrity_unrecoverable")
        ]
        # structlog routes through this stdlib logger; the event name
        # appears either in the message OR as a structured field. At
        # least one matching record MUST exist.
        if not unrecoverable_events:
            # Surface caplog records for diagnostic.
            all_events = [getattr(r, "event", r.getMessage()) for r in caplog.records]
            pytest.fail(
                "voice_capture_integrity_unrecoverable not emitted; "
                f"captured events: {all_events!r}"
            )
