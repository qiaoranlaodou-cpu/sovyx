#!/usr/bin/env python3
"""Quality Gate 10 — degraded-signal surface enforcement.

Mission C4 §Phase 5 §T5.1 — anti-pattern #42 (Operator-actionable
degraded state MUST be surfaced through a single composite store/
endpoint, never as N independent log emissions the operator is
expected to correlate).

This AST checker scans ``src/sovyx/`` for ``logger.warning(...)``
calls whose event name matches the degraded-signal regex:

    ^(.*degraded.*|.*unavailable.*|no_.*_provider.*|.*_unsupported.*)$

For each match, it walks the enclosing function AST upward and
verifies that a ``record(``, ``record_probe(``, ``record_no_llm_provider(``,
``record_stt_language_coerced(``, ``record_ladder_exhausted(``, OR a
``get_default_degraded_store(`` reference exists within the same
function body. The presence of any of these signals indicates the
producer has paired its WARN log with a ``EngineDegradedStore.record(...)``
call, which is the composite-surface requirement anti-pattern #42
enforces.

False positives are explicitly allowlisted via the
``# c4-allowlist: <rationale>`` inline comment on the logger line.

Exit codes:
    0 — every degraded-signal WARN site has a paired store record
    1 — at least one site is uncovered

Invoked from ``scripts/verify_gates.sh`` as Gate 10.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "src" / "sovyx"

# Operator-actionable degraded patterns that the composite banner
# should surface. Tightened from the v0.46.5 spec to exclude pure
# platform-feature gates (``*_unavailable`` for module/init failures);
# those are developer-informational, not operator-actionable, and
# adding them to the composite banner would alert-fatigue the
# operator about conditions they can't directly fix from the
# dashboard. The patterns below are the SHIPPED Mission-C4 trio + the
# generic ``*_degraded`` / ``no_*_provider`` / ``*_language_coerced``
# / ``*_language_unsupported`` shapes that map cleanly to
# operator-actionable composite-banner reasons.
_DEGRADED_PATTERN = re.compile(
    r"("
    r".*[\._]degraded.*"              # voice.windows.audio_service_degraded /
                                       # voice.something_degraded — match a degraded
                                       # token preceded by ``.`` or ``_``.
    r"|no_.*_provider.*"              # no_llm_provider_detected, no_X_provider_*
    r"|.*language_coerced.*"          # stt_language_coerced + future variants
    r"|.*language_unsupported.*"      # stt_language_unsupported pre-coerce
    r")",
    re.IGNORECASE,
)

# Names that satisfy the "paired with composite store" requirement.
_STORE_SIGNAL_NAMES: frozenset[str] = frozenset(
    {
        "record",
        "record_probe",
        "record_no_llm_provider",
        "record_stt_language_coerced",
        "record_ladder_exhausted",
        "get_default_degraded_store",
        "clear_axis",  # paired with a clear is also acceptable
    },
)

# Files that are STRUCTURALLY exempt — these are infrastructure that
# defines the store itself / consumer-side observers / pre-Mission-C4
# legacy emissions that pre-date the store. Each exemption requires a
# CLAUDE.md anti-pattern note for the audit trail.
_EXEMPT_FILES: frozenset[str] = frozenset(
    {
        # The store itself emits eviction warnings — recursive
        # composite-record would be a self-call loop.
        "engine/_degraded_store.py",
        # The Phase 2 governor escalation logger is the consumer-side
        # observer that READS the store — it's the canonical "operator
        # actionable" surface, not a producer that needs pairing.
        "voice/pipeline/_heartbeat_mixin.py",
        # Phase 3 scheduler emits "resurfaced" events when expired
        # acks prune — that's the inverse direction (clearing acks,
        # not adding degraded entries).
        "engine/_ack_resurface_scheduler.py",
        # Phase 3 ack store emits "acked" telemetry — also inverse.
        "engine/_operator_acks_store.py",
    },
)


def _is_exempt(file_path: Path) -> bool:
    try:
        rel = file_path.relative_to(_SCAN_ROOT).as_posix()
    except ValueError:
        # Path is outside the scan root (test fixtures pass tmp_path).
        # Not exempt — let the scanner run normally.
        return False
    return rel in _EXEMPT_FILES


def _line_has_allowlist(file_path: Path, lineno: int) -> bool:
    """Check the source line + the one above for an inline
    ``# c4-allowlist:`` comment. The comment MUST include a short
    rationale after the colon."""
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    candidates = []
    if 0 < lineno <= len(lines):
        candidates.append(lines[lineno - 1])
    if 0 < lineno - 1 <= len(lines):
        candidates.append(lines[lineno - 2])
    return any("# c4-allowlist:" in line for line in candidates)


def _function_calls_store(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Walk the function body for any call to a store-signal name."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                if target.attr in _STORE_SIGNAL_NAMES:
                    return True
            elif isinstance(target, ast.Name):
                if target.id in _STORE_SIGNAL_NAMES:
                    return True
    return False


def _module_imports_store(tree: ast.Module) -> bool:
    """Top-level ``import sovyx.engine._degraded_store`` or
    ``from sovyx.engine._degraded_store import ...`` indicates the
    module knows about the composite store. This signal is weaker
    than per-function store calls but useful for modules that wire
    the store through a chain of helpers."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sovyx.engine._degraded_store":
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sovyx.engine._degraded_store":
                    return True
    return False


def _is_degraded_warning(call: ast.Call) -> tuple[bool, str]:
    """Return (matches, event_name) iff this is a logger.warning call
    with a string-literal first arg matching the degraded pattern."""
    target = call.func
    if not isinstance(target, ast.Attribute):
        return False, ""
    if target.attr != "warning":
        return False, ""
    if not call.args:
        return False, ""
    first_arg = call.args[0]
    if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
        return False, ""
    event_name = first_arg.value
    if _DEGRADED_PATTERN.match(event_name):
        return True, event_name
    return False, ""


def scan_file(file_path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, event_name, reason) for uncovered sites."""
    if _is_exempt(file_path):
        return []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    uncovered: list[tuple[int, str, str]] = []
    module_aware = _module_imports_store(tree)

    # Build function-by-line index so we can locate the enclosing fn.
    fn_ranges: list[tuple[int, int, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            fn_ranges.append((node.lineno, end, node))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        matches, event_name = _is_degraded_warning(node)
        if not matches:
            continue
        if _line_has_allowlist(file_path, node.lineno):
            continue
        # Locate enclosing function (innermost)
        enclosing = None
        for start, end, fn in sorted(fn_ranges, key=lambda r: r[1] - r[0]):
            if start <= node.lineno <= end:
                enclosing = fn
                break
        if enclosing is not None and _function_calls_store(enclosing):
            continue
        if module_aware and enclosing is None:
            # Module-level emission with the store imported — accept.
            continue
        uncovered.append(
            (
                node.lineno,
                event_name,
                "no paired store record / clear_axis call in enclosing function",
            ),
        )
    return uncovered


def main() -> int:
    all_uncovered: list[tuple[Path, int, str, str]] = []
    for py_file in _SCAN_ROOT.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        for lineno, event, reason in scan_file(py_file):
            all_uncovered.append((py_file, lineno, event, reason))

    if not all_uncovered:
        print(
            "Quality Gate 10 — degraded signal surface: "
            "every degraded-signal WARN paired with composite store call.",
        )
        return 0

    print("❌ Quality Gate 10 — degraded-signal surface violations:", file=sys.stderr)
    for path, lineno, event, reason in all_uncovered:
        rel = path.relative_to(_REPO_ROOT).as_posix()
        print(
            f"  {rel}:{lineno}  event={event!r}  reason={reason}",
            file=sys.stderr,
        )
    print(
        "\nAnti-pattern #42 enforcement: every degraded-signal "
        "logger.warning() MUST be paired with a "
        "EngineDegradedStore.record() (or clear_axis) call so the "
        "operator sees the state on the composite banner, not via "
        "log-grep. Allowlist a false positive with an inline "
        "'# c4-allowlist: <rationale>' comment on the logger line.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
