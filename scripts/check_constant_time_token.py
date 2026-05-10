"""CI gate — every WS-token compare under ``dashboard/routes/`` must be constant-time.

WebSocket auth in this codebase reads the session token from query params
and compares it against ``websocket.app.state.auth_token``. A naive
``==`` / ``!=`` comparison is variable-time at the byte level — the C
implementation behind ``str.__eq__`` short-circuits at the first
mismatching character, exposing a network-adjacent timing side-channel
that lets an attacker recover the token byte-by-byte.

The canonical fix is :func:`secrets.compare_digest`, documented as
constant-time for credential comparison. The pattern is already
established in:

  * ``dashboard/server.py:444`` (HTTP basic auth)
  * ``dashboard/routes/_deps.py:39`` (HTTP bearer)
  * ``dashboard/routes/voice_test.py:339`` (WS query token)
  * ``dashboard/routes/websocket.py:27`` (WS query token)

This gate enforces the rule structurally so a fourth drift cannot land
silently. It scans every ``.py`` under ``src/sovyx/dashboard/routes/`` for
``Compare`` AST nodes whose left or right side is one of the canonical
token names (``token``, ``auth_token``, ``expected_token``, ``provided``,
``expected``) used with ``Eq`` / ``NotEq``. Hits print as actionable
``file:line`` violations.

Allowed escape hatch — append ``# noqa: const-time-cmp`` to a comparison
line if the value is genuinely not a credential (e.g. comparing a token
*kind* string against a literal). Use sparingly.

Wired into ``.github/workflows/ci.yml`` as the ``constant-time-token-gate``
job after ``exception-chain-gate``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

_TOKEN_NAMES = frozenset(
    {
        "token",
        "auth_token",
        "expected_token",
        "provided",
        "expected",
    },
)
_NOQA_TOKEN = "# noqa: const-time-cmp"


class _TokenCompareVisitor(ast.NodeVisitor):
    """Collect every ``token == X`` / ``token != X`` (or transposed) compare."""

    def __init__(self, source_lines: list[str]) -> None:
        self._source_lines = source_lines
        self.violations: list[tuple[int, str]] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        # Python chained-compare layout: ``a OP0 b OP1 c`` exposes
        # ops=[OP0, OP1] + comparators=[b, c] + left=a. Operator i
        # therefore relates operands[i] with operands[i + 1].
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            left = operands[i]
            right = operands[i + 1]
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if not (self._is_token_name(left) or self._is_token_name(right)):
                continue
            line_idx = node.lineno - 1
            if 0 <= line_idx < len(self._source_lines):
                line = self._source_lines[line_idx]
                if _NOQA_TOKEN in line:
                    continue
                self.violations.append((node.lineno, line.strip()))
            else:
                self.violations.append((node.lineno, "<line out of range>"))
        self.generic_visit(node)

    @staticmethod
    def _is_token_name(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id in _TOKEN_NAMES


def _scan_file(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    visitor = _TokenCompareVisitor(source.splitlines())
    visitor.visit(tree)
    return visitor.violations


def _iter_source_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("src/sovyx/dashboard/routes"),
        help="Routes tree to scan (default: src/sovyx/dashboard/routes)",
    )
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2

    total_files = 0
    total_violations = 0
    for file in _iter_source_files(args.root):
        total_files += 1
        for line_no, snippet in _scan_file(file):
            total_violations += 1
            print(
                f"{file}:{line_no}: variable-time token compare: {snippet}",
                file=sys.stderr,
            )

    if total_violations:
        print(
            f"\nFAIL: {total_violations} variable-time token compare(s) across {total_files} files.",
            file=sys.stderr,
        )
        print(
            "  Fix: replace the compare with secrets.compare_digest(a, b)",
            file=sys.stderr,
        )
        print(
            "  (handle None operands explicitly — compare_digest only takes str/bytes).",
            file=sys.stderr,
        )
        print(
            f"  Genuine non-credential compares can use the '{_NOQA_TOKEN}' inline escape.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {total_files} dashboard route files clean - every token compare is constant-time.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
