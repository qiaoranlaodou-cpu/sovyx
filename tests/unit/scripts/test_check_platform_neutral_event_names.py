"""Unit tests for the Quality Gate 13 AST scanner (Mission H2 §T1.6).

Verifies the platform-token regex, the four escape hatches, the JSON
report shape, and the LENIENT/STRICT exit-code branching.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from scripts.dev.check_platform_neutral_event_names import (
    _PLATFORM_DISAMBIGUATING_RE,
    _PLATFORM_SUFFIX_RE,
    _PLATFORM_TOKEN_RE,
    GateReport,
    main,
    run_check,
)


class TestPlatformTokenRegex:
    """The platform-token regex catches the H2 audio-stack vocabulary."""

    @pytest.mark.parametrize(
        "literal,token",
        [
            ("audio.apo.bypassed", "apo"),
            ("voice_apo_bypass_activated", "apo"),
            ("voice.wasapi.error", "wasapi"),
            ("audio.dsound.failed", "dsound"),
            ("voice.pulseaudio.module_missing", "pulseaudio"),
            ("voice.pipewire.filter_chain_failed", "pipewire"),
            ("voice.coreaudio.au_failed", "coreaudio"),
            ("voice.wmme.timeout", "wmme"),
            ("voice.directshow.error", "directshow"),
            ("voice.voice_clarity.detected", "voice_clarity"),
            ("voice.module_echo_cancel.disabled", "module_echo_cancel"),
            ("voice.voice_isolation.enabled", "voice_isolation"),
        ],
    )
    def test_matches_platform_tokens(self, literal: str, token: str) -> None:
        match = _PLATFORM_TOKEN_RE.search(literal)
        assert match is not None
        assert match.group(1) == token

    @pytest.mark.parametrize(
        "literal",
        [
            "voice.capture_integrity.bypassed",  # neutral
            "voice.deaf.recovery_attempted",
            "voice.coordinator.dispatch_acknowledged",
            "voice.failover.candidate_attempted",
            "llm.discovery.report",
            "cognitive.loop.started",
        ],
    )
    def test_does_not_match_neutral_names(self, literal: str) -> None:
        assert _PLATFORM_TOKEN_RE.search(literal) is None


class TestPlatformSuffixRegex:
    """The platform-suffix regex permits canonical platform-suffixed names."""

    @pytest.mark.parametrize(
        "literal",
        [
            "audio.capture_chain.scan.linux",
            "audio.capture_chain.scan.windows",
            "audio.capture_chain.scan.darwin",
            "voice.something.darwin",
        ],
    )
    def test_matches_platform_suffix(self, literal: str) -> None:
        assert _PLATFORM_SUFFIX_RE.search(literal) is not None

    @pytest.mark.parametrize(
        "literal",
        [
            "audio.apo.scan",  # no platform suffix
            "voice.linux.scan",  # platform in MIDDLE not suffix
            "audio.linux_extra",  # not a clean suffix
        ],
    )
    def test_does_not_match_non_suffix(self, literal: str) -> None:
        assert _PLATFORM_SUFFIX_RE.search(literal) is None


class TestPlatformDisambiguatingRegex:
    """Names with explicit platform tokens are self-disambiguating."""

    @pytest.mark.parametrize(
        "literal",
        [
            "voice.windows.apo_registry_failed",
            "voice_apo_linux_pactl_failed",
            "audio.darwin.coreaudio_failed",
            "voice.win32.foo",
        ],
    )
    def test_recognises_explicit_platform_anywhere(self, literal: str) -> None:
        assert _PLATFORM_DISAMBIGUATING_RE.search(literal) is not None


class TestScannerOnSyntheticFiles:
    """End-to-end scanner behaviour on synthetic .py inputs."""

    def _write_module(self, root: Path, name: str, body: str) -> Path:
        """Write a temporary module under ``root/<name>.py`` and return path."""
        path = root / name
        path.write_text(dedent(body), encoding="utf-8")
        return path

    def test_clean_module_passes(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "clean.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.error("voice.capture_integrity.bypassed")
            logger.warning("voice.deaf.recovery_attempted")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed
        assert report.calls_inspected == 2

    def test_unguarded_platform_token_violates(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "violating.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.error("audio.apo.bypassed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert not report.passed
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.literal == "audio.apo.bypassed"
        assert v.token_matched == "apo"
        assert v.method == "error"

    def test_sys_platform_gate_exempts(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "gated.py",
            """
            import sys
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            if sys.platform == "win32":
                logger.error("audio.apo.bypassed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed

    def test_platform_suffix_exempts(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "suffixed.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.info("audio.apo.scan.linux")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        # The suffix .linux is platform-disambiguating → exempt
        assert report.passed

    def test_inline_allowlist_comment_exempts(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "allowlisted.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            # h2-allowlist: testing the escape hatch
            logger.error("audio.apo.bypassed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed

    def test_same_line_allowlist_comment_exempts(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "samelinealw.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.error("audio.apo.bypassed")  # h2-allowlist: on the same line
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed

    def test_explicit_platform_in_name_exempts(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "platformprefixed.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.warning("voice.windows.apo_registry_failed")
            logger.warning("voice_apo_linux_pactl_failed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed

    def test_event_keyword_arg_detected(self, tmp_path: Path) -> None:
        self._write_module(
            tmp_path,
            "kwarg.py",
            """
            from sovyx.observability.logging import get_logger
            logger = get_logger(__name__)
            logger.error(event="audio.apo.bypassed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert not report.passed

    def test_non_logger_call_ignored(self, tmp_path: Path) -> None:
        """Calls on non-logger receivers are ignored."""
        self._write_module(
            tmp_path,
            "notlogger.py",
            """
            class Foo:
                def info(self, msg: str) -> None: ...
            f = Foo()
            f.info("audio.apo.bypassed")
            """,
        )
        report = run_check(tmp_path, repo_root=tmp_path)
        assert report.passed


class TestGateReportShape:
    """The JSON report shape matches the analyzer expectations."""

    def test_to_dict_contains_required_keys(self) -> None:
        report = GateReport(files_scanned=10, calls_inspected=100)
        data = report.to_dict()
        for key in (
            "files_scanned",
            "calls_inspected",
            "passed",
            "violation_count",
            "violations",
            "skipped_files",
        ):
            assert key in data

    def test_passed_property_inverts_with_violations(self) -> None:
        from scripts.dev.check_platform_neutral_event_names import Violation

        report = GateReport()
        assert report.passed
        report.violations.append(
            Violation(
                file_path="x.py",
                line=1,
                column=0,
                method="error",
                literal="audio.apo.bypassed",
                token_matched="apo",
            )
        )
        assert not report.passed


class TestMainCLI:
    """The argparse main() honours --strict and --json flags."""

    def test_main_lenient_exits_zero_on_violations(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "bad.py").write_text(
            dedent(
                """
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)
                logger.error("audio.apo.bypassed")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path)])
        assert exit_code == 0  # LENIENT — report-only
        captured = capsys.readouterr()
        assert "violation" in captured.out

    def test_main_strict_exits_nonzero_on_violations(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "bad.py").write_text(
            dedent(
                """
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)
                logger.error("audio.apo.bypassed")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 1

    def test_main_strict_clean_exits_zero(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "clean.py").write_text(
            dedent(
                """
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)
                logger.info("voice.capture_integrity.bypassed")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        exit_code = main(["--scan-root", str(tmp_path), "--repo-root", str(tmp_path), "--strict"])
        assert exit_code == 0
