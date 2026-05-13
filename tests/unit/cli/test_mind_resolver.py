"""Tests for ``sovyx.cli._mind_resolver`` — the shared mind-id resolver.

Covers the full resolution matrix per the mission spec
``docs-internal/missions/MISSION-voice-config-calibrate-enterprise-2026-05-13.md``
§4 Phase 1 (T1.6):

* ``enumerate_minds`` — filesystem-scan correctness over realistic
  ``~/.sovyx/`` layouts (mixed mind dirs, auxiliary dirs, top-level files).
* ``resolve_mind_id`` — explicit-arg + auto-detect paths, including the
  three error surfaces (missing mind, ambiguous, zero minds).

Closes anti-pattern #35 (cross-layer sentinel defaults) by enforcing
that:

* No silent fallback to literal ``"default"`` exists in the resolver.
* Empty / whitespace ``--mind-id`` values are rejected explicitly, not
  silently routed through the auto-detect branch.
* Auto-detect requires exactly one mind; ambiguity is surfaced as an
  actionable error, not a silent first-match.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from sovyx.cli._mind_resolver import enumerate_minds, resolve_mind_id
from sovyx.engine.types import MindId

if TYPE_CHECKING:
    from pathlib import Path


def _seed_mind(data_dir: Path, name: str) -> Path:
    """Create a minimal ``<data_dir>/<name>/mind.yaml`` for the resolver to find."""
    mind_dir = data_dir / name
    mind_dir.mkdir(parents=True)
    yaml_path = mind_dir / "mind.yaml"
    yaml_path.write_text(f"name: {name}\nid: {name}\n", encoding="utf-8")
    return yaml_path


class TestEnumerateMinds:
    """enumerate_minds — list every mind that exists under data_dir."""

    def test_empty_data_dir_returns_empty(self, tmp_path: Path) -> None:
        """A data_dir with no children → empty list."""
        assert enumerate_minds(tmp_path) == []

    def test_nonexistent_data_dir_returns_empty(self, tmp_path: Path) -> None:
        """A data_dir that does not exist on disk → empty list (no error)."""
        missing = tmp_path / "does_not_exist"
        assert enumerate_minds(missing) == []

    def test_data_dir_is_file_returns_empty(self, tmp_path: Path) -> None:
        """A path that is a file rather than a directory → empty list."""
        file_path = tmp_path / "not_a_dir"
        file_path.write_text("", encoding="utf-8")
        assert enumerate_minds(file_path) == []

    def test_single_mind_returned(self, tmp_path: Path) -> None:
        """A single ``jonny/mind.yaml`` → ``[MindId('jonny')]``."""
        _seed_mind(tmp_path, "jonny")
        assert enumerate_minds(tmp_path) == [MindId("jonny")]

    def test_multiple_minds_returned_sorted(self, tmp_path: Path) -> None:
        """Multiple minds returned alphabetically sorted."""
        _seed_mind(tmp_path, "zulu")
        _seed_mind(tmp_path, "alpha")
        _seed_mind(tmp_path, "mike")
        result = enumerate_minds(tmp_path)
        assert result == [MindId("alpha"), MindId("mike"), MindId("zulu")]

    def test_skips_dirs_without_mind_yaml(self, tmp_path: Path) -> None:
        """Directories under data_dir without a mind.yaml are skipped.

        Mirrors the operator's real ``~/.sovyx/`` layout from session
        2026-05-13: ``audit/``, ``logs/``, ``models/``, ``plugins/``,
        ``voice_calibration/``, ``minds/`` exist as siblings of the
        real mind directory but are NOT minds.
        """
        _seed_mind(tmp_path, "jonny")
        for non_mind_dir in (
            "audit",
            "logs",
            "models",
            "plugins",
            "voice_calibration",
            "minds",
        ):
            (tmp_path / non_mind_dir).mkdir()
        (tmp_path / "system.yaml").write_text("", encoding="utf-8")
        (tmp_path / "sovyx.pid").write_text("12345", encoding="utf-8")

        assert enumerate_minds(tmp_path) == [MindId("jonny")]

    def test_skips_dir_where_mind_yaml_is_itself_a_directory(self, tmp_path: Path) -> None:
        """Defensive: ``<mind>/mind.yaml`` as a directory is NOT a mind."""
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "mind.yaml").mkdir()  # not a file
        assert enumerate_minds(tmp_path) == []


class TestResolveMindIdExplicit:
    """resolve_mind_id with an explicit ``--mind-id`` argument."""

    def test_explicit_mind_exists_returns_mind_id(self, tmp_path: Path) -> None:
        """``--mind-id jonny`` + ``jonny/mind.yaml`` exists → ``MindId('jonny')``."""
        _seed_mind(tmp_path, "jonny")
        assert resolve_mind_id("jonny", tmp_path) == MindId("jonny")

    def test_explicit_mind_strips_whitespace(self, tmp_path: Path) -> None:
        """Leading / trailing whitespace stripped before lookup."""
        _seed_mind(tmp_path, "jonny")
        assert resolve_mind_id("  jonny  ", tmp_path) == MindId("jonny")

    def test_explicit_mind_missing_with_available_lists_them(self, tmp_path: Path) -> None:
        """``--mind-id ghost`` when ``jonny`` exists → error lists ``jonny``."""
        _seed_mind(tmp_path, "jonny")
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id("ghost", tmp_path)
        msg = str(exc_info.value)
        assert "ghost" in msg
        assert "jonny" in msg
        assert "not found" in msg.lower()

    def test_explicit_mind_missing_and_no_minds_points_at_init(self, tmp_path: Path) -> None:
        """``--mind-id ghost`` with empty data_dir → suggests ``sovyx init``."""
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id("ghost", tmp_path)
        msg = str(exc_info.value)
        assert "ghost" in msg
        assert "sovyx init" in msg

    def test_explicit_empty_string_rejected(self, tmp_path: Path) -> None:
        """``--mind-id ''`` → BadParameter (empty rejected explicitly)."""
        _seed_mind(tmp_path, "jonny")
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id("", tmp_path)
        assert "empty" in str(exc_info.value).lower()

    def test_explicit_whitespace_only_rejected(self, tmp_path: Path) -> None:
        """``--mind-id '   '`` (whitespace-only) → BadParameter."""
        _seed_mind(tmp_path, "jonny")
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id("   ", tmp_path)
        assert "empty" in str(exc_info.value).lower()


class TestResolveMindIdAutoDetect:
    """resolve_mind_id with no ``--mind-id`` flag (auto-detect path)."""

    def test_none_with_zero_minds_points_at_init(self, tmp_path: Path) -> None:
        """No flag + 0 minds → BadParameter suggesting ``sovyx init``."""
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id(None, tmp_path)
        msg = str(exc_info.value)
        assert "sovyx init" in msg
        assert "no mind configured" in msg.lower()

    def test_none_with_single_mind_auto_detects(self, tmp_path: Path) -> None:
        """No flag + exactly one mind → that mind."""
        _seed_mind(tmp_path, "jonny")
        assert resolve_mind_id(None, tmp_path) == MindId("jonny")

    def test_none_with_single_mind_logs_info(self, tmp_path: Path) -> None:
        """Auto-detect emits structured INFO ``cli.mind_auto_detected``.

        Uses ``structlog.testing.capture_logs`` rather than pytest's
        ``caplog`` because the project configures structlog with a direct
        renderer that does not route every event through the stdlib
        ``logging`` handler ``caplog`` hooks into.
        """
        from structlog.testing import capture_logs

        _seed_mind(tmp_path, "jonny")
        with capture_logs() as captured:
            resolve_mind_id(None, tmp_path)
        events = [entry.get("event") for entry in captured]
        assert "cli.mind_auto_detected" in events, (
            f"expected cli.mind_auto_detected event, got: {events}"
        )
        auto_detected = next(e for e in captured if e.get("event") == "cli.mind_auto_detected")
        assert auto_detected["mind_id"] == "jonny"
        assert auto_detected["log_level"] == "info"

    def test_none_with_two_minds_requires_disambiguation(self, tmp_path: Path) -> None:
        """No flag + 2+ minds → BadParameter asking for ``--mind-id``."""
        _seed_mind(tmp_path, "alpha")
        _seed_mind(tmp_path, "bravo")
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id(None, tmp_path)
        msg = str(exc_info.value)
        assert "alpha" in msg
        assert "bravo" in msg
        assert "--mind-id" in msg

    def test_none_with_many_minds_lists_all_sorted(self, tmp_path: Path) -> None:
        """Disambiguation message lists every mind, alphabetically."""
        _seed_mind(tmp_path, "zulu")
        _seed_mind(tmp_path, "alpha")
        _seed_mind(tmp_path, "mike")
        with pytest.raises(typer.BadParameter) as exc_info:
            resolve_mind_id(None, tmp_path)
        msg = str(exc_info.value)
        assert msg.index("alpha") < msg.index("mike") < msg.index("zulu")


class TestResolveMindIdRealisticDataDir:
    """Real-world: data_dir has aux dirs + top-level files alongside minds."""

    def test_realistic_data_dir_layout_resolves_correctly(self, tmp_path: Path) -> None:
        """Mirrors operator's ``~/.sovyx/`` from session 2026-05-13.

        Layout: ``jonny/mind.yaml`` is the only real mind; ``audit/``,
        ``logs/``, ``models/``, ``plugins/``, ``voice_calibration/``,
        ``minds/`` exist as auxiliary dirs; ``system.yaml``,
        ``channel.env``, ``secrets.env``, ``sovyx.pid``, ``sovyx.port``,
        ``system.db``, ``token`` exist as top-level files. None of those
        should be treated as minds.
        """
        _seed_mind(tmp_path, "jonny")
        for non_mind_dir in (
            "audit",
            "logs",
            "models",
            "plugins",
            "voice_calibration",
            "minds",
        ):
            (tmp_path / non_mind_dir).mkdir()
        for top_level_file in (
            "system.yaml",
            "channel.env",
            "secrets.env",
            "sovyx.pid",
            "sovyx.port",
            "system.db",
            "system.db-shm",
            "system.db-wal",
            "token",
        ):
            (tmp_path / top_level_file).write_text("", encoding="utf-8")

        assert resolve_mind_id(None, tmp_path) == MindId("jonny")
        assert resolve_mind_id("jonny", tmp_path) == MindId("jonny")
        with pytest.raises(typer.BadParameter):
            resolve_mind_id("audit", tmp_path)
        with pytest.raises(typer.BadParameter):
            resolve_mind_id("logs", tmp_path)
        with pytest.raises(typer.BadParameter):
            resolve_mind_id("system.yaml", tmp_path)
