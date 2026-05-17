"""Unit tests for Quality Gate 10 — degraded signal surface checker.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 5 §T5.1 — anti-pattern #42 enforcement.

The checker AST-scans for ``logger.warning(...)`` whose event name
matches the operator-actionable degraded patterns; uncovered sites
(no paired EngineDegradedStore call in the enclosing function) cause
the gate to fail. Tests exercise both the positive path (paired call
present) + the negative path (uncovered).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from scripts.dev.check_degraded_signal_surface import (
    _DEGRADED_PATTERN,
    _is_degraded_warning,
    _line_has_allowlist,
    scan_file,
)


class TestDegradedPattern:
    @pytest.mark.parametrize(
        "event_name,should_match",
        [
            # Mission C4 canonical patterns (should match)
            ("voice.windows.audio_service_degraded", True),
            ("no_llm_provider_detected", True),
            ("voice.factory.stt_language_unsupported", True),
            ("voice.factory.stt_language_coerced", True),
            # Pure platform-feature gates (must NOT match — too noisy)
            ("voice_wyoming_unavailable", False),
            ("voice_audio_service_monitor_unavailable", False),
            ("voice_apo_linux_pactl_daemon_unavailable", False),
            # Unrelated WARNs (must NOT match)
            ("voice_pipeline_deaf_warning", False),
            ("voice.failover.attempted", False),
        ],
    )
    def test_pattern_matches(self, event_name: str, should_match: bool) -> None:
        result = bool(_DEGRADED_PATTERN.match(event_name))
        assert result is should_match, (
            f"Pattern match for {event_name!r}: got {result}, expected {should_match}"
        )


class TestIsDegradedWarning:
    def test_logger_warning_string_arg_matches(self) -> None:
        import ast

        source = dedent(
            """
            logger.warning("no_llm_provider_detected")
            """,
        ).strip()
        tree = ast.parse(source)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        matches, name = _is_degraded_warning(call)
        assert matches is True
        assert name == "no_llm_provider_detected"

    def test_logger_info_does_not_match(self) -> None:
        import ast

        source = dedent(
            """
            logger.info("no_llm_provider_detected")
            """,
        ).strip()
        tree = ast.parse(source)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        matches, _name = _is_degraded_warning(call)
        assert matches is False

    def test_no_args_no_match(self) -> None:
        import ast

        source = "logger.warning()"
        tree = ast.parse(source)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        matches, _ = _is_degraded_warning(call)
        assert matches is False


class TestAllowlistComment:
    def test_inline_allowlist_recognized(self, tmp_path: Path) -> None:
        file_path = tmp_path / "fixture.py"
        file_path.write_text(
            dedent(
                """
                logger.warning("voice.something_degraded")  # c4-allowlist: pre-Mission-C4 legacy
                """,
            ).strip(),
            encoding="utf-8",
        )
        assert _line_has_allowlist(file_path, 1) is True

    def test_no_allowlist_returns_false(self, tmp_path: Path) -> None:
        file_path = tmp_path / "fixture.py"
        file_path.write_text(
            'logger.warning("voice.something_degraded")\n',
            encoding="utf-8",
        )
        assert _line_has_allowlist(file_path, 1) is False


class TestScanFileE2E:
    def test_unpaired_warn_is_flagged(self, tmp_path: Path) -> None:
        # Use a file path under tests/ so the scanner's exemption list
        # doesn't apply. We invoke scan_file directly.
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_warning(logger):
                    logger.warning("voice.something_degraded")
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert len(violations) == 1
        lineno, event, _reason = violations[0]
        assert event == "voice.something_degraded"
        assert lineno == 2

    def test_paired_warn_passes(self, tmp_path: Path) -> None:
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_warning_with_store(logger, store):
                    logger.warning("voice.something_degraded")
                    store.record(entry)
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert violations == []

    def test_allowlist_suppresses_violation(self, tmp_path: Path) -> None:
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_warning(logger):
                    logger.warning("voice.something_degraded")  # c4-allowlist: test
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert violations == []

    def test_get_default_store_call_also_satisfies(self, tmp_path: Path) -> None:
        """A function that resolves the singleton via
        ``get_default_degraded_store()`` (even without a direct
        ``.record(...)`` in the same fn) satisfies the check —
        operator's helper may delegate to a sibling that records."""
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_warning(logger):
                    logger.warning("voice.something_degraded")
                    store = get_default_degraded_store()
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert violations == []

    def test_clear_axis_also_satisfies(self, tmp_path: Path) -> None:
        """A function that CLEARS the axis (resolving a degraded state)
        is also a paired surface — the operator sees the state through
        the clear path."""
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_recovered(logger, store):
                    logger.warning("voice.something_degraded")
                    store.clear_axis("voice")
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert violations == []

    def test_dynamic_event_name_not_flagged(self, tmp_path: Path) -> None:
        """The checker only inspects string-literal event names. A
        dynamically-constructed event (variable, f-string) is not
        flagged — those are out-of-band for AST analysis."""
        file_path = tmp_path / "module_under_test.py"
        file_path.write_text(
            dedent(
                """
                def emit_dynamic(logger, event_name):
                    logger.warning(event_name)
                """,
            ).strip(),
            encoding="utf-8",
        )
        violations = scan_file(file_path)
        assert violations == []
