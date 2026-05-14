"""Tests for ``sovyx.dashboard._shared`` mind-id resolution helpers.

Phase 6.T6.3 (v0.40.1) — pins the observability WARN contract on the
``"default"`` fallback paths in :func:`get_active_mind_id` and
:func:`resolve_active_mind_id_for_request`. The fallback is preserved
intentionally for test fixtures + transient bootstrap states (see
the function docstrings); the WARN exists so any PRODUCTION occurrence
is grep-able as a regression of the Phase 1.T1.5 daemon-boot gate.

Logger-spy pattern (project standard post-v0.39.2 Windows-CI flake fix):
patches ``_shared.logger`` with :class:`MagicMock` so assertions are
robust across structlog routing configs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.dashboard import _shared

_FALLBACK_EVENT = "dashboard.shared.fallback_default_mind"


def _fallback_warn_calls(spy: MagicMock) -> list:
    """Filter the spy's ``warning(...)`` calls to the fallback event."""
    return [
        call
        for call in spy.warning.call_args_list
        if call.args and call.args[0] == _FALLBACK_EVENT
    ]


class TestGetActiveMindIdFallback:
    """``get_active_mind_id`` returns ``"default"`` + WARN when no minds."""

    @pytest.mark.asyncio()
    async def test_returns_real_mind_no_warn_when_mind_manager_has_minds(self) -> None:
        """Happy path — MindManager has at least one active mind → real id
        returned, no fallback WARN fires."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["jonny"]
        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(return_value=mock_manager)

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            result = await _shared.get_active_mind_id(registry)

        assert result == "jonny"
        assert _fallback_warn_calls(spy) == []

    @pytest.mark.asyncio()
    async def test_returns_default_with_warn_when_mind_manager_unregistered(
        self,
    ) -> None:
        """MindManager not registered (test fixture / transient bootstrap)
        → literal "default" returned + structured WARN fires."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            result = await _shared.get_active_mind_id(registry)

        assert result == "default"
        warns = _fallback_warn_calls(spy)
        assert len(warns) == 1
        kwargs = warns[0].kwargs
        assert kwargs["callsite"] == "get_active_mind_id"
        assert "Phase 1.T1.5" in kwargs["reason"]

    @pytest.mark.asyncio()
    async def test_returns_default_with_warn_when_mind_manager_empty(self) -> None:
        """MindManager registered but holding no minds (boot race) → literal
        "default" returned + WARN."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = []
        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(return_value=mock_manager)

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            result = await _shared.get_active_mind_id(registry)

        assert result == "default"
        warns = _fallback_warn_calls(spy)
        assert len(warns) == 1
        assert warns[0].kwargs["callsite"] == "get_active_mind_id"


class TestResolveActiveMindIdForRequestFallback:
    """``resolve_active_mind_id_for_request`` falls back to ("default", FALLBACK)
    + WARN when neither app_state nor MindManager produces a real mind."""

    @pytest.mark.asyncio()
    async def test_no_warn_when_app_state_has_real_mind(self) -> None:
        """``request.app.state.mind_id`` set to a real value → returned
        directly with MIND_ID_SOURCE_APP_STATE; no fallback WARN."""
        request = MagicMock()
        request.app.state.mind_id = "jonny"

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            mind_id, source = await _shared.resolve_active_mind_id_for_request(request)

        assert mind_id == "jonny"
        assert source == _shared.MIND_ID_SOURCE_APP_STATE
        assert _fallback_warn_calls(spy) == []

    @pytest.mark.asyncio()
    async def test_no_warn_when_mind_manager_resolves(self) -> None:
        """app_state empty but MindManager returns a real mind → source
        is MIND_ID_SOURCE_MIND_MANAGER; no fallback WARN."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["jonny"]
        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(return_value=mock_manager)

        request = MagicMock()
        request.app.state.mind_id = ""
        request.app.state.registry = registry

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            mind_id, source = await _shared.resolve_active_mind_id_for_request(request)

        assert mind_id == "jonny"
        assert source == _shared.MIND_ID_SOURCE_MIND_MANAGER
        assert _fallback_warn_calls(spy) == []

    @pytest.mark.asyncio()
    async def test_warn_when_falling_through_to_default_sentinel(self) -> None:
        """Neither cache nor registry produces a real mind → returns
        ("default", FALLBACK_DEFAULT) + WARN fires for the
        ``resolve_active_mind_id_for_request`` callsite."""
        # Note: get_active_mind_id's own fallback ALSO fires its WARN in
        # this path (registry has no MindManager), so we expect TWO
        # WARNs from distinct callsites — one per layer of the
        # resolution chain. Both are useful operationally because they
        # pin the boundary at which the fallback was selected.
        request = MagicMock()
        request.app.state.mind_id = ""
        registry = MagicMock()
        registry.is_registered.return_value = False
        request.app.state.registry = registry

        spy = MagicMock()
        with patch.object(_shared, "logger", spy):
            mind_id, source = await _shared.resolve_active_mind_id_for_request(request)

        assert mind_id == "default"
        assert source == _shared.MIND_ID_SOURCE_FALLBACK_DEFAULT
        warns = _fallback_warn_calls(spy)
        callsites = {call.kwargs["callsite"] for call in warns}
        # Both layers should have surfaced their boundary in the WARN
        # chain — get_active_mind_id (inner) + resolve_active_mind_id_for_request (outer).
        assert "get_active_mind_id" in callsites
        assert "resolve_active_mind_id_for_request" in callsites
