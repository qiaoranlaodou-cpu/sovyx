"""LIVE-1 Bug B regression — degraded-banner navigate chip targets must
point at a REGISTERED dashboard route.

Background: server-side producers build ``ActionChip(action="navigate",
target=...)`` (via ``make_action_chip(label, "navigate", target)``) for the
composite degraded banner. The React Router falls through to its ``*``
NotFound route for any unregistered path, so a chip whose target is not a
real route renders an SPA-404 — the operator clicks "fix" and lands on
"Page not found". This shipped for `/settings/providers`, `/settings/voice`
and `/voice/logs` (Mission D D-P0-2/D-P0-3 + LIVE-1).

This test statically scans ``src/sovyx`` for every navigate-chip target that
is a string literal and asserts the path part is a registered route. It is a
SCOPED regression for the dead-chip-target class — NOT the AP #61 build-time
Python<->React parity gate (Mission D full remediation, out of scope here).

``REGISTERED_ROUTES`` mirrors ``dashboard/src/router.tsx`` and MUST be kept in
sync when routes are added/removed. Non-literal targets (e.g. computed
``primary_target`` in the resource-cohort governor) cannot be resolved
statically and are skipped — they are exercised by their own producer tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Path part of every registered route in dashboard/src/router.tsx (leading
# slash; the parametrized ``engine/resources/{heap,thread}-snapshot/:ts``
# routes are never chip targets and are intentionally omitted).
REGISTERED_ROUTES: frozenset[str] = frozenset(
    {
        "/",
        "/chat",
        "/conversations",
        "/brain",
        "/emotions",
        "/productivity",
        "/logs",
        "/settings",
        "/settings/providers",  # LIVE-1 Bug B
        "/settings/voice",  # LIVE-1 Bug B
        "/plugins",
        "/about",
        "/voice",
        "/voice/health",
        "/voice/platform-diagnostics",
        "/engine/resources",
        "/onboarding",
    }
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "sovyx"


def _path_part(target: str) -> str:
    """Strip the client-side ``#fragment`` — it never reaches the router."""
    return target.split("#", 1)[0]


def _navigate_targets_in(tree: ast.AST) -> list[str]:
    """Collect string-literal navigate targets from one module AST.

    Matches both call shapes used by producers:
    * ``make_action_chip(label, "navigate", "<target>", ...)`` (positional)
    * ``ActionChip(action="navigate", target="<target>", ...)`` (keyword)
    """
    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)

        if name == "make_action_chip":
            # positional: (label_token, action, target, ...)
            if len(node.args) >= 3:  # noqa: PLR2004
                action, target = node.args[1], node.args[2]
                if (
                    isinstance(action, ast.Constant)
                    and action.value == "navigate"
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                ):
                    targets.append(target.value)
        elif name == "ActionChip":
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            action, target = kwargs.get("action"), kwargs.get("target")
            if (
                isinstance(action, ast.Constant)
                and action.value == "navigate"
                and isinstance(target, ast.Constant)
                and isinstance(target.value, str)
            ):
                targets.append(target.value)
    return targets


def _all_navigate_targets() -> list[tuple[str, str]]:
    """Return ``(relative_file, target)`` for every literal navigate target."""
    found: list[tuple[str, str]] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(_SRC_ROOT).as_posix()
        found.extend((rel, target) for target in _navigate_targets_in(tree))
    return found


def test_navigate_chip_target_corpus_is_non_empty() -> None:
    """Guard the scanner itself — if this hits zero the AST match silently
    broke and the resolution assertions below would be vacuously green."""
    targets = _all_navigate_targets()
    flat = {t for _, t in targets}
    # Anchor on the three LIVE-1 / Mission D targets that motivated this test.
    assert "/settings/providers" in flat
    assert "/settings/voice" in flat
    assert "/logs" in flat
    # The dead route must be gone.
    assert "/voice/logs" not in flat


def test_every_literal_navigate_target_is_a_registered_route() -> None:
    """No server-emitted navigate chip may point at an unregistered route."""
    dead: list[tuple[str, str]] = [
        (rel, target)
        for rel, target in _all_navigate_targets()
        if _path_part(target) not in REGISTERED_ROUTES
    ]
    assert dead == [], (
        "Degraded-banner navigate chips target unregistered routes "
        f"(SPA-404): {dead}. Register the route in dashboard/src/router.tsx "
        "and REGISTERED_ROUTES, or repoint the chip."
    )


@pytest.mark.parametrize(
    "target",
    ["/settings/providers", "/settings/voice", "/logs"],
)
def test_live1_bug_b_targets_are_registered(target: str) -> None:
    """Explicit pins for the three in-scope LIVE-1 Bug B targets."""
    assert target in REGISTERED_ROUTES
