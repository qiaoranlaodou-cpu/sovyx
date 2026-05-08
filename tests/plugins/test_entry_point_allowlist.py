"""Tests for v0.32.0 Phase C M1 — entry-point supply-chain allowlist.

Default-deny third-party pip packages registering ``sovyx.plugins``
entry points. First-party (``ep.dist.name == "sovyx"``) plugins always
load. Third-party plugins require explicit operator opt-in via
``EngineConfig.plugins.allow_third_party_plugins`` +
``trusted_plugin_packages``.

The test fixtures use ``MagicMock`` for the entry-point object,
exposing the same ``.name`` / ``.dist.name`` / ``.load`` shape that
``importlib.metadata.entry_points`` returns at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from sovyx.plugins.manager import PluginManager
from sovyx.plugins.sdk import ISovyxPlugin, tool

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.plugins.permissions import Permission


class _DemoPlugin(ISovyxPlugin):
    """Minimal plugin used to verify entry-point load wiring."""

    @property
    def name(self) -> str:
        return "demo"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Demo plugin."

    @property
    def permissions(self) -> list[Permission]:
        return []

    @tool(description="Echo")
    async def echo(self, msg: str) -> str:
        return msg


def _make_ep(*, name: str, dist_name: str, plugin_class: type[ISovyxPlugin]) -> MagicMock:
    """Build a MagicMock entry point matching the importlib.metadata shape."""
    ep = MagicMock()
    ep.name = name
    ep.dist = MagicMock()
    ep.dist.name = dist_name
    ep.load = MagicMock(return_value=plugin_class)
    return ep


class TestEntryPointAllowlist:
    """v0.32.0 Phase C M1 — supply-chain default-deny."""

    def test_third_party_entry_point_skipped_by_default(self, tmp_path: Path) -> None:
        """allow_third_party_plugins=False (default): skip + log."""
        ep = _make_ep(name="evil", dist_name="evil-pkg", plugin_class=_DemoPlugin)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        from sovyx.plugins import manager as _mgr_mod

        with (
            patch("importlib.metadata.entry_points", return_value=[ep]),
            patch.object(_mgr_mod.logger, "warning") as mock_warn,
        ):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        # Plugin NOT loaded.
        assert plugins == []
        # ep.load() never called — the supply-chain contract.
        ep.load.assert_not_called()
        # Structured skip event logged with reason=default_deny.
        warn_events = [c for c in mock_warn.call_args_list]
        skip_calls = [
            c
            for c in warn_events
            if c.args and c.args[0] == "plugin.entry_point.skipped_third_party"
        ]
        assert len(skip_calls) == 1
        kwargs = skip_calls[0].kwargs
        assert kwargs.get("package") == "evil-pkg"
        assert kwargs.get("reason") == "default_deny"

    def test_third_party_entry_point_skipped_when_not_in_allowlist(self, tmp_path: Path) -> None:
        """allow_third_party_plugins=True but package not allowlisted: skip."""
        ep = _make_ep(name="other", dist_name="other-pkg", plugin_class=_DemoPlugin)

        mgr = PluginManager(
            data_dir=tmp_path,
            discover_entry_points=False,
            allow_third_party_plugins=True,
            trusted_plugin_packages=["my-trusted-pkg"],
        )
        from sovyx.plugins import manager as _mgr_mod

        with (
            patch("importlib.metadata.entry_points", return_value=[ep]),
            patch.object(_mgr_mod.logger, "warning") as mock_warn,
        ):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert plugins == []
        ep.load.assert_not_called()
        skip_calls = [
            c
            for c in mock_warn.call_args_list
            if c.args and c.args[0] == "plugin.entry_point.skipped_third_party"
        ]
        assert len(skip_calls) == 1
        assert skip_calls[0].kwargs.get("reason") == "not_in_allowlist"
        assert skip_calls[0].kwargs.get("package") == "other-pkg"

    def test_third_party_entry_point_loaded_when_allowed(self, tmp_path: Path) -> None:
        """allow_third_party_plugins=True AND package in allowlist: load."""
        ep = _make_ep(name="trusted", dist_name="my-trusted-pkg", plugin_class=_DemoPlugin)

        mgr = PluginManager(
            data_dir=tmp_path,
            discover_entry_points=False,
            allow_third_party_plugins=True,
            trusted_plugin_packages=["my-trusted-pkg"],
        )
        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert len(plugins) == 1
        assert plugins[0] is _DemoPlugin
        ep.load.assert_called_once()

    def test_first_party_always_loaded_default(self, tmp_path: Path) -> None:
        """ep.dist.name == 'sovyx': loaded regardless of gate (default-OFF)."""
        ep = _make_ep(name="builtin", dist_name="sovyx", plugin_class=_DemoPlugin)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert len(plugins) == 1
        assert plugins[0] is _DemoPlugin

    def test_first_party_always_loaded_with_gate_enabled(self, tmp_path: Path) -> None:
        """ep.dist.name == 'sovyx': loaded regardless of allowlist contents."""
        ep = _make_ep(name="builtin", dist_name="sovyx", plugin_class=_DemoPlugin)

        mgr = PluginManager(
            data_dir=tmp_path,
            discover_entry_points=False,
            allow_third_party_plugins=True,
            trusted_plugin_packages=[],  # Empty allowlist; first-party still loads.
        )
        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert len(plugins) == 1

    def test_missing_dist_treated_as_third_party(self, tmp_path: Path) -> None:
        """Entry point with no .dist (legacy) is fail-closed: skipped."""
        ep = MagicMock()
        ep.name = "weird"
        ep.dist = None  # Older importlib.metadata variants.
        ep.load = MagicMock(return_value=_DemoPlugin)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with patch("importlib.metadata.entry_points", return_value=[ep]):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert plugins == []
        ep.load.assert_not_called()

    def test_mixed_eps_only_first_party_loaded_by_default(self, tmp_path: Path) -> None:
        """Mix of first-party + third-party: only the first-party loads."""

        class _OtherPlugin(_DemoPlugin):
            @property
            def name(self) -> str:
                return "other"

        first_party = _make_ep(name="builtin", dist_name="sovyx", plugin_class=_DemoPlugin)
        third_party = _make_ep(name="evil", dist_name="evil-pkg", plugin_class=_OtherPlugin)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with patch(
            "importlib.metadata.entry_points",
            return_value=[first_party, third_party],
        ):
            plugins = mgr._discover_entry_points()  # noqa: SLF001

        assert plugins == [_DemoPlugin]
        first_party.load.assert_called_once()
        third_party.load.assert_not_called()


class TestPluginConfigDefaults:
    """v0.32.0 Phase C M1 — engine-config defaults are safe."""

    def test_default_disallows_third_party(self) -> None:
        from sovyx.engine.config import PluginConfig

        cfg = PluginConfig()
        assert cfg.allow_third_party_plugins is False
        assert cfg.trusted_plugin_packages == []

    def test_engine_config_exposes_plugins(self) -> None:
        from sovyx.engine.config import EngineConfig

        cfg = EngineConfig()
        assert cfg.plugins.allow_third_party_plugins is False
        assert cfg.plugins.trusted_plugin_packages == []
