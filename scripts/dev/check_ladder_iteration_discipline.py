#!/usr/bin/env python3
"""Quality Gate 9 — ladder iteration discipline checker.

Mission C3 §T4.1 — STRICT-flip mechanical enforcement of anti-pattern
#41.

Anti-shape detected: a function in ``src/sovyx/voice/health/`` that
receives a candidate-set parameter (named one of ``candidates``,
``targets``, ``entries`` and typed as an iterable / list / sequence)
AND calls a dispatch helper (``request_device_change_restart``,
``open_stream``, ``dispatch_*``) without that call being inside a
``for`` / ``async for`` / ``while`` loop. That is the structural
signature of the C3 pre-mission ``_try_runtime_failover``:
single-shot dispatch that picks one candidate and returns without
iterating the rest.

The checker is mechanical, AST-based, runs in < 1 s. Invoked from
``scripts/verify_gates.sh`` as Gate 9.

Exit codes:
    0 — no anti-shape detected
    1 — at least one function in scope matches the anti-shape

Mission anchor:
``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T4.1.

Generalises: ``feedback_enterprise_only`` — when a list of candidates
exists, every one must be iterated within a single attempt window
before collapsing to a fallback.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_DIR = _REPO_ROOT / "src" / "sovyx" / "voice" / "health"

# Parameter names that suggest "this function receives a candidate set".
_CANDIDATE_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "candidates",
        "targets",
        "entries",
        "endpoints",
    },
)

# Dispatch helpers that, when called from a function with a candidate
# parameter, MUST be inside a loop.
_DISPATCH_HELPER_ATTRS: frozenset[str] = frozenset(
    {
        "request_device_change_restart",
        "request_exclusive_restart",
        "request_shared_restart",
    },
)


def _has_candidate_param(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True iff any positional / kw-only param name matches the
    candidate-set vocabulary.
    """
    args = node.args
    all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    return any(a.arg in _CANDIDATE_PARAM_NAMES for a in all_args)


def _is_inside_loop(node: ast.AST, function_body: list[ast.AST]) -> bool:
    """Walk the function body to find whether *node* (a Call) is inside
    a ``for`` / ``async for`` / ``while`` loop.
    """

    class _Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.loop_depth = 0
            self.found_in_loop = False

        def _visit_loop(self, n: ast.AST) -> None:
            self.loop_depth += 1
            self.generic_visit(n)
            self.loop_depth -= 1

        def visit_For(self, n: ast.For) -> None:
            self._visit_loop(n)

        def visit_AsyncFor(self, n: ast.AsyncFor) -> None:
            self._visit_loop(n)

        def visit_While(self, n: ast.While) -> None:
            self._visit_loop(n)

        def visit_Call(self, n: ast.Call) -> None:
            if n is node and self.loop_depth > 0:
                self.found_in_loop = True
            self.generic_visit(n)

    walker = _Walker()
    for stmt in function_body:
        walker.visit(stmt)
    return walker.found_in_loop


def _find_dispatch_calls(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Call]:
    """Collect every Call to a dispatch helper inside the function body."""
    hits: list[ast.Call] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _DISPATCH_HELPER_ATTRS:
            continue
        hits.append(node)
    return hits


def _scan_file(path: Path) -> list[tuple[str, str, int]]:
    """Return (file_relative_str, function_name, line) for every
    anti-shape match.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    violations: list[tuple[str, str, int]] = []
    rel = path.relative_to(_REPO_ROOT).as_posix()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _has_candidate_param(node):
            continue
        dispatch_calls = _find_dispatch_calls(node)
        if not dispatch_calls:
            continue
        for call in dispatch_calls:
            if not _is_inside_loop(call, list(node.body)):
                violations.append((rel, node.name, call.lineno))
    return violations


def main() -> int:
    if not _TARGET_DIR.is_dir():
        sys.stderr.write(
            f"Quality Gate 9: target directory not found: {_TARGET_DIR}\n",
        )
        return 1

    all_violations: list[tuple[str, str, int]] = []
    for path in _TARGET_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        all_violations.extend(_scan_file(path))

    if not all_violations:
        print(
            "Quality Gate 9 — ladder iteration discipline: no anti-shape detected.",
        )
        return 0

    sys.stderr.write(
        "Quality Gate 9 — anti-pattern #41 detected:\n",
    )
    for rel, func_name, line in all_violations:
        sys.stderr.write(f"  {rel}:{line} — {func_name}\n")
    sys.stderr.write(
        "\n  A candidate-set parameter + dispatch call outside a loop\n"
        "  signals the C3 single-shot dispatch anti-shape. Refactor\n"
        "  the dispatch into a bounded loop-in-place iterator per\n"
        "  Mission C3 §T1.1, or rename the parameter if it is NOT a\n"
        "  candidate set.\n",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
