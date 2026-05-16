"""Tests for ``scripts/dev/analyze_c3_telemetry.py``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.12.

Pin the F1/F3/F4/F5 gate evaluators on synthetic log fixtures.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# Import the script as a module (it's not a package).
_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "dev" / "analyze_c3_telemetry.py"
_spec = importlib.util.spec_from_file_location("analyze_c3_telemetry", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
analyzer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyzer)


def _write_log(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    log = tmp_path / "sovyx.log"
    with log.open("w", encoding="utf-8") as fp:
        for r in records:
            fp.write(json.dumps(r) + "\n")
    return log


class TestF1NoStrandedCandidates:
    """F1 — zero ``voice.failover.failed verdict=downgraded_to_source``
    while ``candidates_remaining > 0``.
    """

    def test_passes_on_empty_log(self, tmp_path: Path) -> None:
        log = _write_log(tmp_path, [])
        result = analyzer.analyze(log)
        assert result["F1"]["passed"] is True
        assert result["F1"]["stranded_failures"] == 0

    def test_passes_when_legacy_failed_has_zero_remaining(self, tmp_path: Path) -> None:
        records = [
            {
                "event": "voice.failover.failed",
                "voice.verdict": "downgraded_to_source",
                "voice.candidates_remaining": 0,
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F1"]["passed"] is True

    def test_fails_when_one_stranded_failure_detected(self, tmp_path: Path) -> None:
        """Pre-Mission-C3 regression: one ladder collapsed with 2
        candidates_remaining.
        """
        records = [
            {
                "event": "voice.failover.failed",
                "voice.verdict": "downgraded_to_source",
                "voice.candidates_remaining": 2,
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F1"]["passed"] is False
        assert result["F1"]["stranded_failures"] == 1


class TestF3LadderLatency:
    """F3 — successful ladder time-to-success P50/P99 budget."""

    def test_vacuous_pass_when_no_succeeded_ladders(self, tmp_path: Path) -> None:
        log = _write_log(tmp_path, [])
        result = analyzer.analyze(log)
        assert result["F3"]["passed"] is True
        assert result["F3"]["successful_count"] == 0

    def test_passes_when_all_under_budget(self, tmp_path: Path) -> None:
        records = [
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 500,
            },
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 1500,
            },
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 3000,
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F3"]["passed"] is True
        assert result["F3"]["successful_count"] == 3

    def test_fails_when_p50_exceeds_budget(self, tmp_path: Path) -> None:
        records = [
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 10000,
            },
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 15000,
            },
            {
                "event": "voice.failover.ladder_complete",
                "voice.verdict": "succeeded",
                "voice.elapsed_ms": 20000,
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F3"]["passed"] is False


class TestF4FrameDropGate:
    """F4 — zero per-frame drops during a ladder iteration window."""

    def test_passes_when_drop_outside_ladder_window(self, tmp_path: Path) -> None:
        """A per-frame drop fired BEFORE any ladder started — F4 still
        passes (no drops inside the gating window).
        """
        records = [
            {"event": "voice.frame.drop_detected", "voice.gap_ms": 100.0},
            {"event": "voice.failover.ladder_started", "voice.ladder_id": "id1"},
            {"event": "voice.failover.ladder_complete", "voice.ladder_id": "id1"},
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F4"]["passed"] is True
        assert result["F4"]["drops_outside_ladder"] == 1
        assert result["F4"]["drops_inside_ladder"] == 0

    def test_fails_when_drop_inside_ladder_window(self, tmp_path: Path) -> None:
        """Per-frame drop fired INSIDE the ladder — F4 fails (Mission
        §T2.5 gating should have caught this).
        """
        records = [
            {"event": "voice.failover.ladder_started", "voice.ladder_id": "id1"},
            {"event": "voice.frame.drop_detected", "voice.gap_ms": 100.0},
            {"event": "voice.failover.ladder_complete", "voice.ladder_id": "id1"},
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F4"]["passed"] is False
        assert result["F4"]["drops_inside_ladder"] == 1


class TestF5DeafWarnThrottle:
    """F5 — post-exhaustion deaf-warn throttle rate."""

    def test_vacuous_pass_with_zero_terminal_warns(self, tmp_path: Path) -> None:
        log = _write_log(tmp_path, [])
        result = analyzer.analyze(log)
        assert result["F5"]["passed"] is True
        assert result["F5"]["terminal_warn_count"] == 0

    def test_passes_when_terminal_warn_rate_within_budget(self, tmp_path: Path) -> None:
        """One terminal warn — within the 1/min budget."""
        records = [
            {
                "event": "voice_pipeline_deaf_warning",
                "coordinator_terminal": True,
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["F5"]["passed"] is True


class TestSummaries:
    def test_event_counts_populated(self, tmp_path: Path) -> None:
        records = [
            {"event": "voice.failover.ladder_started"},
            {"event": "voice.failover.ladder_started"},
            {"event": "voice.failover.candidate_attempted"},
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["event_counts"]["voice.failover.ladder_started"] == 2
        assert result["event_counts"]["voice.failover.candidate_attempted"] == 1

    def test_error_class_distribution_populated(self, tmp_path: Path) -> None:
        records = [
            {
                "event": "voice.failover.candidate_failed",
                "voice.error_class": "unopenable_this_boot",
            },
            {
                "event": "voice.failover.candidate_failed",
                "voice.error_class": "unopenable_this_boot",
            },
            {
                "event": "voice.failover.candidate_failed",
                "voice.error_class": "transient_retryable_same_device",
            },
        ]
        log = _write_log(tmp_path, records)
        result = analyzer.analyze(log)
        assert result["error_class_distribution"]["unopenable_this_boot"] == 2
        assert result["error_class_distribution"]["transient_retryable_same_device"] == 1


class TestMainCLI:
    def test_exit_code_zero_when_all_gates_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = _write_log(tmp_path, [])
        exit_code = analyzer.main(["--log", str(log)])
        assert exit_code == 0

    def test_exit_code_nonzero_on_f1_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = [
            {
                "event": "voice.failover.failed",
                "voice.verdict": "downgraded_to_source",
                "voice.candidates_remaining": 2,
            },
        ]
        log = _write_log(tmp_path, records)
        exit_code = analyzer.main(["--log", str(log)])
        assert exit_code == 1

    def test_json_output_mode(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        log = _write_log(tmp_path, [])
        analyzer.main(["--log", str(log), "--json"])
        captured = capsys.readouterr()
        # Should be parseable JSON.
        parsed = json.loads(captured.out)
        assert "F1" in parsed
        assert "F3" in parsed
        assert "F4" in parsed
        assert "F5" in parsed
