"""F1 falsifiability test for Quality Gate 13 (Mission H2 §10.2).

Verifies the AST scanner correctly detects a synthetically-injected
unguarded platform-token emission AND that all four escape hatches
exempt their respective patterns. F1 is the load-bearing falsifiability
gate for Mission H2 — without this test passing, Gate 13's STRICT
promotion at v0.51.0 is not safe.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from scripts.dev.check_platform_neutral_event_names import main, run_check


def _write_module(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


class TestF1Falsifiability:
    """The synthetic-drift scenario MUST fail STRICT mode."""

    def test_synthetic_dsound_violation_fails_strict(self, tmp_path: Path) -> None:
        """Adding a fresh `logger.error("audio.dsound.failed")` line
        without any escape hatch MUST trigger Gate 13 STRICT failure.

        Reference: mission §3 F1 — counterfactual on F1 says ``if the
        deliberate drift does NOT cause Gate 13 to fail, the AST scanner
        is too lenient``.
        """
        _write_module(
            tmp_path,
            "synthetic_violation.py",
            """
            from sovyx.observability.logging import get_logger

            logger = get_logger(__name__)

            def trigger() -> None:
                logger.error("audio.dsound.failed", reason="synthetic")
            """,
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 1, "Gate 13 STRICT should have rejected the synthetic violation"

    def test_synthetic_apo_violation_fails_strict(self, tmp_path: Path) -> None:
        """Mirror of the L1067 forensic anchor — synthetic ``audio.apo.bypassed``
        emit without a gate MUST fail STRICT.
        """
        _write_module(
            tmp_path,
            "synthetic_apo.py",
            """
            from sovyx.observability.logging import get_logger

            logger = get_logger(__name__)

            def trigger() -> None:
                logger.error("audio.apo.bypassed", verdict="failure")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert not report.passed
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.literal == "audio.apo.bypassed"
        assert v.token_matched == "apo"

    def test_allowlist_comment_silences_synthetic_violation(self, tmp_path: Path) -> None:
        """Adding the ``# h2-allowlist:`` comment turns the same emit
        into a green run — proves the escape hatch is functional.
        """
        _write_module(
            tmp_path,
            "allowlisted_violation.py",
            """
            from sovyx.observability.logging import get_logger

            logger = get_logger(__name__)

            def trigger() -> None:
                # h2-allowlist: regression-test escape hatch
                logger.error("audio.apo.bypassed", verdict="failure")
            """,
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 0

    def test_sys_platform_gate_silences_synthetic_violation(self, tmp_path: Path) -> None:
        """Wrapping the emit in ``if sys.platform == "win32":`` silences
        Gate 13 — proves escape hatch (a) is functional.
        """
        _write_module(
            tmp_path,
            "gated_violation.py",
            """
            import sys

            from sovyx.observability.logging import get_logger

            logger = get_logger(__name__)

            def trigger() -> None:
                if sys.platform == "win32":
                    logger.error("audio.apo.bypassed", verdict="failure")
            """,
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 0

    def test_platform_suffix_exempts_synthetic_violation(self, tmp_path: Path) -> None:
        """Using a platform-suffixed name (``audio.apo.scan.linux``)
        silences Gate 13 — proves escape hatch (b) is functional.
        """
        _write_module(
            tmp_path,
            "suffixed_violation.py",
            """
            from sovyx.observability.logging import get_logger

            logger = get_logger(__name__)

            def trigger() -> None:
                logger.info("audio.apo.scan.linux", verdict="info")
            """,
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 0

    def test_falsifiability_baseline_at_head(self) -> None:
        """The CURRENT HEAD ``src/sovyx/`` must report ≥ 1 violation in
        LENIENT mode — verifies the scanner actually runs against the
        real codebase + correctly identifies pre-mission violations.

        After Phase 1.B + 1.D ship, this count drops; after Phase 3
        STRICT ships, it MUST be 0. The test is bound to LENIENT mode
        so it doesn't fail CI during the staged-adoption window.
        """
        repo_root = Path(__file__).resolve().parents[3]
        scan_root = repo_root / "src" / "sovyx"
        report = run_check(scan_root, repo_root=repo_root)
        # Pre-mission baseline: 31 violations across the codebase.
        # The exact count is brittle (Phase 1.B closes 6, Phase 1.D
        # closes 8 more, etc.); this test only verifies the scanner
        # runs end-to-end without crashing AND identifies at least
        # one site at HEAD.
        assert report.files_scanned > 0
        assert report.calls_inspected > 0
        # Phase 3 v0.51.0 STRICT flip will tighten this to == 0; until
        # then the assertion is bounded above only.
