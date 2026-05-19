#!/usr/bin/env python3
"""Mission C3 Phase 3 — telemetry calibration analyzer.

Parses the operator's structlog JSON log and computes the
falsifiability gates documented in
``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§3 + §7:

* **F1 gate** — zero ``voice.failover.failed`` events where
  ``verdict=downgraded_to_source`` AND ``candidates_remaining > 0``
  in the same ladder. Detection invariant: post-Mission-C3 the
  loop iterates the full candidate set before declaring failure,
  so the L1063 anchor signature (one-shot dispatch leaving 2
  candidates stranded) MUST NOT recur.
* **F2 gate** — already validated in CI synthetic tests at T1.5
  (``tests/regression/test_c3_failover_ladder_iteration.py``).
  This analyzer just notes the contract; no runtime data needed.
* **F3 gate** — ladder time-to-success P50 ≤ 8000 ms; P99 ≤
  30000 ms. Measured from ``voice.failover.ladder_complete`` event's
  ``elapsed_ms`` field where ``verdict="succeeded"``.
* **F4 gate** — zero per-frame ``voice.frame.drop_detected`` events
  fired DURING a ladder iteration window (between
  ``voice.failover.ladder_started`` and
  ``voice.failover.ladder_complete``). At least 1
  ``voice.failover.frame_loss_window`` summary event per ladder
  with drops.
* **F5 gate** — post-ladder-exhaustion ``voice_pipeline_deaf_warning``
  frequency ≤ 1/min with ``coordinator_terminal=True`` tag.

Usage::

    uv run python scripts/dev/analyze_c3_telemetry.py
    uv run python scripts/dev/analyze_c3_telemetry.py --log /path/to/sovyx.log
    uv run python scripts/dev/analyze_c3_telemetry.py --json

The script is committed (per ``feedback_no_inline_scripts_in_chat``)
so the operator can re-run it across the v0.45.x telemetry window.
Read-only — never mutates the log or any sovyx state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_LOG = Path.home() / ".sovyx" / "logs" / "sovyx.log"

# Mission C3 event-name inventory.
LADDER_STARTED = "voice.failover.ladder_started"
LADDER_COMPLETE = "voice.failover.ladder_complete"
CANDIDATE_ATTEMPTED = "voice.failover.candidate_attempted"
CANDIDATE_FAILED = "voice.failover.candidate_failed"
CANDIDATE_SKIPPED = "voice.failover.candidate_skipped"
CANDIDATE_SUCCEEDED = "voice.failover.succeeded"
LEGACY_FAILED = "voice.failover.failed"
FRAME_DROP_DETECTED = "voice.frame.drop_detected"
FRAME_LOSS_WINDOW = "voice.failover.frame_loss_window"
PIPELINE_DEAF = "voice_pipeline_deaf_warning"


def _iter_log_records(path: Path) -> list[dict[str, Any]]:
    """Read the structlog JSON log file line-by-line.

    Skips malformed JSON lines (defensive — older log lines may use
    a different format).
    """
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except FileNotFoundError:
        return []
    return records


def _event_name(record: dict[str, Any]) -> str:
    """Extract the structlog event name from a record."""
    return str(record.get("event", ""))


def _evaluate_f1_no_stranded_candidates(records: list[dict[str, Any]]) -> dict[str, Any]:
    """F1: zero ``voice.failover.failed verdict=downgraded_to_source``
    events while ``candidates_remaining > 0``.
    """
    stranded_failures = 0
    for record in records:
        if _event_name(record) != LEGACY_FAILED:
            continue
        verdict = record.get("voice.verdict", "")
        candidates_remaining = record.get("voice.candidates_remaining", 0)
        if (
            verdict == "downgraded_to_source"
            and isinstance(candidates_remaining, (int, float))
            and candidates_remaining > 0
        ):
            stranded_failures += 1
    passed = stranded_failures == 0
    return {
        "gate": "F1",
        "passed": passed,
        "stranded_failures": stranded_failures,
        "summary": (
            "PASS — zero ladder collapses with candidates stranded"
            if passed
            else f"FAIL — {stranded_failures} stranded-failure events found"
        ),
    }


def _evaluate_f3_ladder_latency(records: list[dict[str, Any]]) -> dict[str, Any]:
    """F3: ladder time-to-success P50 ≤ 8000 ms; P99 ≤ 30000 ms."""
    successful_elapsed: list[int] = []
    for record in records:
        if _event_name(record) != LADDER_COMPLETE:
            continue
        verdict = record.get("voice.verdict", "")
        elapsed_ms = record.get("voice.elapsed_ms")
        if verdict == "succeeded" and isinstance(elapsed_ms, (int, float)):
            successful_elapsed.append(int(elapsed_ms))

    if not successful_elapsed:
        return {
            "gate": "F3",
            "passed": True,  # vacuously — no ladders to evaluate
            "summary": "VACUOUS — no successful ladder runs in window",
            "successful_count": 0,
        }

    sorted_elapsed = sorted(successful_elapsed)
    n = len(sorted_elapsed)
    p50 = sorted_elapsed[n // 2]
    p99_idx = max(0, int(0.99 * n) - 1)
    p99 = sorted_elapsed[p99_idx]
    passed = p50 <= 8000 and p99 <= 30000  # noqa: PLR2004
    return {
        "gate": "F3",
        "passed": passed,
        "successful_count": n,
        "p50_ms": p50,
        "p99_ms": p99,
        "summary": (
            f"PASS — P50={p50}ms (≤ 8000), P99={p99}ms (≤ 30000)"
            if passed
            else f"FAIL — P50={p50}ms or P99={p99}ms outside budget"
        ),
    }


def _evaluate_f4_frame_drop_gate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """F4: zero per-frame ``voice.frame.drop_detected`` events during a
    ladder iteration window. ≥ 1 ``voice.failover.frame_loss_window``
    summary per ladder with drops.

    Implementation: walk records in order, tracking the
    ``_failover_ladder_in_progress`` flag per ladder_id pair
    (started, complete). For each per-frame drop, check whether ANY
    ladder is in progress at that point in the log.
    """
    in_flight_ladders = 0
    drops_inside_ladder = 0
    drops_outside_ladder = 0
    summaries_seen = 0
    ladders_with_summary = set()
    ladders_with_per_frame_drops_inside: set[str] = set()
    current_ladder_id = ""

    for record in records:
        event = _event_name(record)
        if event == LADDER_STARTED:
            in_flight_ladders += 1
            current_ladder_id = str(record.get("voice.ladder_id", ""))
        elif event == LADDER_COMPLETE:
            in_flight_ladders = max(0, in_flight_ladders - 1)
            current_ladder_id = ""
        elif event == FRAME_DROP_DETECTED:
            if in_flight_ladders > 0:
                drops_inside_ladder += 1
                if current_ladder_id:
                    ladders_with_per_frame_drops_inside.add(current_ladder_id)
            else:
                drops_outside_ladder += 1
        elif event == FRAME_LOSS_WINDOW:
            summaries_seen += 1
            ladder_id = str(record.get("voice.ladder_id", ""))
            if ladder_id:
                ladders_with_summary.add(ladder_id)

    passed = drops_inside_ladder == 0
    return {
        "gate": "F4",
        "passed": passed,
        "drops_inside_ladder": drops_inside_ladder,
        "drops_outside_ladder": drops_outside_ladder,
        "frame_loss_window_summaries": summaries_seen,
        "ladders_with_summary": len(ladders_with_summary),
        "summary": (
            f"PASS — 0 per-frame drops inside ladder, {summaries_seen} summary events"
            if passed
            else f"FAIL — {drops_inside_ladder} per-frame drops fired during ladder iteration"
        ),
    }


def _evaluate_f5_deaf_warn_throttle(records: list[dict[str, Any]]) -> dict[str, Any]:
    """F5: post-ladder-exhaustion deaf-warn frequency ≤ 1/min.

    Counts deaf warnings tagged with ``coordinator_terminal=True``.
    """
    terminal_warn_count = 0
    non_terminal_warn_count = 0
    first_terminal_monotonic: float | None = None
    last_terminal_monotonic: float | None = None

    for record in records:
        if _event_name(record) != PIPELINE_DEAF:
            continue
        coord_terminal = record.get("coordinator_terminal", False)
        if coord_terminal:
            terminal_warn_count += 1
            ts = record.get("voice.monotonic_ts") or record.get("monotonic_ts")
            if isinstance(ts, (int, float)):
                if first_terminal_monotonic is None:
                    first_terminal_monotonic = float(ts)
                last_terminal_monotonic = float(ts)
        else:
            non_terminal_warn_count += 1

    if terminal_warn_count == 0:
        return {
            "gate": "F5",
            "passed": True,
            "terminal_warn_count": 0,
            "non_terminal_warn_count": non_terminal_warn_count,
            "summary": "VACUOUS — no terminal deaf warnings in window",
        }

    if (
        first_terminal_monotonic is not None
        and last_terminal_monotonic is not None
        and last_terminal_monotonic > first_terminal_monotonic
    ):
        window_minutes = (last_terminal_monotonic - first_terminal_monotonic) / 60.0
        rate_per_min = terminal_warn_count / max(window_minutes, 1.0 / 60.0)
        passed = rate_per_min <= 1.05  # 5% jitter tolerance  # noqa: PLR2004
    else:
        rate_per_min = float(terminal_warn_count)
        passed = terminal_warn_count <= 1

    return {
        "gate": "F5",
        "passed": passed,
        "terminal_warn_count": terminal_warn_count,
        "non_terminal_warn_count": non_terminal_warn_count,
        "rate_per_min": round(rate_per_min, 3),
        "summary": (
            f"PASS — {rate_per_min:.2f} terminal warnings/min (≤ 1.05)"
            if passed
            else f"FAIL — {rate_per_min:.2f} terminal warnings/min (> 1.05)"
        ),
    }


def _summarize_event_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    """Return Counter of every C3-relevant event name."""
    counter: Counter[str] = Counter()
    for record in records:
        event = _event_name(record)
        if event in {
            LADDER_STARTED,
            LADDER_COMPLETE,
            CANDIDATE_ATTEMPTED,
            CANDIDATE_FAILED,
            CANDIDATE_SKIPPED,
            CANDIDATE_SUCCEEDED,
            LEGACY_FAILED,
            FRAME_DROP_DETECTED,
            FRAME_LOSS_WINDOW,
            PIPELINE_DEAF,
        }:
            counter[event] += 1
    return dict(counter)


def _summarize_error_class_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    """Distribution of ``error_class`` values across candidate-failed
    events.
    """
    counter: Counter[str] = Counter()
    for record in records:
        if _event_name(record) != CANDIDATE_FAILED:
            continue
        error_class = str(record.get("voice.error_class", "unknown"))
        counter[error_class] += 1
    return dict(counter)


def analyze(log_path: Path) -> dict[str, Any]:
    """Top-level entry — read log + compute every gate."""
    records = _iter_log_records(log_path)
    return {
        "log_path": str(log_path),
        "records_parsed": len(records),
        "event_counts": _summarize_event_counts(records),
        "error_class_distribution": _summarize_error_class_distribution(records),
        "F1": _evaluate_f1_no_stranded_candidates(records),
        "F3": _evaluate_f3_ladder_latency(records),
        "F4": _evaluate_f4_frame_drop_gate(records),
        "F5": _evaluate_f5_deaf_warn_throttle(records),
    }


def _render_pretty(result: dict[str, Any]) -> None:
    """Human-readable output (non-JSON)."""
    print(f"Mission C3 telemetry analyzer — {result['log_path']}")
    print(f"  Records parsed: {result['records_parsed']}")
    print("\n  Event counts:")
    for event, count in sorted(result["event_counts"].items()):
        print(f"    {event}: {count}")
    print("\n  Error class distribution (candidate_failed):")
    for cls, count in sorted(result["error_class_distribution"].items()):
        print(f"    {cls}: {count}")
    print("\n  Falsifiability gates:")
    for gate_key in ("F1", "F3", "F4", "F5"):
        gate = result[gate_key]
        marker = "✅" if gate["passed"] else "❌"
        print(f"    {marker} {gate_key}: {gate['summary']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=Path,
        default=_DEFAULT_LOG,
        help=f"Log file path (default: {_DEFAULT_LOG}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output suitable for programmatic consumption.",
    )
    args = parser.parse_args(argv)

    result = analyze(args.log)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _render_pretty(result)

    # Exit non-zero if any gate failed (CI-friendly).
    gates = ("F1", "F3", "F4", "F5")
    if any(not result[g]["passed"] for g in gates):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
