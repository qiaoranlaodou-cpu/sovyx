"""Composite-axes population test for the ``voice_status`` producer.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.7 + §T1.13.

Asserts that ``dashboard/voice_status.py:get_voice_status`` correctly
populates ``status["degraded"]["composite_axes"]`` +
``status["degraded"]["composite_severity"]`` from the cross-axis
``EngineDegradedStore`` via ``_compute_composite_severity``.

The producer's contract is that ``/api/voice/status`` returns the
SAME composite view that ``/api/engine/degraded`` exposes — operators
on legacy /api/voice/status consumers (pre-C4 dashboards, scripts,
external integrations) see the composite state without polling the
new endpoint. Tested across the 0/1/2/3-axis cases per ADR-D6.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from sovyx.dashboard.voice_status import get_voice_status
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)


def _make_entry(axis: str, reason: str) -> DegradedEntry:
    ts = time.monotonic()
    return DegradedEntry(
        axis=axis,
        reason=reason,
        severity="warn",
        title_token=f"degraded.{axis}.title",
        body_token=f"degraded.{axis}.body",
        action_chips=(),
        metadata={},
        first_observed_monotonic=ts,
        last_observed_monotonic=ts,
        occurrence_count=1,
    )


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture()
def mock_registry() -> MagicMock:
    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=False)
    return registry


class TestVoiceStatusCompositeAxes:
    """Mission C4 §T1.7 — composite_axes + composite_severity producer."""

    @pytest.mark.asyncio()
    async def test_empty_store_yields_no_composite(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """0 axes → composite_axes=[] + composite_severity=None."""
        status = await get_voice_status(mock_registry)
        degraded = status["degraded"]
        assert degraded["composite_axes"] == []
        assert degraded["composite_severity"] is None

    @pytest.mark.asyncio()
    async def test_single_axis_yields_warn(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """1 axis (voice) → composite_severity=warn."""
        get_default_degraded_store().record(_make_entry("voice", "a"))
        status = await get_voice_status(mock_registry)
        degraded = status["degraded"]
        assert degraded["composite_axes"] == ["voice"]
        assert degraded["composite_severity"] == "warn"

    @pytest.mark.asyncio()
    async def test_two_axes_yield_error(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """2 distinct axes (voice + llm) → composite_severity=error."""
        store = get_default_degraded_store()
        store.record(_make_entry("voice", "a"))
        store.record(_make_entry("llm", "b"))
        status = await get_voice_status(mock_registry)
        degraded = status["degraded"]
        assert sorted(degraded["composite_axes"]) == ["llm", "voice"]
        assert degraded["composite_severity"] == "error"

    @pytest.mark.asyncio()
    async def test_three_axes_yield_critical(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """3 distinct axes → composite_severity=critical (operator session)."""
        store = get_default_degraded_store()
        store.record(_make_entry("voice", "a"))
        store.record(_make_entry("llm", "b"))
        store.record(_make_entry("stt", "c"))
        status = await get_voice_status(mock_registry)
        degraded = status["degraded"]
        assert sorted(degraded["composite_axes"]) == ["llm", "stt", "voice"]
        assert degraded["composite_severity"] == "critical"

    @pytest.mark.asyncio()
    async def test_distinct_axes_count_not_entry_count(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Two entries on the SAME axis still count as 1 axis for severity.

        Synergy guardrail per ADR-D6: severity escalates by distinct
        axis count, not entry count. Future telemetry surfaces multiple
        reasons per axis (e.g. llm.no_provider + llm.rate_limited) but
        the operator-facing severity stays at 1 axis until a SECOND
        axis (voice, stt, …) joins.
        """
        store = get_default_degraded_store()
        store.record(_make_entry("voice", "reason_a"))
        store.record(_make_entry("voice", "reason_b"))
        status = await get_voice_status(mock_registry)
        degraded = status["degraded"]
        assert degraded["composite_axes"] == ["voice"]
        assert degraded["composite_severity"] == "warn"
