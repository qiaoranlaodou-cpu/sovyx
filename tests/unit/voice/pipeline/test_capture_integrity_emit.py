"""Unit tests for the dual-emission wrapper (Mission H2 §T1.6).

Verifies :func:`emit_capture_integrity_event` dual-emits both neutral
and legacy events, carries the correct metadata, and respects the
``capture_integrity_dual_emit_enabled`` kill-switch.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice._event_names import (
    CaptureIntegrityEvent,
)
from sovyx.voice._platform_metadata import current_platform_token
from sovyx.voice.pipeline._capture_integrity_emit import (
    SCHEMA_VERSION,
    emit_capture_integrity_event,
)

_WRAPPER_LOGGER = "sovyx.voice.pipeline._capture_integrity_emit"


@pytest.fixture(autouse=True)
def _clear_platform_cache() -> None:
    """Cached platform token must not bleed across tests that monkeypatch sys.platform."""
    current_platform_token.cache_clear()
    yield
    current_platform_token.cache_clear()


def _captured_events_by_name(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """Filter caplog records by the structlog ``event`` field.

    Mirrors :func:`tests.unit.voice.health.test_capture_integrity._events_of`
    — structlog renders the log payload as a dict captured in ``record.msg``;
    the canonical event-name string lives under the ``event`` key.
    """
    return [
        r.msg
        for r in caplog.records
        if r.name == _WRAPPER_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == name
    ]


class TestDualEmissionBasics:
    """Both neutral and legacy events fire with matching severity."""

    def test_bypassed_dual_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypassed")
        legacy = _captured_events_by_name(caplog, "audio.apo.bypassed")
        assert len(neutral) == 1
        assert len(legacy) == 1
        assert neutral[0].get("level") == "error"
        assert legacy[0].get("level") == "error"

    def test_bypass_activated_dual_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_ACTIVATED,
            "warning",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypass_activated")
        legacy = _captured_events_by_name(caplog, "voice_apo_bypass_activated")
        assert len(neutral) == 1
        assert len(legacy) == 1
        assert neutral[0].get("level") == "warning"

    def test_bypass_failed_dual_emits_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_FAILED,
            "error",
            mind_id="jonny",
            error="boom",
            error_type="RuntimeError",
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypass_failed")
        legacy = _captured_events_by_name(caplog, "voice_apo_bypass_failed")
        assert len(neutral) == 1
        assert len(legacy) == 1

    def test_bypass_ineffective_dual_emits_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_INEFFECTIVE,
            "error",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset", "linux.pipewire_direct"],
            voice_clarity_active=False,
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypass_ineffective")
        legacy = _captured_events_by_name(caplog, "voice_apo_bypass_ineffective")
        assert len(neutral) == 1
        assert len(legacy) == 1


class TestNeutralMetadataFields:
    """Neutral emissions carry the three v2.0.0 schema metadata fields."""

    def test_neutral_carries_platform(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        monkeypatch.setattr("sys.platform", "linux")
        current_platform_token.cache_clear()
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypassed")[0]
        assert neutral.get("voice.platform") == "linux"

    def test_neutral_carries_bypass_family(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset", "linux.alsa_capture_switch"],
            voice_clarity_active=False,
            verdict="failure",
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypassed")[0]
        assert neutral.get("voice.bypass_family") == "alsa_capture_chain"

    def test_neutral_carries_schema_version(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=[],
            voice_clarity_active=False,
            verdict="failure",
        )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypassed")[0]
        assert neutral.get("voice.event_schema_version") == SCHEMA_VERSION

    def test_legacy_does_not_carry_new_metadata(self, caplog: pytest.LogCaptureFixture) -> None:
        """Legacy emission stays exactly as pre-mission shape."""
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        legacy = _captured_events_by_name(caplog, "audio.apo.bypassed")[0]
        # Legacy carries the pre-mission bare keys, NOT the dotted-namespace
        # neutral metadata.
        assert "voice.platform" not in legacy
        assert "voice.bypass_family" not in legacy
        assert "voice.event_schema_version" not in legacy


class TestMixedPlatformWarning:
    """A cross-platform strategy list fires the structured WARN."""

    def test_mixed_platform_emits_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=["win.voice_clarity_disable", "linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        warn_events = _captured_events_by_name(
            caplog, "voice.capture_integrity.mixed_platform_strategies"
        )
        assert len(warn_events) == 1
        assert warn_events[0].get("level") == "warning"

    def test_homogeneous_linux_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=[
                "linux.alsa_mixer_reset",
                "linux.alsa_capture_switch",
                "linux.pipewire_direct",
            ],
            voice_clarity_active=False,
            verdict="failure",
        )
        warn_events = _captured_events_by_name(
            caplog, "voice.capture_integrity.mixed_platform_strategies"
        )
        assert warn_events == []


class TestKillSwitch:
    """capture_integrity_dual_emit_enabled = False suppresses legacy emission."""

    def test_kill_switch_off_suppresses_legacy(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        with patch(
            "sovyx.voice.pipeline._capture_integrity_emit._is_dual_emit_enabled",
            return_value=False,
        ):
            emit_capture_integrity_event(
                CaptureIntegrityEvent.BYPASSED,
                "error",
                mind_id="jonny",
                strategies=["linux.alsa_mixer_reset"],
                voice_clarity_active=False,
                verdict="failure",
            )
        neutral = _captured_events_by_name(caplog, "voice.capture_integrity.bypassed")
        legacy = _captured_events_by_name(caplog, "audio.apo.bypassed")
        assert len(neutral) == 1
        assert len(legacy) == 0
