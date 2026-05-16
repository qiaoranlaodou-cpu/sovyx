"""Tests for ``select_alternative_endpoint(recent_probe_results=...)``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.2.

Pin the new optional ``recent_probe_results`` parameter that
``_runtime_failover._try_runtime_failover`` will pass in Phase 2.D:

* When the cache flags a candidate via ``is_known_unopenable``, the
  selector skips it AS IF it were quarantined.
* The skip composes with the existing quarantine + explicit-exclusion
  filters (a candidate skipped by EITHER quarantine OR the cache is
  excluded; not both required).
* ``None`` default preserves pre-Mission-C3 behaviour bit-exactly.
* The cache lookup probes both the GUID and the canonical_name keys
  per the belt-and-suspenders pattern at
  ``_cascade_verdict.py:_is_skippable``.
* Successful invalidation via ``record_success`` removes the skip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.health._cascade_verdict import select_alternative_endpoint
from sovyx.voice.health._endpoint_guid import derive_endpoint_guid
from sovyx.voice.health._probe_result_cache import (
    ProbeResultCache,
    ProbeResultEntry,
)
from sovyx.voice.health._quarantine import EndpointQuarantine

if TYPE_CHECKING:
    import pytest


def _input_entry(
    *,
    index: int,
    name: str,
    host_api_name: str = "ALSA",
    is_default: bool = False,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name=host_api_name,
        max_input_channels=1,
        max_output_channels=0,
        default_samplerate=48_000,
        is_os_default=is_default,
    )


class TestProbeCacheConsult:
    """``recent_probe_results`` parameter — ADR-D4 skip-on-bad-probe."""

    def test_none_preserves_pre_mission_behaviour(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default ``None`` MUST yield identical results to pre-mission."""
        from sovyx.voice import device_enum

        good = _input_entry(index=0, name="Good Mic", is_default=True)
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [good])
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)

        # Without cache.
        result_without = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
        )
        # With cache=None.
        result_with_none = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=None,
        )
        assert result_without is not None
        assert result_with_none is not None
        assert result_without.name == result_with_none.name == "Good Mic"

    def test_cache_skip_via_no_signal_verdict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Candidate flagged ``NO_SIGNAL`` by cache MUST be skipped."""
        from sovyx.voice import device_enum

        dead = _input_entry(index=0, name="Dead Mic")
        good = _input_entry(index=1, name="Good Mic")
        monkeypatch.setattr(
            device_enum,
            "enumerate_devices",
            lambda: [dead, good],
        )
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)

        cache = ProbeResultCache()
        dead_guid = derive_endpoint_guid(dead, apo_reports=None, platform_key="win32")
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid=dead_guid,
                host_api="ALSA",
                verdict="NO_SIGNAL",
            ),
        )

        result = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        assert result is not None
        assert result.name == "Good Mic"

    def test_cache_skip_via_unopenable_error_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``-9985 paDeviceUnavailable`` flags UNOPENABLE_THIS_BOOT."""
        from sovyx.voice import device_enum

        busy = _input_entry(index=0, name="Busy Mic")
        free = _input_entry(index=1, name="Free Mic")
        monkeypatch.setattr(
            device_enum,
            "enumerate_devices",
            lambda: [busy, free],
        )
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)

        cache = ProbeResultCache()
        busy_guid = derive_endpoint_guid(busy, apo_reports=None, platform_key="win32")
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid=busy_guid,
                host_api="ALSA",
                verdict="",
                error_code="-9985",
            ),
        )

        result = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        assert result is not None
        assert result.name == "Free Mic"

    def test_cache_does_not_skip_format_retry_codes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``-9986`` (FORMAT_RETRYABLE) MUST NOT skip — opener handles."""
        from sovyx.voice import device_enum

        formaty = _input_entry(index=0, name="Format Mic", is_default=True)
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [formaty])
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)

        cache = ProbeResultCache()
        formaty_guid = derive_endpoint_guid(formaty, apo_reports=None, platform_key="win32")
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid=formaty_guid,
                host_api="ALSA",
                verdict="",
                error_code="-9986",  # paInvalidSampleRate — FORMAT_RETRYABLE
            ),
        )

        result = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        # The cache should NOT skip this — opener handles via permutation.
        assert result is not None
        assert result.name == "Format Mic"

    def test_record_success_re_admits_skipped_candidate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADR-D5 — ``record_success`` clears the dead entry; the
        candidate is admitted again on next selection.
        """
        from sovyx.voice import device_enum

        cycler = _input_entry(index=0, name="Cycler Mic", is_default=True)
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [cycler])
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)

        cache = ProbeResultCache()
        cycler_guid = derive_endpoint_guid(cycler, apo_reports=None, platform_key="win32")
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid=cycler_guid,
                host_api="ALSA",
                verdict="NO_SIGNAL",
            ),
        )

        # First call — cache says skip → no candidate (only one device).
        result_skipped = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        assert result_skipped is None

        # Invalidate the dead entry — simulates a successful open of
        # the same device (operator re-plugged USB, etc.).
        cache.record_success(cycler_guid, "ALSA")

        # Second call — cache no longer skips; candidate admitted.
        result_admitted = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        assert result_admitted is not None
        assert result_admitted.name == "Cycler Mic"

    def test_quarantine_and_cache_compose(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Quarantine and cache filters compose — candidate skipped
        by EITHER one is excluded.
        """
        from sovyx.voice import device_enum

        quarantined = _input_entry(index=0, name="Quarantined Mic")
        cached_dead = _input_entry(index=1, name="Cached Dead Mic")
        survivor = _input_entry(index=2, name="Survivor Mic")
        monkeypatch.setattr(
            device_enum,
            "enumerate_devices",
            lambda: [quarantined, cached_dead, survivor],
        )

        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        quarantined_guid = derive_endpoint_guid(
            quarantined,
            apo_reports=None,
            platform_key="win32",
        )
        q.add(endpoint_guid=quarantined_guid)

        cache = ProbeResultCache()
        cached_dead_guid = derive_endpoint_guid(
            cached_dead,
            apo_reports=None,
            platform_key="win32",
        )
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid=cached_dead_guid,
                host_api="ALSA",
                verdict="NO_SIGNAL",
            ),
        )

        result = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            recent_probe_results=cache,
        )
        # Both Quarantined Mic and Cached Dead Mic skipped; survivor wins.
        assert result is not None
        assert result.name == "Survivor Mic"
