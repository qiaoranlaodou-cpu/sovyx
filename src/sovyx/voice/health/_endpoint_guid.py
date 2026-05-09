"""Stable cross-boot identifier for capture endpoints (Phase 5.F.1).

Extracted from ``voice/health/_factory_integration.py`` (anti-pattern
#16 god-file split). The function :func:`derive_endpoint_guid`
implements ADR §1 endpoint_guid semantics — a single resolution-order
authority that ComboStore + audit-trail logging both depend on.

Resolution order (ADR §1):

1. **Windows + APO report** — MMDevices endpoint GUID from the matched
   :class:`~sovyx.voice._apo_detector.CaptureApoReport`. Canonical
   identifier used throughout the Windows audit trail.
2. **Linux, sysfs-available** —
   :func:`~sovyx.voice.health._fingerprint_linux.compute_linux_endpoint_fingerprint`
   derives a stable ID from ``/sys/class/sound/card<N>/device`` symlink
   targets (PCI BDF for onboard codecs, USB VID:PID for USB-audio).
3. **macOS, HAL-available** —
   :func:`~sovyx.voice.health._fingerprint_macos.compute_macos_endpoint_fingerprint`
   uses the CoreAudio device UID.
4. **Fallback surrogate** — SHA256 over
   ``(canonical_name, host_api_name, platform_key)`` formatted as
   ``{surrogate-8-4-4-4-12}``.

The three distinct visual prefixes (``{...win GUID}`` / ``{linux-...}``
/ ``{surrogate-...}``) let operators reading logs tell at a glance
which mechanism produced the record.
"""

from __future__ import annotations

import hashlib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry


def derive_endpoint_guid(
    resolved: DeviceEntry,
    *,
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
) -> str:
    """Return a stable identifier for ``resolved`` across boots.

    Resolution order (ADR §1 endpoint_guid semantics):

    1. **Windows + APO report**: use the MMDevices endpoint GUID
       from the matched :class:`~sovyx.voice._apo_detector.CaptureApoReport`.
       Canonical identifier used throughout the Windows audit trail.

    2. **Linux, sysfs-available**: use
       :func:`~sovyx.voice.health._fingerprint_linux.compute_linux_endpoint_fingerprint`
       which derives a stable ID from ``/sys/class/sound/card<N>/device``
       symlink targets — PCI BDF for onboard codecs, USB VID:PID for
       USB-audio. Returns the equivalent of an MMDevice GUID on
       Linux: ``{linux-pci-<bdf>-codec-<vendor>:<device>-<pcm>-<dir>}``
       or ``{linux-usb-<vid>:<pid>-<pcm>-<dir>}``. Falls through when
       the device is a virtual alias (``"default"`` / ``"pulse"``) or
       sysfs is inaccessible.

    3. **Fallback surrogate** — SHA256 over
       ``(canonical_name, host_api_name, platform_key)`` formatted as
       ``{surrogate-8-4-4-4-12}``. Stable enough to survive normal
       reboots (``canonical_name`` is MME-truncation-normalised) but
       brittle against hotplug reorder, PipeWire/PulseAudio naming
       changes, and cross-host-API queries for the same physical
       device.

    The three distinct visual prefixes (``{...win GUID}`` /
    ``{linux-...}`` / ``{surrogate-...}``) let operators reading logs
    tell at a glance which mechanism produced the record. ComboStore
    accepts any non-empty string per its R12 sanity rule.
    """
    plat = platform_key or sys.platform

    if plat == "win32" and apo_reports:
        from sovyx.voice._apo_detector import find_endpoint_report

        report = find_endpoint_report(apo_reports, device_name=resolved.name)
        if report is not None and report.endpoint_id:
            return report.endpoint_id

    if plat == "linux":
        from sovyx.voice.health._fingerprint_linux import (  # noqa: PLC0415 — lazy-Linux
            compute_linux_endpoint_fingerprint,
        )

        linux_fp = compute_linux_endpoint_fingerprint(resolved)
        if linux_fp is not None:
            return linux_fp

    if plat == "darwin":
        from sovyx.voice.health._fingerprint_macos import (  # noqa: PLC0415 — lazy-Darwin
            compute_macos_endpoint_fingerprint,
        )

        macos_fp = compute_macos_endpoint_fingerprint(resolved)
        if macos_fp is not None:
            return macos_fp

    hasher = hashlib.sha256()
    hasher.update(resolved.canonical_name.encode("utf-8"))
    hasher.update(b"\x1f")
    hasher.update(resolved.host_api_name.encode("utf-8"))
    hasher.update(b"\x1f")
    hasher.update(plat.encode("utf-8"))
    digest = hasher.hexdigest()
    return (
        "{surrogate-"
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        "}"
    )


__all__ = ["derive_endpoint_guid"]
