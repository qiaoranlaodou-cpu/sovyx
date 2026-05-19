"""F1 falsifiability — Mission H4 Gate 15 STRICT must reject a deliberate violation.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§3 + §10.2 + §12 V-H4-1 / V-H4-2.

This test EXISTS to be the operational proof that Gate 15 STRICT
mechanically detects an unguarded ``logger.info("self.health.snapshot",
**{"made_up_field": value})`` producer site OR an unpaired
``ort.InferenceSession(...)`` / ``LRULockDict(...)`` construction site.
If this test passes pre-Phase-1.B (no SSoT wire-up yet), the scanner
is too lenient. If this test fails post-mission, Gate 15 is broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNER = _REPO_ROOT / "scripts" / "dev" / "check_resource_hygiene_discipline.py"

_SYNTHETIC_PRODUCER_VIOLATION = '''\
"""Synthetic — Gate 15 STRICT must reject this."""
from sovyx.observability.logging import get_logger
logger = get_logger(__name__)

def fire():
    logger.info(
        "self.health.snapshot",
        **{"totally_made_up_field": 42},
    )
'''

_SYNTHETIC_ONNX_VIOLATION = '''\
"""Synthetic — Gate 15 STRICT must reject an unpaired ONNX session."""
import onnxruntime as ort

def setup():
    sess = ort.InferenceSession("model.onnx")
'''

_SYNTHETIC_LOCKDICT_VIOLATION = '''\
"""Synthetic — Gate 15 STRICT must reject an unpaired LRULockDict."""
from sovyx.engine._lock_dict import LRULockDict

def setup():
    locks = LRULockDict(maxsize=128)
'''


def _run_scanner(scan_root: Path, tmp_root: Path) -> tuple[int, dict]:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SCANNER),
            "--scan-root",
            str(scan_root),
            "--strict",
            "--repo-root",
            str(tmp_root),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload


class TestGate15Falsifiability:
    def test_synthetic_producer_drift_fails_strict(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "src" / "sovyx" / "synthetic"
        scan_root.mkdir(parents=True)
        (scan_root / "_synthetic_producer.py").write_text(
            _SYNTHETIC_PRODUCER_VIOLATION, encoding="utf-8"
        )
        rc, payload = _run_scanner(scan_root, tmp_path)
        assert rc != 0, (
            "Gate 15 STRICT must FAIL on a synthetic unknown snapshot field — "
            "see Mission H4 §3 F1 falsifiability."
        )
        assert payload["passed"] is False
        assert payload["violation_count"] >= 1
        assert any(v["kind"] == "producer_unknown_field" for v in payload["violations"])

    def test_synthetic_onnx_unpaired_fails_strict(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "src" / "sovyx" / "voice"
        scan_root.mkdir(parents=True)
        (scan_root / "_synthetic_onnx.py").write_text(_SYNTHETIC_ONNX_VIOLATION, encoding="utf-8")
        rc, payload = _run_scanner(scan_root, tmp_path)
        assert rc != 0
        assert payload["passed"] is False
        assert any(v["kind"] == "onnx_unpaired" for v in payload["violations"])

    def test_synthetic_lockdict_unpaired_fails_strict(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "src" / "sovyx" / "foo"
        scan_root.mkdir(parents=True)
        (scan_root / "_synthetic_lockdict.py").write_text(
            _SYNTHETIC_LOCKDICT_VIOLATION, encoding="utf-8"
        )
        rc, payload = _run_scanner(scan_root, tmp_path)
        assert rc != 0
        assert payload["passed"] is False
        assert any(v["kind"] == "lockdict_unpaired" for v in payload["violations"])

    def test_allowlist_suppresses_onnx_violation(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "src" / "sovyx" / "voice"
        scan_root.mkdir(parents=True)
        (scan_root / "_synthetic_allowlisted.py").write_text(
            '"""allowlisted variant"""\n'
            "import onnxruntime as ort\n"
            "def setup():\n"
            '    sess = ort.InferenceSession("model.onnx")  # h4-allowlist: out-of-process\n',
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, tmp_path)
        assert rc == 0
        assert payload["passed"] is True
