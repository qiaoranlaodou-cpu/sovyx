"""End-to-end integration tests for ``sovyx dashboard doctor`` (Mission C5 §T3.5).

These tests invoke the CLI via :mod:`subprocess` so the full
import-resolution + typer-app construction + ``importlib.resources`` /
``STATIC_DIR`` lookup chain runs exactly as it would for an operator.
Distinct from the unit tests at ``tests/unit/cli/test_dashboard_doctor.py``
which use Typer's ``CliRunner`` (in-process; fast; covers logic) — this
suite covers the install-shape behavior + exit-code contract through
the actual ``sovyx`` entry point.

Marked ``@pytest.mark.integration`` so the default ``pytest`` invocation
(``-m 'not integration'`` per ``pyproject.toml``) skips them; CI explicit
integration runs pick them up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"


def _sovyx_entry() -> list[str]:
    """Resolve the ``sovyx`` entry point for subprocess invocation.

    The package's ``[project.scripts]`` ``sovyx`` entry point is
    ``sovyx.cli.main:app``. ``python -m sovyx.cli.main`` does NOT invoke
    the typer ``app()`` because the module has no ``if __name__ ==
    "__main__"`` guard. The cleanest portable invocation is a tiny
    inline shim: import the app + invoke it.
    """
    return [
        sys.executable,
        "-c",
        "from sovyx.cli.main import app; app()",
    ]


def _copy_head(tmp: Path) -> Path:
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


@pytest.mark.integration()
def test_dashboard_doctor_exits_zero_on_head_install() -> None:
    """Sanity: the head install MUST yield exit 0 + FULLY_PRESENT."""
    result = subprocess.run(
        [*_sovyx_entry(), "dashboard", "doctor"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (
        f"head install must pass; got exit={result.returncode}. "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "FULLY_PRESENT" in result.stdout


@pytest.mark.integration()
def test_dashboard_doctor_json_emits_parseable_payload() -> None:
    """``--json`` output MUST be a single parseable JSON document."""
    result = subprocess.run(
        [*_sovyx_entry(), "dashboard", "doctor", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0
    # Rich's print_json adds no decoration on a TTY-detached run.
    payload = json.loads(result.stdout.strip())
    assert payload["verdict"] == "fully_present"
    assert isinstance(payload["referenced_count"], int)
    assert payload["referenced_count"] >= 1
    assert "scan_duration_ms" in payload


@pytest.mark.integration()
def test_dashboard_doctor_exits_nonzero_on_partial_install(tmp_path: Path) -> None:
    """Subprocess install-shape test: monkey-patch ``STATIC_DIR`` is NOT
    available through subprocess. Instead, point the CLI at a partial
    fixture via ``SOVYX_DATA_DIR`` + a custom static_dir.

    Since the CLI's ``sovyx dashboard doctor`` reads from
    ``sovyx.dashboard.STATIC_DIR`` (compile-time constant) rather than an
    operator-overridable env var, the cleanest integration path is to
    run the **checker script** against the fixture directly. The CLI
    wraps the same scanner; the script delegates to it. This validates
    the operator's escape hatch (``check_dashboard_bundle_integrity.py``
    directly) yields the same verdict as the CLI would on a partial
    install.
    """
    fixture = _copy_head(tmp_path)
    from sovyx.dashboard._integrity import scan_bundle_integrity

    baseline = scan_bundle_integrity(fixture)
    assert baseline.referenced_assets, "fixture sanity"
    (fixture / baseline.referenced_assets[0]).unlink()

    checker = REPO_ROOT / "scripts" / "dev" / "check_dashboard_bundle_integrity.py"
    result = subprocess.run(
        [sys.executable, str(checker), "--static-dir", str(fixture)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "PARTIAL" in combined or "FAILED" in combined


@pytest.mark.integration()
def test_aggregate_doctor_renders_dashboard_section_via_subprocess() -> None:
    """The aggregate ``sovyx doctor`` flow renders the Mission C5 §T3.4
    surface ("Dashboard — bundle integrity") via subprocess.

    Note: ``sovyx doctor`` without a subcommand prints the typer help (the
    aggregate render is wired into the ``voice`` subcommand path per
    historical mission cadence). We invoke the section through the
    section's section header — which appears whenever the doctor flow
    triggers the C5 surface.

    Mirrors the C4 ``_render_voice_degraded_banner_surface`` validation
    pattern: render the section in isolation and assert the section
    header string survives the subprocess boundary.
    """
    # The C5 surface is wired into ``_render_doctor_surfaces`` of the
    # voice doctor flow; invoke ``sovyx doctor voice`` to trigger the
    # full rendering chain.
    result = subprocess.run(
        [*_sovyx_entry(), "doctor", "voice"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    # The voice doctor may exit non-zero if PortAudio is unavailable on
    # the test host; we don't gate on exit code — only on the section
    # rendering, which is observability-only and always runs.
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    output = stdout + stderr
    assert "Dashboard" in output and "bundle integrity" in output, (
        f"aggregate doctor did NOT render the Mission C5 §T3.4 section; output={output!r}"
    )
