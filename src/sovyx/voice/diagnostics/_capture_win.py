"""Windows forensic diagnostic producer (W3.1 Phase 1).

The Linux bash toolkit is Linux-only (it captures the ALSA/PipeWire graph),
so ``sovyx doctor voice --full-diag`` refuses on Windows. The triage analyzer,
however, has ALWAYS been multi-toolkit on the READ side: its ``toolkit ==
"windows"`` hypotheses expect a "Windows v2" ``SUMMARY.json``. The missing
half was a PRODUCER that emits it. This module is that producer's first
phase: it composes the already-shipped, already-tested Windows health probes
into the Windows-v2 contract (the :mod:`sovyx.voice.diagnostics._schema` SSoT
the analyzer reads), so a Windows operator's tarball drives real triage
verdicts.

Reuse (no new forensic code in this phase):

* ``audio_endpoints`` ← :func:`sovyx.voice._apo_detector.detect_capture_apos`
  (each active capture endpoint + its ``voice_clarity_active`` bit — the H2
  Voice-Clarity-APO signal).
* ``consent_store`` ← :func:`sovyx.voice.health._mic_permission.check_microphone_permission`
  (the Windows ConsentStore verdict — the H5 mic-permission signal).

Documented Phase-2 enhancements (NOT in this phase): the ``live_captures``
shared-vs-exclusive WASAPI comparator oracle (boosts H2 confidence; needs a
real audio comparison), per-app ``nonpackaged_apps`` consent rows, and a full
active+inactive endpoint enumeration (the H9 all-inactive case). The CLI /
``_runner`` dispatch for ``--full-diag`` on win32 + the Gate-20 parity check
are the W3.2 / W3.3 steps.
"""

from __future__ import annotations

import json
import platform
import tarfile
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.diagnostics import _schema

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.health._mic_permission import MicPermissionReport

logger = get_logger(__name__)

_TOOL_NAME = "sovyx-voice-diagnostic-windows"
_TOOL_VERSION = "2.0.0"


def build_windows_summary(
    *,
    apo_reports: Sequence[CaptureApoReport] | None = None,
    permission: MicPermissionReport | None = None,
    host: str | None = None,
    captured_at_utc: str | None = None,
    selftest_status: str = "pass",
) -> dict[str, Any]:
    """Compose the Windows-v2 ``SUMMARY.json`` dict from the Windows probes.

    ``apo_reports`` / ``permission`` default to the real probes when ``None``
    (lazy-imported so this module stays import-clean cross-platform); tests
    inject them. Field names come from the SSoT so producer + consumer can
    never drift (anti-pattern #53).
    """
    if apo_reports is None:
        from sovyx.voice._apo_detector import detect_capture_apos  # noqa: PLC0415

        apo_reports = detect_capture_apos()
    if permission is None:
        from sovyx.voice.health._mic_permission import (  # noqa: PLC0415
            check_microphone_permission,
        )

        permission = check_microphone_permission()

    # ``detect_capture_apos`` only returns ACTIVE endpoints, so is_active=True
    # for each. (The all-inactive H9 case needs a full enumeration — Phase 2.)
    audio_endpoints = [
        {
            _schema.WIN_ENDPOINT_FRIENDLY_NAME: r.endpoint_name,
            _schema.WIN_ENDPOINT_IS_ACTIVE: True,
            _schema.WIN_ENDPOINT_VOICE_CLARITY_ACTIVE: r.voice_clarity_active,
        }
        for r in apo_reports
    ]

    from sovyx.voice.health._mic_permission import (  # noqa: PLC0415
        MicPermissionStatus,
    )

    denied = permission.status is MicPermissionStatus.DENIED
    consent_store: dict[str, Any] = {
        _schema.WIN_CONSENT_USER_GLOBAL: 0 if denied else 1,
        # Per-app rows need a deeper probe (Phase 2); H5 still fires on the
        # user-global denial above.
        _schema.WIN_CONSENT_NONPACKAGED: [],
    }

    return {
        _schema.SUMMARY_SCHEMA_VERSION: 1,
        "tool": _TOOL_NAME,
        "tool_version": _TOOL_VERSION,
        "host": host if host is not None else platform.node(),
        "captured_at_utc": captured_at_utc if captured_at_utc is not None else _utc_now_iso(),
        "os_descriptor": platform.platform(),
        "status": "complete",
        "exit_code": "0",
        _schema.ANALYZER_SELFTEST_STATUS: selftest_status,
        _schema.WIN_AUDIO_ENDPOINTS: audio_endpoints,
        _schema.WIN_CONSENT_STORE: consent_store,
        # Linux toolkit doesn't emit network_llm either; reserved for a
        # provider-reachability probe in a later phase.
        _schema.NETWORK_LLM: [],
    }


def write_windows_tarball(
    summary: dict[str, Any],
    output_root: Path,
    *,
    work_name: str | None = None,
) -> Path:
    """Write ``summary`` into a tarball that :func:`triage_tarball` can read.

    Mirrors the Linux toolkit's output shape (a ``SUMMARY.json`` +
    ``_diagnostics/alerts.jsonl`` under a single root dir) so the analyzer's
    extract + find-summary seam handles it unchanged. Returns the tarball path.
    """
    name = work_name or f"sovyx-voice-diagnostic-win-{_stamp()}-{uuid.uuid4().hex[:8]}"
    work = output_root / name
    (work / "_diagnostics").mkdir(parents=True)
    (work / "SUMMARY.json").write_text(json.dumps(summary, indent=2))
    (work / "_diagnostics" / "alerts.jsonl").write_text("")
    (work / "MANIFEST.md").write_text("# Sovyx Windows voice diagnostic\n")
    tar_path = output_root / f"{name}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(work, arcname=work.name)
    logger.info("voice.diagnostics.windows_tarball_written", path=str(tar_path))
    return tar_path


def run_windows_diag(output_root: Path) -> Path:
    """Produce a Windows forensic tarball from live probes. Returns its path."""
    return write_windows_tarball(build_windows_summary(), output_root)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


__all__ = ["build_windows_summary", "run_windows_diag", "write_windows_tarball"]
