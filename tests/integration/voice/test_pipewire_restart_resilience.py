"""Integration tests for the PipeWire restart UP gate (F2-H04, §3.K).

W2.C2 wire-up — :class:`LinuxAudioServiceMonitor._run` now gates UP
emission on ``_post_up_health_check`` returning ``True``. These tests
drive the full ``_run`` loop with a stubbed query callable and a
patched health check so the contract is pinned end-to-end without
spawning real subprocesses or systemd units.

Three branches covered:

1. **Happy UP**: services come back active AND pactl is responsive
   → UP event emitted exactly once.
2. **Deferred UP then recover**: services come back active but pactl
   is unresponsive on round N → no UP; on round N+1 pactl recovers
   → UP emitted exactly once.
3. **DOWN never gated**: DOWN transitions are NOT subject to the
   pactl gate (pactl can't be queried if the daemon is dead).

Timing budget: ``poll_interval_s=0.05`` and ``await asyncio.sleep(0.25)``
(or 0.4 for the retry test) — per CLAUDE.md anti-pattern #22 Windows
``time.monotonic()`` ticks at ~15.6 ms, so the previous 0.01 / 0.06 s
budget was below the coarse-clock floor and surfaced as flake on
loaded Windows hosts (test passed in isolation, failed mid-suite at
~9 min mark of the full pytest run). 50 ms poll × 3 rounds = 150 ms
budget; 250 ms sleep leaves ~100 ms margin for event-loop scheduling
under load. Linux CI is unaffected (sub-µs sleep) — the bump only
raises the floor on platforms that need it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sovyx.voice.health._audio_service_linux import LinuxAudioServiceMonitor
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind


def _scripted_query(states: Iterator[str | None]) -> Any:
    """Return a query callable that yields successive states per call.

    Each ``query(service)`` call pulls one state from the iterator.
    Useful for driving the monitor through a deterministic state
    transition sequence under a single asyncio loop.
    """

    def query(_service: str) -> str | None:
        return next(states)

    return query


class TestUpGate:
    """Gate behaviour around the UP transition."""

    @pytest.mark.asyncio()
    async def test_up_emitted_when_pactl_responsive(self) -> None:
        """active+responsive sequence → exactly one UP event."""
        # First poll seeds the baseline ("inactive"). Second poll
        # transitions to "active" and pactl returns True.
        states = iter(["inactive", "active", "active", "active"])
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.05,
            query=_scripted_query(states),
        )

        events: list[AudioServiceEvent] = []

        async def collect(evt: AudioServiceEvent) -> None:
            events.append(evt)

        # Mock health check: always responsive.
        monitor._post_up_health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await monitor.start(collect)
        # Two poll intervals = baseline + transition (anti-pattern #22).
        await asyncio.sleep(0.25)
        await monitor.stop()

        up_events = [e for e in events if e.kind is AudioServiceEventKind.UP]
        assert len(up_events) == 1, f"expected 1 UP event, got {events!r}"

    @pytest.mark.asyncio()
    async def test_up_deferred_when_pactl_unresponsive_then_recovers(self) -> None:
        """active+unresponsive → no UP; later active+responsive → UP fires."""
        # 1st poll: inactive (seed). 2nd: active (UP gate). 3rd: active (retry).
        states = iter(["inactive", "active", "active", "active", "active", "active"])
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.05,
            query=_scripted_query(states),
        )

        events: list[AudioServiceEvent] = []

        async def collect(evt: AudioServiceEvent) -> None:
            events.append(evt)

        # Health check: returns False on first call (gate defers UP),
        # True on the second call (retry succeeds).
        health = AsyncMock(side_effect=[False, True, True, True])
        monitor._post_up_health_check = health  # type: ignore[method-assign]

        await monitor.start(collect)
        # Allow several poll rounds so the retry path engages
        # (anti-pattern #22: ≥ 4 × poll_interval_s for Windows margin).
        await asyncio.sleep(0.4)
        await monitor.stop()

        up_events = [e for e in events if e.kind is AudioServiceEventKind.UP]
        # Exactly one UP after the gate recovered — NOT two (no
        # duplicate after retry success).
        assert len(up_events) == 1, f"expected 1 UP after retry, got {events!r}"
        # Gate must have been consulted at least once before the emit.
        assert health.await_count >= 2

    @pytest.mark.asyncio()
    async def test_down_never_consults_health_check(self) -> None:
        """DOWN transitions bypass the pactl gate (daemon is dead → no pactl)."""
        # 1st poll: active (seed). 2nd: inactive (DOWN, NOT gated).
        states = iter(["active", "inactive", "inactive", "inactive"])
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=0.05,
            query=_scripted_query(states),
        )

        events: list[AudioServiceEvent] = []

        async def collect(evt: AudioServiceEvent) -> None:
            events.append(evt)

        health = AsyncMock(return_value=False)
        monitor._post_up_health_check = health  # type: ignore[method-assign]

        await monitor.start(collect)
        # Anti-pattern #22: ≥ 4 × poll_interval_s for Windows margin.
        await asyncio.sleep(0.25)
        await monitor.stop()

        down_events = [e for e in events if e.kind is AudioServiceEventKind.DOWN]
        assert len(down_events) == 1, f"expected 1 DOWN event, got {events!r}"
        # Health check MUST NOT have been called on the DOWN path.
        health.assert_not_awaited()


class TestStrictModePromotionPending:
    """Placeholders for the STRICT-mode flip deferred to a later cycle.

    Audit §3.K flip step + ``feedback_staged_adoption`` — the
    LENIENT-to-STRICT promotion (INFO → WARNING + SLO alert when
    ``voice_audio_service_up_health_check_failed`` fires > 3x in any
    60 s window) is gated on operator telemetry from v0.37.x in the
    real env (Sony VAIO + Mint + PipeWire + Razer USB). The test below
    is INTENTIONALLY SKIPPED so the desired contract is visible in
    source control; the future commit that promotes the flip unskips
    + adjusts assertions.
    """

    @pytest.mark.skip(
        reason=(
            "STRICT flip pending v0.37.x telemetria — see "
            "TODO at _audio_service_linux.py::_run UP gate. "
            "Unskip when promoting INFO -> WARNING + adding SLO "
            "alert per audit §3.K flip step."
        ),
    )
    @pytest.mark.asyncio()
    async def test_strict_mode_warns_on_repeated_gate_defer(self) -> None:
        """Repeated gate defers in a 60 s window MUST log WARNING + alert."""
        # When the flip lands, this test mocks _post_up_health_check to
        # return False on 4+ consecutive calls within a synthetic 60 s
        # window (using a frozen clock), then asserts:
        #   * logger emitted "voice_audio_service_up_health_check_failed"
        #     at WARNING level (not INFO).
        #   * a parallel "audio.service.up_gate_deferred" SLO alert was
        #     fired exactly once for the burst.
        # The current LENIENT implementation only logs INFO and never
        # alerts — that's the deliberate behaviour for v0.37.x.
        raise AssertionError("placeholder for STRICT-mode promotion")
