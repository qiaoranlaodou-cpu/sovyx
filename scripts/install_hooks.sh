#!/usr/bin/env bash
# scripts/install_hooks.sh — one-time setup for the verify_gates pre-push hook.
#
# Configures git to use the tracked .githooks/ directory instead of
# .git/hooks/ (which is not tracked). After this runs once per clone,
# `git push` automatically invokes .githooks/pre-push, which rejects
# the push unless `./scripts/verify_gates.sh` has produced a recent
# clean proof for the current HEAD.
#
# Codified by:
#   MISSION-post-v0_42_2-quality-discipline-2026-05-14.md Phase 2.T2.3
#
# Usage:
#   ./scripts/install_hooks.sh

set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

# Color (only when TTY).
if [[ -t 1 ]]; then
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    RESET=$(printf '\033[0m')
else
    GREEN=""; YELLOW=""; RESET=""
fi

if [[ ! -d ".githooks" ]]; then
    echo "error: .githooks/ not found in $REPO_ROOT" >&2
    exit 1
fi

# Ensure hook scripts are executable. On Windows, chmod is mostly
# decorative but harmless; on POSIX it's load-bearing.
chmod +x .githooks/* 2>/dev/null || true

# Point git at the tracked hooks dir.
git config core.hooksPath .githooks

CURRENT=$(git config core.hooksPath)
printf '%s✓%s hooks installed: git core.hooksPath = %s\n' "$GREEN" "$RESET" "$CURRENT"

# Sanity: list what's installed.
printf '\n%sActive hooks:%s\n' "$YELLOW" "$RESET"
ls -1 .githooks/ | while read -r h; do
    [[ "$h" == "README.md" ]] && continue
    printf '  - %s\n' "$h"
done

cat <<EOF

${GREEN}Done.${RESET} From now on, every \`git push\` to this repo will:
  1. Read .git/.last-gates-pass marker
  2. Verify HEAD SHA matches + marker is < 30 min old
  3. Reject the push if either check fails

To produce the marker, run:
  ${YELLOW}./scripts/verify_gates.sh${RESET}

The script writes the marker on green-via-summary-line verdict (not
on harness exit code, which is unreliable per feedback_ci_preflight
Addendum 2026-05-14).

Emergency bypass: \`git push --no-verify\` (CLAUDE.md proibits this
except with explicit operator approval + commit-body rationale).
EOF
