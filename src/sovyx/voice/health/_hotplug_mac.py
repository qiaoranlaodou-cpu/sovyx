"""macOS hot-plug listener — subprocess-fallback wrapper (v0.32.0+).

ADR §4.4.2 commits to
``AudioObjectAddPropertyListener(kAudioHardwarePropertyDevices)`` on
macOS, but Sprint 2 shipped a Noop because the CoreAudio bindings
(``pyobjc-framework-CoreAudio``) are a non-trivial extra and the
cross-platform cascade / probe peers were Sprint 4 territory too.

Round 3 paranoid audit (HIGH-1) flagged the unconditional Noop as a
production gap: AirPods disconnects / Bluetooth route changes / USB
mic unplugs were silently dropped on every macOS host. v0.32.0 wires
this module to :mod:`sovyx.voice._hotplug_mac_subprocess` (the polling
``system_profiler`` fallback that already shipped in Sprint 2 step
6.a but had no entry point) and flips the default ON for darwin via
:attr:`VoiceTuningConfig.voice_macos_hotplug_subprocess_enabled`.

The native ``AudioObjectAddPropertyListener`` path stays the canonical
long-term solution per Sprint 4 / Task #28; the subprocess fallback
is the honest "good enough until Sprint 4 lands" patch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.voice._hotplug_mac_subprocess import (
        HotplugEvent as _SubprocessHotplugEvent,
    )
    from sovyx.voice.health.contract import HotplugEvent as _ContractHotplugEvent

logger = get_logger(__name__)


class _SubprocessHotplugListenerAdapter:
    """Bridge :class:`MacosHotplugSubprocessWatchdog` to :class:`HotplugListener`.

    The subprocess watchdog ships with a callback signature that takes
    :class:`sovyx.voice._hotplug_mac_subprocess.HotplugEvent` (a local
    dataclass tied to ``system_profiler``'s output). The watchdog's
    consumer expects :class:`sovyx.voice.health.contract.HotplugEvent`
    (the platform-agnostic contract). This adapter does the translation
    and wraps the watchdog's start/stop into the protocol surface.

    Lifecycle is idempotent per the :class:`HotplugListener` contract:
    a second :meth:`start` is a no-op, :meth:`stop` is safe to call
    even when ``start`` raised.
    """

    def __init__(self, *, interval_s: float = 30.0) -> None:
        # Lazy import — the subprocess watchdog imports asyncio + the
        # darwin-only system_profiler shim; keeping the import lazy
        # means the listener module costs nothing on Linux/Windows
        # boots that never call ``build_macos_hotplug_listener``.
        from sovyx.voice._hotplug_mac_subprocess import (
            MacosHotplugSubprocessWatchdog,
        )

        self._watchdog = MacosHotplugSubprocessWatchdog(interval_s=interval_s)
        self._on_event: Callable[[_ContractHotplugEvent], Awaitable[None]] | None = None
        self._started = False

    async def start(
        self,
        on_event: Callable[[_ContractHotplugEvent], Awaitable[None]],
    ) -> None:
        """Install the watchdog's polling task and forward events.

        The translation maps the subprocess watchdog's
        ``"added"``/``"removed"`` kinds to the contract's
        :attr:`HotplugEventKind.DEVICE_ADDED`/:attr:`DEVICE_REMOVED`,
        and packs the device's CoreAudio UID into ``endpoint_guid``.
        """
        if self._started:
            return
        self._on_event = on_event
        # Re-bind the watchdog's callback to the bridge — we can't
        # pass it at construction time because the contract's
        # ``on_event`` is set in ``start``, not at __init__.
        self._watchdog._on_event = self._dispatch  # noqa: SLF001
        await self._watchdog.start()
        self._started = True
        logger.info(
            "voice_hotplug_listener_started",
            platform="darwin",
            backend="subprocess_polling",
            interval_s=self._watchdog._interval_s,  # noqa: SLF001
        )

    async def stop(self) -> None:
        """Tear down the polling watchdog. Idempotent."""
        if not self._started:
            return
        await self._watchdog.stop()
        self._on_event = None
        self._started = False

    async def _dispatch(self, event: _SubprocessHotplugEvent) -> None:
        """Translate a subprocess :class:`HotplugEvent` into the contract
        form and forward to the watchdog's registered callback."""
        from sovyx.voice.health.contract import HotplugEvent as ContractEvent
        from sovyx.voice.health.contract import HotplugEventKind

        if self._on_event is None:
            return
        kind = (
            HotplugEventKind.DEVICE_ADDED
            if event.kind == "added"
            else HotplugEventKind.DEVICE_REMOVED
        )
        contract_event = ContractEvent(
            kind=kind,
            endpoint_guid=event.device.unique_id or None,
            device_friendly_name=event.device.name or None,
        )
        await self._on_event(contract_event)


def build_macos_hotplug_listener() -> HotplugListener:
    """Return the macOS hot-plug listener.

    v0.32.0 default (Round 3 paranoid audit HIGH-1): when
    :attr:`VoiceTuningConfig.voice_macos_hotplug_subprocess_enabled`
    is True (default for darwin), returns the subprocess-polling
    fallback wrapped to the :class:`HotplugListener` protocol; when
    False, returns :class:`NoopHotplugListener` (the legacy Sprint 2
    behaviour, kept as the manual opt-out path).

    Sprint 4 / Task #28 will swap the subprocess fallback for a
    native :func:`AudioObjectAddPropertyListener` backend; this
    factory's signature stays stable across the swap.
    """
    # Lazy import — the engine config module is heavyweight (loads
    # YAML, walks env). Importing inside the factory keeps the
    # listener module's import surface tight.
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_macos_hotplug_subprocess_enabled:
        logger.info(
            "voice_hotplug_listener_unavailable",
            platform="darwin",
            reason="voice_macos_hotplug_subprocess_enabled=False",
        )
        return NoopHotplugListener(
            reason="macOS subprocess fallback disabled by config",
        )
    logger.info(
        "voice_hotplug_listener_using_subprocess_fallback",
        platform="darwin",
        backend="system_profiler_polling",
        interval_s=tuning.voice_macos_hotplug_subprocess_interval_s,
        note="native AudioObjectAddPropertyListener is Sprint 4 / Task #28",
    )
    return _SubprocessHotplugListenerAdapter(
        interval_s=tuning.voice_macos_hotplug_subprocess_interval_s,
    )


__all__ = [
    "build_macos_hotplug_listener",
]
