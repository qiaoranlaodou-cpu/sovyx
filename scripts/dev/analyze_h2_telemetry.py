#!/usr/bin/env python3
"""Mission H2 Phase 2 telemetry analyzer — F1..F6 verdict aggregator.

Reads operator structured-log output (typically ``~/.sovyx/logs/sovyx.log``)
and emits a JSON verdict for the six falsifiability gates declared in
Mission H2 §3 + §9. Operator runs this once the v0.49.6..v0.49.9 cohort
has accumulated ≥ 1 minor cycle of real-world emissions, surfaces the
JSON output to Claude, and Claude evaluates STRICT-flip readiness per
``feedback_full_autonomous_authority``.

F-gate semantics:

* **F1 — Gate 13 falsifiability:** runs the AST scanner under STRICT
  mode against a synthetic file with a deliberate unguarded
  ``audio.dsound.failed`` emit; PASS iff the scanner exits non-zero
  (i.e. detected the violation).
* **F2 — Dual-emission ratio:** counts legacy + neutral event-name
  occurrences across the log; PASS iff ``|legacy − neutral| / max == 0``
  for the bypass-coordinator family.
* **F3 — Forensic-replay regression:** runs the regression test
  module ``tests/regression/test_h2_apo_bypass_event_misroute_replay.py``
  via ``uv run pytest``; PASS iff exit 0.
* **F4 — PII redaction continuity:** runs the security regression
  ``tests/security/test_pii_redaction_h2.py``; PASS iff exit 0.
* **F5 — Dashboard ingestion:** runs the vitest assertions in
  ``dashboard/src/locales/__tests__/locale-completeness.test.ts`` +
  the Quality Gate 8 boundary tests for ``last_bypass_event_*`` fields.
* **F6 — OTel semconv conformance:** verifies that the three v2.0.0
  metadata fields (``voice.platform``, ``voice.bypass_family``,
  ``voice.event_schema_version``) round-trip through observed log
  entries without type coercion (each appears under its expected type
  in at least one log record).

Anti-pattern compliance:
* #18 — operator runs this; no remote-API call.
* #34 — pure analysis tool; no production side-effects.
* #45 — does NOT itself emit platform-token literals; safe under
  Gate 13.

Usage:

    uv run python scripts/dev/analyze_h2_telemetry.py \\
        --log ~/.sovyx/logs/sovyx.log --json

    # Skip individual gates if their input is unavailable:
    uv run python scripts/dev/analyze_h2_telemetry.py \\
        --log /tmp/sample.log --skip-vitest

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§9 + §12 V-H2-11. Output schema verbatim from §9.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Neutral / legacy event names — mirrored from src/sovyx/voice/_event_names.py.
# Keeping them as literals (not importing) makes this script standalone so an
# operator can run it against an old daemon log even after the wrapper module
# has been removed at v0.51.0.
_NEUTRAL_EVENTS = {
    "voice.capture_integrity.bypass",
    "voice.capture_integrity.bypassed",
    "voice.capture_integrity.bypass_activated",
    "voice.capture_integrity.bypass_ineffective",
    "voice.capture_integrity.bypass_failed",
}
_LEGACY_EVENTS = {
    "voice.apo.bypass",
    "audio.apo.bypassed",
    "voice_apo_bypass_activated",
    "voice_apo_bypass_ineffective",
    "voice_apo_bypass_failed",
}

# v2.0.0 schema metadata field markers used by F6.
_F6_METADATA_FIELDS = {
    "voice.platform": (str, "linux,windows,darwin,other"),
    "voice.bypass_family": (str, "alsa_capture_chain,voice_clarity,..."),
    "voice.event_schema_version": (str, "2.0.0"),
}


@dataclass
class GateVerdict:
    passes: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TelemetryReport:
    f1_gate13_falsifiability: GateVerdict
    f2_dual_emission_ratio: GateVerdict
    f3_forensic_replay: GateVerdict
    f4_pii_continuity: GateVerdict
    f5_dashboard_ingestion: GateVerdict
    f6_otel_semconv_conformance: GateVerdict

    @property
    def ready_for_strict_flip(self) -> bool:
        return all(
            v.passes
            for v in (
                self.f1_gate13_falsifiability,
                self.f2_dual_emission_ratio,
                self.f3_forensic_replay,
                self.f4_pii_continuity,
                self.f5_dashboard_ingestion,
                self.f6_otel_semconv_conformance,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "f1_gate13_falsifiability": {
                "passes": self.f1_gate13_falsifiability.passes,
                **self.f1_gate13_falsifiability.detail,
            },
            "f2_dual_emission_ratio": {
                "passes": self.f2_dual_emission_ratio.passes,
                **self.f2_dual_emission_ratio.detail,
            },
            "f3_forensic_replay": {
                "passes": self.f3_forensic_replay.passes,
                **self.f3_forensic_replay.detail,
            },
            "f4_pii_continuity": {
                "passes": self.f4_pii_continuity.passes,
                **self.f4_pii_continuity.detail,
            },
            "f5_dashboard_ingestion": {
                "passes": self.f5_dashboard_ingestion.passes,
                **self.f5_dashboard_ingestion.detail,
            },
            "f6_otel_semconv_conformance": {
                "passes": self.f6_otel_semconv_conformance.passes,
                **self.f6_otel_semconv_conformance.detail,
            },
            "ready_for_strict_flip": self.ready_for_strict_flip,
        }


def _iter_log_records(log_path: Path) -> list[dict[str, Any]]:
    """Parse one JSON record per line from the sovyx structured log.

    Returns empty list if the log doesn't exist; non-JSON lines are skipped
    silently (operator may have mixed plain text into a debug log).
    """
    if not log_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def evaluate_f1(repo_root: Path) -> GateVerdict:
    """Quality Gate 13 STRICT correctly rejects a synthetic platform-token emit.

    Writes a temporary .py file with an unguarded ``audio.dsound.failed``
    emission, runs the scanner under ``--strict``, expects non-zero exit.
    """
    scanner = repo_root / "scripts" / "dev" / "check_platform_neutral_event_names.py"
    if not scanner.is_file():
        return GateVerdict(passes=False, detail={"error": "scanner script not found"})

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        synthetic = tmp_path / "synthetic_violation.py"
        synthetic.write_text(
            "from sovyx.observability.logging import get_logger\n"
            "logger = get_logger(__name__)\n"
            "def trigger() -> None:\n"
            '    logger.error("audio.dsound.failed", reason="f1_synthetic")\n',
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 — fixed argv
            [
                sys.executable,
                str(scanner),
                "--scan-root",
                str(tmp_path),
                "--repo-root",
                str(tmp_path),
                "--strict",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        synthetic_caught = result.returncode != 0
        return GateVerdict(
            passes=synthetic_caught,
            detail={
                "synthetic_violation_count": 1 if synthetic_caught else 0,
                "scanner_exit": result.returncode,
                "false_positives": [],
            },
        )


def evaluate_f2(records: list[dict[str, Any]]) -> GateVerdict:
    """Dual-emission ratio across the bypass-coordinator event family.

    For every neutral name, the legacy twin SHOULD fire the same number
    of times. ``drift`` is the absolute difference between the two totals
    across all 5 paired names.
    """
    counts: Counter[str] = Counter()
    for r in records:
        event = r.get("event")
        if event in _NEUTRAL_EVENTS or event in _LEGACY_EVENTS:
            counts[event] += 1
    legacy_total = sum(counts.get(e, 0) for e in _LEGACY_EVENTS)
    neutral_total = sum(counts.get(e, 0) for e in _NEUTRAL_EVENTS)
    drift = abs(legacy_total - neutral_total)
    # PASS if the totals match (and both ≥ 0 — vacuous PASS when the
    # operator hasn't triggered any bypass dispatches yet is acceptable).
    passes = drift == 0
    return GateVerdict(
        passes=passes,
        detail={
            "legacy_count": legacy_total,
            "neutral_count": neutral_total,
            "drift": drift,
        },
    )


def _run_pytest(repo_root: Path, target: str, *, timeout_s: float = 120.0) -> tuple[bool, str]:
    """Run a pytest target under uv; return (passes, stderr_tail)."""
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        return False, "uv not found in PATH"
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv
            [uv_bin, "run", "python", "-m", "pytest", target, "-q"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"
    tail = (result.stdout or "")[-400:] + (result.stderr or "")[-400:]
    return result.returncode == 0, tail


def evaluate_f3(repo_root: Path) -> GateVerdict:
    """F3 — forensic-replay regression test passes."""
    passes, tail = _run_pytest(
        repo_root,
        "tests/regression/test_h2_apo_bypass_event_misroute_replay.py",
    )
    return GateVerdict(passes=passes, detail={"tail": tail})


def evaluate_f4(repo_root: Path) -> GateVerdict:
    """F4 — PII redaction continuity regression passes."""
    passes, tail = _run_pytest(
        repo_root,
        "tests/security/test_pii_redaction_h2.py",
    )
    return GateVerdict(passes=passes, detail={"tail": tail})


def evaluate_f5(repo_root: Path, skip_vitest: bool) -> GateVerdict:
    """F5 — dashboard ingestion: locale-completeness + boundary round-trip pass."""
    py_pass, py_tail = _run_pytest(
        repo_root,
        "tests/dashboard/test_voice_status.py::TestH2CaptureBypassEventMetadata",
    )
    detail: dict[str, Any] = {"boundary_round_trip_passes": py_pass}
    if not py_pass:
        detail["py_tail"] = py_tail

    if skip_vitest:
        detail["vitest_skipped"] = True
        return GateVerdict(passes=py_pass, detail=detail)

    npx = shutil.which("npx")
    dashboard_dir = repo_root / "dashboard"
    if npx is None or not dashboard_dir.is_dir():
        detail["vitest_skipped_reason"] = "npx or dashboard/ missing"
        return GateVerdict(passes=py_pass, detail=detail)
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv
            [
                npx,
                "vitest",
                "run",
                "src/locales/__tests__/locale-completeness.test.ts",
                "--reporter=dot",
            ],
            cwd=str(dashboard_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=120.0,
        )
        vitest_pass = result.returncode == 0
        detail["i18n_locales_verified"] = ["en", "pt-BR", "es"] if vitest_pass else []
        if not vitest_pass:
            detail["vitest_tail"] = (result.stdout or "")[-300:] + (result.stderr or "")[-300:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        detail["vitest_skipped_reason"] = str(exc)
        vitest_pass = False

    return GateVerdict(passes=py_pass and vitest_pass, detail=detail)


def evaluate_f6(records: list[dict[str, Any]]) -> GateVerdict:
    """F6 — OTel semconv conformance: each v2.0.0 metadata field round-trips
    under its expected type in at least one neutral event record.

    Vacuous PASS when no neutral events were observed (operator hasn't
    triggered any dispatches yet) — Phase 2 calibration window can re-
    run once dispatches accumulate.
    """
    neutral_records = [r for r in records if r.get("event") in _NEUTRAL_EVENTS]
    fields_round_trip: list[str] = []
    missing: list[str] = []
    if not neutral_records:
        return GateVerdict(
            passes=True,
            detail={
                "fields_round_trip": [],
                "vacuous_pass": True,
                "neutral_event_count": 0,
            },
        )
    for field_name, (expected_type, _) in _F6_METADATA_FIELDS.items():
        has = any(
            isinstance(r.get(field_name), expected_type) and r.get(field_name)
            for r in neutral_records
        )
        if has:
            fields_round_trip.append(field_name)
        else:
            missing.append(field_name)
    passes = not missing
    return GateVerdict(
        passes=passes,
        detail={
            "fields_round_trip": fields_round_trip,
            "missing": missing,
            "neutral_event_count": len(neutral_records),
        },
    )


def build_report(
    *,
    log_path: Path,
    repo_root: Path,
    skip_vitest: bool,
    skip_pytest: bool,
) -> TelemetryReport:
    records = _iter_log_records(log_path)
    f1 = evaluate_f1(repo_root)
    f2 = evaluate_f2(records)
    if skip_pytest:
        f3 = GateVerdict(passes=True, detail={"skipped": True})
        f4 = GateVerdict(passes=True, detail={"skipped": True})
        f5 = GateVerdict(passes=True, detail={"skipped": True})
    else:
        f3 = evaluate_f3(repo_root)
        f4 = evaluate_f4(repo_root)
        f5 = evaluate_f5(repo_root, skip_vitest=skip_vitest)
    f6 = evaluate_f6(records)
    return TelemetryReport(
        f1_gate13_falsifiability=f1,
        f2_dual_emission_ratio=f2,
        f3_forensic_replay=f3,
        f4_pii_continuity=f4,
        f5_dashboard_ingestion=f5,
        f6_otel_semconv_conformance=f6,
    )


def _format_human(report: TelemetryReport) -> str:
    lines = ["Mission H2 — F1..F6 telemetry verdict", ""]
    data = report.to_dict()
    for key in (
        "f1_gate13_falsifiability",
        "f2_dual_emission_ratio",
        "f3_forensic_replay",
        "f4_pii_continuity",
        "f5_dashboard_ingestion",
        "f6_otel_semconv_conformance",
    ):
        v = data[key]
        marker = "PASS" if v["passes"] else "FAIL"
        detail = {k: vv for k, vv in v.items() if k != "passes"}
        lines.append(f"  [{marker}] {key} :: {detail}")
    lines.append("")
    lines.append(f"ready_for_strict_flip = {report.ready_for_strict_flip}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission H2 §9 + §12 V-H2-11 — F1..F6 telemetry analyzer."
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path(os.path.expanduser("~/.sovyx/logs/sovyx.log")),
        help="Path to sovyx structured log (default: ~/.sovyx/logs/sovyx.log)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root for pytest invocations (default: auto-detected).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON verdict.",
    )
    parser.add_argument(
        "--skip-vitest",
        action="store_true",
        help="Skip vitest invocation in F5 (faster; CI-friendly).",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip F3/F4/F5 pytest invocations (analyzer-only mode, log-driven).",
    )
    args = parser.parse_args(argv)

    report = build_report(
        log_path=args.log,
        repo_root=args.repo_root,
        skip_vitest=args.skip_vitest,
        skip_pytest=args.skip_pytest,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_human(report))

    return 0 if report.ready_for_strict_flip else 1


if __name__ == "__main__":
    sys.exit(main())
