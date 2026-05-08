"""Tests for sovyx.dashboard._shared — shared utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard._shared import (
    MIND_ID_SOURCE_EXPLICIT_REQUEST,
    MIND_ID_SOURCE_FALLBACK_DEFAULT,
    MIND_ID_SOURCE_MIND_MANAGER,
    get_active_mind_id,
    resolve_mind_yaml_path_for_request,
)


class TestGetActiveMindId:
    @pytest.mark.asyncio()
    async def test_returns_default_when_no_manager(self) -> None:
        """No MindManager registered → returns 'default'."""
        registry = MagicMock()
        registry.is_registered.return_value = False
        result = await get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_returns_first_active_mind(self) -> None:
        """MindManager with active minds → returns first."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["nyx", "aria"]

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        result = await get_active_mind_id(registry)
        assert result == "nyx"

    @pytest.mark.asyncio()
    async def test_returns_default_when_no_active_minds(self) -> None:
        """MindManager registered but no active minds → returns 'default'."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = []

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        result = await get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_returns_default_on_exception(self) -> None:
        """If MindManager resolution throws → returns 'default'."""
        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        result = await get_active_mind_id(registry)
        assert result == "default"


def _build_mock_request(
    *,
    cached_mind_id: str = "",
    registry: MagicMock | None = None,
    mind_yaml_path_override: Path | None = None,
) -> MagicMock:
    """Build a starlette Request mock with the given app.state shape."""
    req = MagicMock()
    req.app.state.mind_id = cached_mind_id
    req.app.state.registry = registry
    if mind_yaml_path_override is not None:
        req.app.state.mind_yaml_path = mind_yaml_path_override
    else:
        # Mimic missing attribute by raising on getattr-with-default fallback.
        # Easier: just set None; the helper uses ``getattr(..., None)``.
        req.app.state.mind_yaml_path = None
    return req


class TestResolveMindYamlPathForRequest:
    """Phase 3.A Layer B regression — closes anti-pattern #35 reincidence #6.

    Pre-fix ``server.py:775`` set ``app.state.mind_yaml_path = data_dir/"aria"
    /mind.yaml`` once at boot. Multi-mind operators had voice / config /
    onboarding / setup / providers persistence written to a phantom mind.
    The new resolver routes every persistence operation to the active mind's
    YAML per-request.
    """

    @pytest.mark.asyncio()
    async def test_resolves_to_active_mind_yaml_path(self, tmp_path: Path) -> None:
        """Production happy path: resolves to ``data_dir/<active_mind>/mind.yaml``.

        This is the smoking-gun regression — pre-fix the path always pointed
        at ``"aria"``; now it points at the operator's real active mind.
        """
        # Set up two mind directories — operator's real mind ("jonny") and
        # the phantom ("aria") that pre-fix used to receive everything.
        (tmp_path / "jonny").mkdir()
        (tmp_path / "aria").mkdir()

        mind_manager = MagicMock()
        mind_manager.get_active_minds.return_value = ["jonny"]

        eng_cfg = MagicMock()
        eng_cfg.database.data_dir = tmp_path

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            return cls in (MindManager, EngineConfig)

        async def resolve(cls: type) -> object:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            if cls is MindManager:
                return mind_manager
            if cls is EngineConfig:
                return eng_cfg
            raise RuntimeError(f"unexpected resolve({cls})")

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=resolve)

        req = _build_mock_request(registry=registry)
        mind_id, yaml_path, source = await resolve_mind_yaml_path_for_request(req)

        assert mind_id == "jonny"
        assert yaml_path == tmp_path / "jonny" / "mind.yaml"
        # Critical: the path is NOT the phantom "aria" path.
        assert yaml_path != tmp_path / "aria" / "mind.yaml"
        assert source == MIND_ID_SOURCE_MIND_MANAGER

    @pytest.mark.asyncio()
    async def test_explicit_mind_id_override(self, tmp_path: Path) -> None:
        """Routes that receive ``mind_id`` in body can pass it as override."""
        (tmp_path / "explicit-mind").mkdir()

        eng_cfg = MagicMock()
        eng_cfg.database.data_dir = tmp_path

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.config import EngineConfig

            return cls is EngineConfig

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=eng_cfg)

        req = _build_mock_request(registry=registry)
        mind_id, yaml_path, source = await resolve_mind_yaml_path_for_request(
            req, explicit_mind_id="explicit-mind"
        )

        assert mind_id == "explicit-mind"
        assert yaml_path == tmp_path / "explicit-mind" / "mind.yaml"
        assert source == MIND_ID_SOURCE_EXPLICIT_REQUEST

    @pytest.mark.asyncio()
    async def test_explicit_default_sentinel_falls_through(self, tmp_path: Path) -> None:
        """Explicit ``"default"`` is a sentinel — must not be honoured.

        Operator brief misalignment: a frontend that hardcodes
        ``mind_id="default"`` in the request body must NOT cause persistence
        to land at ``data_dir/"default"/mind.yaml``. The resolver falls back
        to the live MindManager lookup.
        """
        (tmp_path / "real-mind").mkdir()

        mind_manager = MagicMock()
        mind_manager.get_active_minds.return_value = ["real-mind"]

        eng_cfg = MagicMock()
        eng_cfg.database.data_dir = tmp_path

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            return cls in (MindManager, EngineConfig)

        async def resolve(cls: type) -> object:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            if cls is MindManager:
                return mind_manager
            if cls is EngineConfig:
                return eng_cfg
            raise RuntimeError(f"unexpected resolve({cls})")

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=resolve)

        req = _build_mock_request(registry=registry)
        mind_id, yaml_path, _ = await resolve_mind_yaml_path_for_request(
            req, explicit_mind_id="default"
        )

        # The sentinel was rejected; resolver fell through to MindManager.
        assert mind_id == "real-mind"
        assert yaml_path == tmp_path / "real-mind" / "mind.yaml"

    @pytest.mark.asyncio()
    async def test_app_state_override_honoured_for_tests(self, tmp_path: Path) -> None:
        """Test/legacy override: ``app.state.mind_yaml_path`` wins if set.

        Pre-Phase-3.A this was the production wire; it's now a test-only
        dependency-injection path. Tests across the suite still set this
        directly; the helper preserves that contract.
        """
        override = tmp_path / "test-override" / "mind.yaml"
        req = _build_mock_request(mind_yaml_path_override=override)
        # No registry → resolver falls back to "default", but the override
        # path takes precedence over re-deriving from data_dir.
        mind_id, yaml_path, _ = await resolve_mind_yaml_path_for_request(req)

        assert yaml_path == override
        assert mind_id == "default"  # cached app_state was empty + no registry

    @pytest.mark.asyncio()
    async def test_returns_none_when_mind_directory_missing(self, tmp_path: Path) -> None:
        """Fresh-install + mind-not-initialised → ``None`` (skip persistence).

        Preserves pre-Phase-3.A semantics: ``app.state.mind_yaml_path`` was
        only set if the file existed at boot; if no mind initialised, the
        consumer's ``if mind_yaml_path is not None:`` guard skipped writes.
        """
        # data_dir exists but no mind directory inside it.
        eng_cfg = MagicMock()
        eng_cfg.database.data_dir = tmp_path

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.config import EngineConfig

            return cls is EngineConfig

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=eng_cfg)

        req = _build_mock_request(registry=registry)
        _, yaml_path, source = await resolve_mind_yaml_path_for_request(req)

        assert yaml_path is None
        assert source == MIND_ID_SOURCE_FALLBACK_DEFAULT

    @pytest.mark.asyncio()
    async def test_returns_none_when_no_registry(self) -> None:
        """No registry on app.state → ``None`` (defensive; can't resolve)."""
        req = _build_mock_request(registry=None)
        _, yaml_path, _ = await resolve_mind_yaml_path_for_request(req)
        assert yaml_path is None

    @pytest.mark.asyncio()
    async def test_returns_none_when_engine_config_resolve_throws(self, tmp_path: Path) -> None:
        """``EngineConfig.resolve`` raising → ``None`` (anti-pattern #33)."""
        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.config import EngineConfig

            return cls is EngineConfig

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        req = _build_mock_request(registry=registry)
        _, yaml_path, _ = await resolve_mind_yaml_path_for_request(req)
        assert yaml_path is None

    @pytest.mark.asyncio()
    async def test_no_aria_hardcode(self, tmp_path: Path) -> None:
        """Anti-regression: ``"aria"`` literal MUST NOT appear in the path
        when the active mind is a different identifier.

        Forensic anchor: ``server.py:775`` pre-Phase-3.A had a hardcoded
        ``data_dir / "aria" / "mind.yaml"`` that defeated multi-mind
        topologies. This test pins the structural fix.
        """
        (tmp_path / "operator-mind").mkdir()

        mind_manager = MagicMock()
        mind_manager.get_active_minds.return_value = ["operator-mind"]

        eng_cfg = MagicMock()
        eng_cfg.database.data_dir = tmp_path

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            return cls in (MindManager, EngineConfig)

        async def resolve(cls: type) -> object:
            from sovyx.engine.bootstrap import MindManager
            from sovyx.engine.config import EngineConfig

            if cls is MindManager:
                return mind_manager
            if cls is EngineConfig:
                return eng_cfg
            raise RuntimeError(f"unexpected resolve({cls})")

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=resolve)

        req = _build_mock_request(registry=registry)
        mind_id, yaml_path, _ = await resolve_mind_yaml_path_for_request(req)

        assert mind_id == "operator-mind"
        assert yaml_path is not None
        assert "aria" not in yaml_path.parts
