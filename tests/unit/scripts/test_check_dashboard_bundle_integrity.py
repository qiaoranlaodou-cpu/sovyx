"""Unit tests for scripts/dev/check_dashboard_bundle_integrity.py (Mission C5 §T1.2).

These tests exercise the checker's argument parsing, JSON output shape,
and human-readable rendering. The forensic-replay falsifiability gate
lives in ``tests/integration/scripts/test_check_dashboard_bundle_integrity_falsifiability.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "dev" / "check_dashboard_bundle_integrity.py"


@pytest.fixture()
def checker_module() -> object:
    """Load the script as a module so we can call ``main()`` directly."""
    spec = importlib.util.spec_from_file_location(
        "check_dashboard_bundle_integrity",
        CHECKER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestMainEntry:
    def test_default_static_dir_passes_at_head(
        self,
        checker_module: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = checker_module.main([])  # type: ignore[attr-defined]
        captured = capsys.readouterr()
        assert rc == 0
        assert "FULLY_PRESENT" in captured.out

    def test_static_dir_missing_returns_nonzero(
        self,
        checker_module: object,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "nonexistent"
        rc = checker_module.main(["--static-dir", str(target)])  # type: ignore[attr-defined]
        captured = capsys.readouterr()
        assert rc != 0
        combined = captured.out + captured.err
        assert "STATIC_DIR_MISSING" in combined or "FAILED" in combined

    def test_json_output_emits_valid_payload(
        self,
        checker_module: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = checker_module.main(["--json"])  # type: ignore[attr-defined]
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["verdict"] == "fully_present"
        assert "scan_duration_ms" in payload
        assert "referenced_count" in payload
        assert payload["referenced_count"] >= 1
        assert "missing_assets" in payload
        assert isinstance(payload["missing_assets"], list)

    def test_partial_bundle_returns_nonzero(
        self,
        checker_module: object,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Build a synthetic partial bundle by removing a chunk that
        # index.html ACTUALLY references (some assets on disk are
        # orphan stale chunks from prior builds; deleting an orphan
        # would NOT cause PARTIAL).
        import shutil

        from sovyx.dashboard._integrity import scan_bundle_integrity

        head_static = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"
        fixture = tmp_path / "static"
        shutil.copytree(head_static, fixture)
        baseline = scan_bundle_integrity(fixture)
        assert baseline.referenced_assets, "fixture sanity"
        (fixture / baseline.referenced_assets[0]).unlink()
        rc = checker_module.main(["--static-dir", str(fixture)])  # type: ignore[attr-defined]
        captured = capsys.readouterr()
        assert rc != 0
        combined = captured.out + captured.err
        assert "PARTIAL" in combined or "FAILED" in combined
