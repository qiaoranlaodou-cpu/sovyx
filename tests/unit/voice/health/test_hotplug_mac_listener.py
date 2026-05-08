"""Tests for the macOS hotplug listener — v0.32.0 subprocess wire-up.

Round 3 paranoid audit HIGH-1: until v0.32.0 the macOS hotplug listener
was an unconditional Noop (Sprint 4 / Task #28 unfinished). v0.32.0
wires :func:`build_macos_hotplug_listener` to the polling
``system_profiler`` fallback by default for darwin via
:attr:`VoiceTuningConfig.voice_macos_hotplug_subprocess_enabled`.

Tests pin:

* The default-ON behaviour for darwin returns the subprocess adapter.
* The opt-out via ``voice_macos_hotplug_subprocess_enabled=False``
  returns the legacy ``NoopHotplugListener``.
* The bridge translates the subprocess watchdog's local
  :class:`HotplugEvent` into the contract-level
  :class:`sovyx.voice.health.contract.HotplugEvent` correctly.
* Lifecycle (start / stop / idempotent) on the adapter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice._hotplug_mac_subprocess import (
    AudioDeviceSnapshot,
)
from sovyx.voice._hotplug_mac_subprocess import (
    HotplugEvent as SubprocessHotplugEvent,
)
from sovyx.voice.health._hotplug import NoopHotplugListener
from sovyx.voice.health._hotplug_mac import (
    _SubprocessHotplugListenerAdapter,
    build_macos_hotplug_listener,
)
from sovyx.voice.health.contract import (
    HotplugEvent as ContractHotplugEvent,
)
from sovyx.voice.health.contract import (
    HotplugEventKind,
)


class TestBuildMacosHotplugListenerDefault:
    """Default-ON for darwin per v0.32.0 (Round 3 audit HIGH-1)."""

    def test_default_returns_subprocess_adapter_when_enabled(self) -> None:
        """When the config flag is True (default for darwin), the
        factory returns the subprocess adapter — not the Noop."""
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(
                voice_macos_hotplug_subprocess_enabled=True,
                voice_macos_hotplug_subprocess_interval_s=30.0,
            ),
        ):
            listener = build_macos_hotplug_listener()
        assert isinstance(listener, _SubprocessHotplugListenerAdapter)

    def test_returns_noop_when_disabled(self) -> None:
        """Operator opt-out via env override returns the legacy
        Noop listener (the Sprint 2 behaviour)."""
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(
                voice_macos_hotplug_subprocess_enabled=False,
                voice_macos_hotplug_subprocess_interval_s=30.0,
            ),
        ):
            listener = build_macos_hotplug_listener()
        assert isinstance(listener, NoopHotplugListener)

    def test_factory_passes_interval_to_adapter(self) -> None:
        """The configured interval flows through to the adapter so
        operator overrides take effect at construction time."""
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(
                voice_macos_hotplug_subprocess_enabled=True,
                voice_macos_hotplug_subprocess_interval_s=15.0,
            ),
        ):
            listener = build_macos_hotplug_listener()
        assert isinstance(listener, _SubprocessHotplugListenerAdapter)
        # The watchdog clamps to [5, 300]; 15 is in range so it should
        # round-trip exactly.
        assert listener._watchdog._interval_s == 15.0


class TestSubprocessAdapterEventTranslation:
    """Bridge contract: subprocess HotplugEvent → contract HotplugEvent."""

    @pytest.mark.asyncio
    async def test_added_event_translates_to_device_added(self) -> None:
        """An ``"added"`` subprocess event becomes a
        :attr:`HotplugEventKind.DEVICE_ADDED` contract event with
        the device's CoreAudio UID packed into ``endpoint_guid``."""
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        received: list[ContractHotplugEvent] = []

        async def on_event(event: ContractHotplugEvent) -> None:
            received.append(event)

        adapter._on_event = on_event
        sub_event = SubprocessHotplugEvent(
            kind="added",
            device=AudioDeviceSnapshot(
                unique_id="apple-airpods-uid-42",
                name="AirPods Pro",
                is_input=True,
                is_output=True,
            ),
        )
        await adapter._dispatch(sub_event)
        assert len(received) == 1
        assert received[0].kind is HotplugEventKind.DEVICE_ADDED
        assert received[0].endpoint_guid == "apple-airpods-uid-42"
        assert received[0].device_friendly_name == "AirPods Pro"

    @pytest.mark.asyncio
    async def test_removed_event_translates_to_device_removed(self) -> None:
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        received: list[ContractHotplugEvent] = []

        async def on_event(event: ContractHotplugEvent) -> None:
            received.append(event)

        adapter._on_event = on_event
        sub_event = SubprocessHotplugEvent(
            kind="removed",
            device=AudioDeviceSnapshot(
                unique_id="usb-mic-uid-99",
                name="USB Microphone",
                is_input=True,
                is_output=False,
            ),
        )
        await adapter._dispatch(sub_event)
        assert len(received) == 1
        assert received[0].kind is HotplugEventKind.DEVICE_REMOVED
        assert received[0].endpoint_guid == "usb-mic-uid-99"

    @pytest.mark.asyncio
    async def test_dispatch_no_op_when_no_callback_installed(self) -> None:
        """Defensive — :meth:`_dispatch` may fire after
        :meth:`stop` cleared the callback. Must not raise."""
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        adapter._on_event = None
        sub_event = SubprocessHotplugEvent(
            kind="added",
            device=AudioDeviceSnapshot(
                unique_id="x",
                name="X",
                is_input=True,
                is_output=False,
            ),
        )
        # Must not raise.
        await adapter._dispatch(sub_event)


class TestSubprocessAdapterLifecycle:
    """Adapter honours the :class:`HotplugListener` protocol contract."""

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """A second :meth:`start` is a no-op."""
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        callback: AsyncMock = AsyncMock()
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=("", []),
        ):
            await adapter.start(callback)
            assert adapter._started is True
            # Second start: no-op.
            await adapter.start(callback)
            assert adapter._started is True
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling :meth:`stop` before :meth:`start` is safe; a second
        :meth:`stop` is a no-op."""
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        # stop before start: no-op.
        await adapter.stop()
        callback: AsyncMock = AsyncMock()
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=("", []),
        ):
            await adapter.start(callback)
            await adapter.stop()
            assert adapter._started is False
            # Second stop: no-op.
            await adapter.stop()
            assert adapter._started is False

    @pytest.mark.asyncio
    async def test_start_installs_callback_on_watchdog(self) -> None:
        """The adapter forwards events from the watchdog to the
        operator's ``on_event`` via :meth:`_dispatch`."""
        adapter = _SubprocessHotplugListenerAdapter(interval_s=30.0)
        callback: AsyncMock = AsyncMock()
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=("", []),
        ):
            await adapter.start(callback)
            # Synthetic event through the dispatch path.
            sub_event = SubprocessHotplugEvent(
                kind="added",
                device=AudioDeviceSnapshot(
                    unique_id="u",
                    name="N",
                    is_input=True,
                    is_output=False,
                ),
            )
            await adapter._dispatch(sub_event)
            await adapter.stop()
        callback.assert_awaited_once()
        forwarded = callback.await_args.args[0]
        assert isinstance(forwarded, ContractHotplugEvent)
        assert forwarded.kind is HotplugEventKind.DEVICE_ADDED


class TestSubprocessAdapterIntegration:
    """Default ``EngineConfig()`` constructs an adapter that can start +
    stop cleanly — the integration smoke test."""

    @pytest.mark.asyncio
    async def test_default_config_adapter_starts_and_stops(self) -> None:
        """When the operator runs default config on darwin, the
        polling task is started + cleanly torn down. Patches
        ``system_profiler`` so the test runs on any host."""
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(
                voice_macos_hotplug_subprocess_enabled=True,
                voice_macos_hotplug_subprocess_interval_s=30.0,
            ),
        ):
            listener = build_macos_hotplug_listener()
        assert isinstance(listener, _SubprocessHotplugListenerAdapter)
        callback: AsyncMock = AsyncMock()
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=("", []),
        ):
            await listener.start(callback)
            # Give the watchdog one tick before tearing down — exercises
            # the dispatch loop without depending on real system_profiler.
            await asyncio.sleep(0.05)
            await listener.stop()
        # Watchdog stopped cleanly; no exceptions propagated.
        assert listener._started is False
