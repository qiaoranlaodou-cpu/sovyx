"""Tests for ``scripts/dev/check_ladder_iteration_discipline.py``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T4.1.

The Quality Gate 9 static checker rejects anti-pattern #41
(single-shot candidate dispatch). These tests pin the detector
against synthetic fixtures so a future refactor that breaks the
detection silently (e.g. AST node shape change) surfaces in CI.

The script's AST-walker is exercised via its public functions
imported as a module (matching the C2 §T4.1 ``check_boundary_round_trip_coverage.py``
test pattern at ``test_analyze_c3_telemetry.py``).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "dev"
    / "check_ladder_iteration_discipline.py"
)
_spec = importlib.util.spec_from_file_location("check_ladder_iteration_discipline", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def _write_fixture(tmp_path: Path, content: str) -> Path:
    """Write a synthetic Python fixture file. The checker's
    ``_scan_file`` calls ``path.relative_to(_REPO_ROOT)``, so the
    caller MUST monkeypatch ``checker._REPO_ROOT`` to ``tmp_path``
    before invoking — see the autouse fixture below.
    """
    path = tmp_path / "fixture.py"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the checker's ``_REPO_ROOT`` to point at ``tmp_path`` so
    ``relative_to`` calls inside ``_scan_file`` resolve against a
    location the synthetic fixtures share. End-to-end subprocess test
    is unaffected because it runs the script standalone.
    """
    monkeypatch.setattr(checker, "_REPO_ROOT", tmp_path)


# Synthetic anti-shape: candidate-set parameter + dispatch call
# OUTSIDE a loop. This is the C3 pre-mission ``_try_runtime_failover``
# signature.
_ANTI_SHAPE_SOURCE = """
async def _try_X_failover(*, candidates, capture_task):
    target = candidates[0]
    # Single-shot dispatch — the C3 anti-pattern #41 anti-shape.
    result = await capture_task.request_device_change_restart(target)
    return result
"""


# Synthetic compliant shape: candidate-set parameter + dispatch call
# INSIDE a for loop. Post-Mission-C3 ``_try_runtime_failover`` shape.
_COMPLIANT_SHAPE_SOURCE = """
async def _try_Y_failover(*, candidates, capture_task):
    for target in candidates:
        result = await capture_task.request_device_change_restart(target)
        if result.engaged:
            return result
    return None
"""


# Synthetic compliant: while-loop dispatch (also valid).
_WHILE_LOOP_SOURCE = """
async def _try_Z_failover(*, candidates, capture_task):
    i = 0
    while i < len(candidates):
        result = await capture_task.request_device_change_restart(candidates[i])
        if result.engaged:
            return result
        i += 1
    return None
"""


# Synthetic compliant: async-for-loop dispatch.
_ASYNC_FOR_SOURCE = """
async def _try_W_failover(*, candidates, capture_task):
    async for target in candidates:
        result = await capture_task.request_device_change_restart(target)
        if result.engaged:
            return result
    return None
"""


# Synthetic no-op: function does NOT receive a candidate-set parameter
# → not flagged regardless of dispatch placement.
_NO_CANDIDATE_PARAM_SOURCE = """
async def _open_one(*, target, capture_task):
    return await capture_task.request_device_change_restart(target)
"""


# Synthetic no-op: function has candidate-set parameter but does NOT
# call any dispatch helper → not flagged.
_NO_DISPATCH_CALL_SOURCE = """
def filter_candidates(*, candidates):
    return [c for c in candidates if c.healthy]
"""


class TestAntiShapeDetection:
    """Anti-pattern #41 is detected on synthetic single-shot dispatch."""

    def test_detects_single_shot_dispatch_with_candidates_param(self, tmp_path: Path) -> None:
        fixture = _write_fixture(tmp_path, _ANTI_SHAPE_SOURCE)
        violations = checker._scan_file(fixture)
        assert len(violations) == 1
        rel, func_name, line = violations[0]
        assert func_name == "_try_X_failover"
        assert line > 0


class TestCompliantShapes:
    """Loop-in-place patterns are NOT flagged."""

    def test_for_loop_dispatch_passes(self, tmp_path: Path) -> None:
        fixture = _write_fixture(tmp_path, _COMPLIANT_SHAPE_SOURCE)
        violations = checker._scan_file(fixture)
        assert violations == []

    def test_while_loop_dispatch_passes(self, tmp_path: Path) -> None:
        fixture = _write_fixture(tmp_path, _WHILE_LOOP_SOURCE)
        violations = checker._scan_file(fixture)
        assert violations == []

    def test_async_for_loop_dispatch_passes(self, tmp_path: Path) -> None:
        fixture = _write_fixture(tmp_path, _ASYNC_FOR_SOURCE)
        violations = checker._scan_file(fixture)
        assert violations == []

    def test_no_candidate_param_not_flagged(self, tmp_path: Path) -> None:
        """A function dispatching on a single target (no candidate-set
        parameter) MUST NOT be flagged — the anti-shape is specifically
        about iteration discipline over a SET of candidates.
        """
        fixture = _write_fixture(tmp_path, _NO_CANDIDATE_PARAM_SOURCE)
        violations = checker._scan_file(fixture)
        assert violations == []

    def test_no_dispatch_call_not_flagged(self, tmp_path: Path) -> None:
        """A helper that filters candidates but never dispatches MUST
        NOT be flagged.
        """
        fixture = _write_fixture(tmp_path, _NO_DISPATCH_CALL_SOURCE)
        violations = checker._scan_file(fixture)
        assert violations == []


class TestHelperPredicates:
    """Unit-test the ``_has_candidate_param`` predicate."""

    def test_recognises_canonical_param_names(self) -> None:
        import ast

        for name in ("candidates", "targets", "entries", "endpoints"):
            src = f"def f(*, {name}): pass"
            tree = ast.parse(src)
            func = tree.body[0]
            assert isinstance(func, ast.FunctionDef)
            assert checker._has_candidate_param(func) is True

    def test_rejects_unrelated_param_names(self) -> None:
        import ast

        for name in ("target", "x", "config", "result"):
            src = f"def f(*, {name}): pass"
            tree = ast.parse(src)
            func = tree.body[0]
            assert isinstance(func, ast.FunctionDef)
            assert checker._has_candidate_param(func) is False


class TestEndToEndScriptInvocation:
    """End-to-end: run the script via subprocess against HEAD; assert
    exit 0 (post-mission, no anti-shape).
    """

    def test_runs_clean_against_head(self) -> None:
        result = subprocess.run(  # noqa: S603 — explicit local script
            [sys.executable, str(_SCRIPT_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "no anti-shape detected" in result.stdout
