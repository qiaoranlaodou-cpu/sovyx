"""W3.0 / AP #53 — producer↔consumer round-trip for the Windows-v2 contract.

Builds a synthetic Windows ``SUMMARY.json`` whose field names come from the
SSoT (:mod:`sovyx.voice.diagnostics._schema`), runs it through the triage
analyzer (which now reads the SAME SSoT), and asserts the Windows hypotheses
fire as designed. If a field name drifts on EITHER side, the relevant
hypothesis stops firing and this test fails — which is exactly the
counted-but-zeroed-hypothesis class AP #53 exists to prevent, and the
contract a future Windows producer (W3.1) must satisfy.
"""

from __future__ import annotations

import json
import tarfile
from typing import TYPE_CHECKING

from sovyx.voice.diagnostics import _schema
from sovyx.voice.diagnostics.triage import triage_tarball

if TYPE_CHECKING:
    from pathlib import Path


def _windows_summary(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        _schema.SUMMARY_SCHEMA_VERSION: 1,
        "tool": "sovyx-voice-diagnostic-windows",  # → _detect_toolkit == windows
        "tool_version": "2.0.0",
        "host": "win-host",
        "captured_at_utc": "2026-06-01T00:00:00Z",
        "status": "complete",
        "final_exit_code": 0,
        _schema.ANALYZER_SELFTEST_STATUS: "pass",
    }
    base.update(overrides)
    return base


def _windows_tarball(tmp_path: Path, summary: dict[str, object]) -> Path:
    root = tmp_path / "sovyx-voice-diagnostic-win-20260601T000000Z-deadbeef"
    (root / "_diagnostics").mkdir(parents=True)
    (root / "SUMMARY.json").write_text(json.dumps(summary, indent=2))
    (root / "_diagnostics" / "alerts.jsonl").write_text("")
    (root / "MANIFEST.md").write_text("# MANIFEST")
    tar_path = tmp_path / "win-fixture.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(root, arcname=root.name)
    return tar_path


def _confidence(result: object, hid: str) -> float:
    for h in result.hypotheses:  # type: ignore[attr-defined]
        if h.hid == hid:
            return h.confidence
    return 0.0


class TestWindowsV2RoundTrip:
    def test_toolkit_detected_as_windows(self, tmp_path: Path) -> None:
        tar = _windows_tarball(tmp_path, _windows_summary())
        result = triage_tarball(tar)
        assert result.toolkit == "windows"

    def test_h2_apo_confirmed_fires(self, tmp_path: Path) -> None:
        summary = _windows_summary(
            **{
                _schema.WIN_AUDIO_ENDPOINTS: [
                    {
                        _schema.WIN_ENDPOINT_FRIENDLY_NAME: "Realtek Mic",
                        _schema.WIN_ENDPOINT_IS_ACTIVE: True,
                        _schema.WIN_ENDPOINT_VOICE_CLARITY_ACTIVE: True,
                    }
                ],
                _schema.WIN_LIVE_CAPTURES: {
                    _schema.WIN_LIVE_VERDICT: _schema.WIN_LIVE_VERDICT_APO_CONFIRMED,
                    _schema.WIN_LIVE_DELTA_RMS: -30.0,
                    _schema.WIN_LIVE_DELTA_VAD: 0.5,
                },
            }
        )
        result = triage_tarball(_windows_tarball(tmp_path, summary))
        # endpoint (0.4) + comparator confirmed (0.6) → high-confidence H2.
        assert _confidence(result, "H2") >= 0.5

    def test_h2_apo_not_culprit_does_not_fire(self, tmp_path: Path) -> None:
        summary = _windows_summary(
            **{
                _schema.WIN_LIVE_CAPTURES: {
                    _schema.WIN_LIVE_VERDICT: _schema.WIN_LIVE_VERDICT_APO_NOT_CULPRIT,
                },
            }
        )
        result = triage_tarball(_windows_tarball(tmp_path, summary))
        assert _confidence(result, "H2") < 0.5

    def test_h5_consent_denied_fires(self, tmp_path: Path) -> None:
        summary = _windows_summary(
            **{
                _schema.WIN_CONSENT_STORE: {
                    _schema.WIN_CONSENT_USER_GLOBAL: 0,
                    _schema.WIN_CONSENT_NONPACKAGED: [
                        {
                            _schema.WIN_CONSENT_APP_PATH: "C:/python/python.exe",
                            _schema.WIN_CONSENT_APP_VALUE: 0,
                        }
                    ],
                },
            }
        )
        result = triage_tarball(_windows_tarball(tmp_path, summary))
        assert _confidence(result, "H5") >= 0.5

    def test_h9_no_active_endpoints_fires(self, tmp_path: Path) -> None:
        summary = _windows_summary(
            **{
                _schema.WIN_AUDIO_ENDPOINTS: [
                    {
                        _schema.WIN_ENDPOINT_FRIENDLY_NAME: "Disabled Mic",
                        _schema.WIN_ENDPOINT_IS_ACTIVE: False,
                    }
                ],
            }
        )
        result = triage_tarball(_windows_tarball(tmp_path, summary))
        assert _confidence(result, "H9") >= 0.5
