"""F1 falsifiability gate for Mission C5 Quality Gate 11.

Mission C5 §F1 — a deliberately-broken bundle MUST cause the checker
to exit non-zero. The test creates a tmpfs copy of the head bundle,
removes one chunk, runs the checker against the partial fixture, and
asserts:

* exit code != 0
* "PARTIAL" or equivalent verdict label appears in the output

If this test passes on pre-mission HEAD, the checker is too lax —
strengthen the matcher. If it FAILS post-mission, Quality Gate 11 is
broken and Phase 1.A MUST not ship.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"
CHECKER = REPO_ROOT / "scripts" / "dev" / "check_dashboard_bundle_integrity.py"


def _copy_head(tmp: Path) -> Path:
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


@pytest.mark.integration()
def test_partial_bundle_fails_gate11(tmp_path: Path) -> None:
    """F1 falsifiability: a partial bundle MUST cause Gate 11 to fail.

    Mission C5 §F1.
    """
    fixture = _copy_head(tmp_path)
    candidates = sorted((fixture / "assets").glob("*.js"))
    assert len(candidates) > 5, "fixture sanity"
    candidates[0].unlink()

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--static-dir", str(fixture)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    combined = (result.stdout + result.stderr).upper()
    assert result.returncode != 0, (
        f"Gate 11 must fail on partial bundle; got exit={result.returncode}. "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "PARTIAL" in combined or "FAILED" in combined, (
        f"Expected PARTIAL/FAILED in output; got {combined!r}"
    )


@pytest.mark.integration()
def test_index_html_missing_fails_gate11(tmp_path: Path) -> None:
    fixture = tmp_path / "static"
    fixture.mkdir()
    (fixture / "assets").mkdir()  # assets dir exists, no index.html
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--static-dir", str(fixture)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "INDEX_HTML_MISSING" in combined or "FAILED" in combined


@pytest.mark.integration()
def test_static_dir_missing_fails_gate11(tmp_path: Path) -> None:
    target = tmp_path / "nonexistent"  # never created
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--static-dir", str(target)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "STATIC_DIR_MISSING" in combined or "FAILED" in combined


@pytest.mark.integration()
def test_healthy_head_bundle_passes_gate11(tmp_path: Path) -> None:
    """Sanity: the committed HEAD bundle MUST pass Gate 11."""
    fixture = _copy_head(tmp_path)
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--static-dir", str(fixture)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"HEAD bundle must pass; got exit={result.returncode}. "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "FULLY_PRESENT" in (result.stdout + result.stderr)


@pytest.mark.integration()
def test_json_output_parseable(tmp_path: Path) -> None:
    """--json flag emits a parseable JSON report."""
    fixture = _copy_head(tmp_path)
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--static-dir", str(fixture), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    import json

    payload = json.loads(result.stdout)
    assert payload["verdict"] == "fully_present"
    assert isinstance(payload["referenced_count"], int)
    assert payload["referenced_count"] >= 1
    assert isinstance(payload["scan_duration_ms"], (int, float))
