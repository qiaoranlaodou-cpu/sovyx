"""Tests for ``LinuxAudioServiceMonitor._post_up_health_check`` (F2-H04, §3.K).

W2.C1 foundation — the helper isolates the ``pactl info`` round-trip so
the wire-up step (W2.C2) can gate UP-event emission on it. Tests mock
``asyncio.create_subprocess_exec`` so no real subprocess is spawned, and
each pin one branch of the contract:

* rc=0 within 1.0 s → ``True`` (PipeWire / PulseAudio is responsive).
* rc=1 → ``False`` (daemon up but unhappy; defer UP).
* TimeoutError → ``False`` + subprocess killed (no zombie).
* FileNotFoundError (no ``pactl`` binary) → ``False``.
* OSError on spawn → ``False``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health import _audio_service_linux
from sovyx.voice.health._audio_service_linux import (
    _DEFERRAL_REWARN_EVERY,
    LinuxAudioServiceMonitor,
)
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind


def _query_stub(_service: str) -> str | None:
    return "active"


def _build_monitor() -> LinuxAudioServiceMonitor:
    return LinuxAudioServiceMonitor(
        services_to_monitor=frozenset({"pipewire.service"}),
        poll_interval_s=2.0,
        query=_query_stub,
    )


class TestPostUpHealthCheck:
    """Pin each branch of the helper's contract."""

    @pytest.mark.asyncio()
    async def test_rc_zero_returns_true(self) -> None:
        """``pactl info`` exit 0 → daemon is responsive."""
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as spawn_mock:
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is True
        spawn_mock.assert_awaited_once()
        # ``proc.kill`` MUST NOT have been called on the happy path.
        proc.kill.assert_not_called()

    @pytest.mark.asyncio()
    async def test_rc_nonzero_returns_false(self) -> None:
        """``pactl info`` non-zero exit → daemon up but unhappy; defer UP."""
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=1)
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_timeout_returns_false_and_kills_subprocess(self) -> None:
        """1 s ceiling enforced; timed-out subprocess MUST be killed."""
        proc = MagicMock()
        # proc.wait() awaited only inside the cleanup block (after the
        # kill); patched asyncio.wait_for raises before that point.
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        with (
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch(
                "sovyx.voice.health._audio_service_linux.asyncio.wait_for",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio()
    async def test_missing_pactl_returns_false(self) -> None:
        """``pactl`` not on PATH → ``False`` (no zombie cleanup needed)."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("pactl: not found")),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_oserror_on_spawn_returns_false(self) -> None:
        """Spawn-level OSError → ``False`` (e.g. ENOMEM, EPERM)."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("transient")),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_helper_does_not_raise_on_any_branch(self) -> None:
        """Closure check: helper MUST swallow every error path.

        Per audit §3.K the helper sits on the hot UP-event path; a
        leaked exception would crash the monitor's poll loop and force
        a watchdog restart. Defensive contract: every branch returns
        bool, never raises.
        """
        for side_effect in (FileNotFoundError(), OSError(), PermissionError()):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=side_effect),
            ):
                monitor = _build_monitor()
                # MUST NOT raise.
                result = await monitor._post_up_health_check()
                assert result is False


class TestUpGateDeferral:
    """W0.5 — the deferred-UP path defers the event, tracks a run-length,
    and warns without flooding (anti-pattern #27)."""

    @pytest.mark.asyncio()
    async def test_failing_health_check_defers_event_and_advances_counter(self) -> None:
        """When ``pactl`` stays unresponsive, the UP event is held back and
        the deferral run-length climbs — the watchdog never reacts to a
        systemctl-active-but-pactl-dead daemon."""
        # inactive (baseline DOWN) → active (UP transition) → active …
        states = ["inactive", "active", "active", "active"]

        def _q(_svc: str) -> str | None:
            return states.pop(0) if states else "active"

        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.01,
            query=_q,
        )
        events: list[AudioServiceEvent] = []

        async def _cb(event: AudioServiceEvent) -> None:
            events.append(event)

        with patch.object(monitor, "_post_up_health_check", new=AsyncMock(return_value=False)):
            await monitor.start(_cb)
            await asyncio.sleep(0.1)
            await monitor.stop()

        # UP never emitted (perpetually deferred); run-length advanced.
        assert events == []
        assert monitor._consecutive_up_deferrals >= 1  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_passing_health_check_emits_up_and_resets_counter(self) -> None:
        """Once ``pactl`` answers, the UP event fires and the deferral
        run-length resets to zero."""
        states = ["inactive", "active"]

        def _q(_svc: str) -> str | None:
            return states.pop(0) if states else "active"

        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.01,
            query=_q,
        )
        events: list[AudioServiceEvent] = []

        async def _cb(event: AudioServiceEvent) -> None:
            events.append(event)

        with patch.object(monitor, "_post_up_health_check", new=AsyncMock(return_value=True)):
            await monitor.start(_cb)
            await asyncio.sleep(0.1)
            await monitor.stop()

        assert any(e.kind is AudioServiceEventKind.UP for e in events)
        assert monitor._consecutive_up_deferrals == 0  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_sustained_deferral_warns_throttled_not_per_poll(self) -> None:
        """A wedged daemon must not emit one WARN per poll (anti-pattern
        #27): the canonical ``audio.service.up_gate_deferred`` topic fires
        on the 1st deferral and then only every ``_DEFERRAL_REWARN_EVERY``."""
        states = ["inactive"] + ["active"] * 50

        def _q(_svc: str) -> str | None:
            return states.pop(0) if states else "active"

        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.001,
            query=_q,
        )

        async def _cb(_event: AudioServiceEvent) -> None:
            return

        fake_logger = MagicMock()
        with (
            patch.object(monitor, "_post_up_health_check", new=AsyncMock(return_value=False)),
            patch.object(_audio_service_linux, "logger", fake_logger),
        ):
            await monitor.start(_cb)
            await asyncio.sleep(0.1)
            await monitor.stop()

        topics = [c.args[0] for c in fake_logger.warning.call_args_list if c.args]
        deferred_warns = [t for t in topics if t == "audio.service.up_gate_deferred"]
        counter = monitor._consecutive_up_deferrals  # noqa: SLF001
        # Precondition: at least one UP transition was deferred.
        assert counter >= 1
        # Exact throttle contract: warn on the 1st deferral + every Nth.
        assert len(deferred_warns) == 1 + counter // _DEFERRAL_REWARN_EVERY
