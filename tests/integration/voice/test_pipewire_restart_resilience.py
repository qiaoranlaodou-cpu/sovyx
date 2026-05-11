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
            poll_interval_s=0.01,
            query=_scripted_query(states),
        )

        events: list[AudioServiceEvent] = []

        async def collect(evt: AudioServiceEvent) -> None:
            events.append(evt)

        # Mock health check: always responsive.
        monitor._post_up_health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await monitor.start(collect)
        # Two poll intervals = baseline + transition.
        await asyncio.sleep(0.06)
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
            poll_interval_s=0.01,
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
        # Allow several poll rounds so the retry path engages.
        await asyncio.sleep(0.15)
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
            poll_interval_s=0.01,
            query=_scripted_query(states),
        )

        events: list[AudioServiceEvent] = []

        async def collect(evt: AudioServiceEvent) -> None:
            events.append(evt)

        health = AsyncMock(return_value=False)
        monitor._post_up_health_check = health  # type: ignore[method-assign]

        await monitor.start(collect)
        await asyncio.sleep(0.06)
        await monitor.stop()

        down_events = [e for e in events if e.kind is AudioServiceEventKind.DOWN]
        assert len(down_events) == 1, f"expected 1 DOWN event, got {events!r}"
        # Health check MUST NOT have been called on the DOWN path.
        health.assert_not_awaited()
