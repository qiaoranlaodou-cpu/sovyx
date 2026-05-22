"""Falsifiability tests — Mission C Gate 18 response_model presence.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.0 +
``docs-internal/MISSION-C-FORENSIC-AUDIT-2026-05-21.md`` §17 Gate 18.

These tests EXIST to be the operational proof that:

1. Synthetic violation (a fresh route file with a router decorator
   lacking ``response_model=``) is rejected in STRICT mode.
2. Allowlist marker (``# c-allowlist: response_model_skip reason=<...>``)
   on the decorator line OR the immediately-preceding line silences
   the violation.
3. Decorators with ``response_model=`` are not flagged.
4. Non-router decorators (``@app.get``, ``@some_function``) are not
   flagged.

If any test here regresses, Gate 18 is broken — DO NOT silence it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNER = _REPO_ROOT / "scripts" / "dev" / "check_response_model_presence.py"


def _run_scanner(
    scan_root: Path,
    *,
    strict: bool = True,
) -> tuple[int, dict[str, object]]:
    args = [sys.executable, str(_SCANNER), "--scan-root", str(scan_root), "--json"]
    if strict:
        args.append("--strict")
    result = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result.returncode, payload


class TestGate18Falsifiability:
    def test_synthetic_missing_response_model_strict_fails(
        self,
        tmp_path: Path,
    ) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_violation.py").write_text(
            """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/synthetic-c-gate18")
def handler() -> dict:
    return {"ok": True}
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc != 0
        assert payload["passed"] is False
        assert payload["violation_count"] == 1
        assert payload["violations"][0]["route_path"] == "/synthetic-c-gate18"

    def test_response_model_present_passes(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_compliant.py").write_text(
            """\
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class Resp(BaseModel):
    ok: bool


@router.get("/synthetic-compliant", response_model=Resp)
def handler() -> Resp:
    return Resp(ok=True)
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["passed"] is True
        assert payload["violation_count"] == 0

    def test_allowlist_same_line_silences_violation(
        self,
        tmp_path: Path,
    ) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_allowlisted.py").write_text(
            """\
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/stream")  # c-allowlist: response_model_skip reason=StreamingResponse body
def handler() -> StreamingResponse:
    return StreamingResponse(iter([b"x"]))
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0
        assert payload["decorators_allowlisted"] == 1

    def test_allowlist_preceding_line_silences_violation(
        self,
        tmp_path: Path,
    ) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_allowlisted_above.py").write_text(
            """\
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()


# c-allowlist: response_model_skip reason=FileResponse body
@router.get("/download")
def handler() -> FileResponse:
    return FileResponse("/tmp/x")
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc == 0
        assert payload["violation_count"] == 0
        assert payload["decorators_allowlisted"] == 1

    def test_allowlist_without_reason_does_not_silence(
        self,
        tmp_path: Path,
    ) -> None:
        """Allowlist marker MUST carry `reason=<text>` per the gate
        contract. A bare `# c-allowlist: response_model_skip` is treated
        as commentary, not a waiver."""
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_bad_allowlist.py").write_text(
            """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/x")  # c-allowlist: response_model_skip
def handler() -> dict:
    return {}
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc != 0
        assert payload["violation_count"] == 1

    def test_non_router_decorator_not_flagged(self, tmp_path: Path) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_app.py").write_text(
            """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/x")
def handler() -> dict:
    return {}
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        # The gate scope is `router.<verb>` only; `app.<verb>` is
        # legitimate (FastAPI app instance, not a router).
        assert rc == 0
        assert payload["violation_count"] == 0

    def test_sub_router_suffix_is_recognized(self, tmp_path: Path) -> None:
        """Receiver names ending in `_router` are also treated as routers
        (e.g., `voice_router`, `engine_router`)."""
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_subrouter.py").write_text(
            """\
from fastapi import APIRouter

voice_router = APIRouter()


@voice_router.get("/x")
def handler() -> dict:
    return {}
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=True)
        assert rc != 0
        assert payload["violation_count"] == 1

    def test_lenient_mode_exits_zero_with_violations(
        self,
        tmp_path: Path,
    ) -> None:
        scan_root = tmp_path / "routes"
        scan_root.mkdir(parents=True)
        (scan_root / "synthetic_lenient.py").write_text(
            """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/lenient-violation")
def handler() -> dict:
    return {}
""",
            encoding="utf-8",
        )
        rc, payload = _run_scanner(scan_root, strict=False)
        assert rc == 0
        assert payload["passed"] is False
        assert payload["violation_count"] == 1


class TestGate18CurrentBaseline:
    """Anchor the current real-world baseline so a regression to
    response_model adoption is loud."""

    def test_dashboard_routes_baseline_under_threshold(self) -> None:
        """Mission C audit estimated ~70 missing response_model decorators
        across ~26 route files. The exact number is the LENIENT baseline;
        we assert it does NOT exceed 80 (a regression budget). Once
        Phase C.4 batches close routes, this assertion can tighten."""
        from scripts.dev.check_response_model_presence import scan_dir

        report = scan_dir()
        assert report.files_scanned > 0
        assert report.decorators_inspected > 100
        # Pre-mission baseline is ~70; allow up to 80 as the
        # regression-budget headroom. Phase C.4 progressively closes
        # routes; this assertion tightens as the body work lands.
        assert len(report.violations) <= 80, (
            f"Regression budget exceeded: {len(report.violations)} routes "
            "missing response_model (pre-C.0 baseline was ~70). Either "
            "close the new routes or add a `# c-allowlist: "
            "response_model_skip reason=<...>` marker."
        )
