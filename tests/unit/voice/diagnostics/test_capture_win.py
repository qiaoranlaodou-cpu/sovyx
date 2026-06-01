"""W3.1 Phase 1 — the Windows forensic producer + producer→triage round-trip.

Proves the producer composes the Windows health probes into the SSoT contract
AND that a tarball it writes drives the analyzer's Windows hypotheses
end-to-end — closing the loop the analyzer was always ready for but no
producer fed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.voice._apo_detector import CaptureApoReport
from sovyx.voice.diagnostics import _schema
from sovyx.voice.diagnostics._capture_win import (
    build_windows_summary,
    write_windows_tarball,
)
from sovyx.voice.diagnostics.triage import triage_tarball
from sovyx.voice.health._mic_permission import (
    MicPermissionReport,
    MicPermissionStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


def _apo(name: str, *, voice_clarity: bool) -> CaptureApoReport:
    return CaptureApoReport(
        endpoint_id="{0.0.1.00000000}.{deadbeef}",
        endpoint_name=name,
        enumerator="USB",
        fx_binding_count=1,
        voice_clarity_active=voice_clarity,
    )


def _perm(status: MicPermissionStatus) -> MicPermissionReport:
    return MicPermissionReport(status=status)


def _summary(**kw: object) -> dict:
    kw.setdefault("host", "win-host")
    kw.setdefault("captured_at_utc", "2026-06-01T00:00:00Z")
    return build_windows_summary(**kw)  # type: ignore[arg-type]


def _confidence(result: object, hid: str) -> float:
    for h in result.hypotheses:  # type: ignore[attr-defined]
        if h.hid == hid:
            return h.confidence
    return 0.0


class TestBuildWindowsSummary:
    def test_maps_apo_endpoint_to_audio_endpoints(self) -> None:
        summary = _summary(
            apo_reports=[_apo("Razer Mic", voice_clarity=True)],
            permission=_perm(MicPermissionStatus.GRANTED),
        )
        endpoints = summary[_schema.WIN_AUDIO_ENDPOINTS]
        assert len(endpoints) == 1
        assert endpoints[0][_schema.WIN_ENDPOINT_FRIENDLY_NAME] == "Razer Mic"
        assert endpoints[0][_schema.WIN_ENDPOINT_IS_ACTIVE] is True
        assert endpoints[0][_schema.WIN_ENDPOINT_VOICE_CLARITY_ACTIVE] is True

    def test_granted_permission_is_user_global_one(self) -> None:
        summary = _summary(apo_reports=[], permission=_perm(MicPermissionStatus.GRANTED))
        assert summary[_schema.WIN_CONSENT_STORE][_schema.WIN_CONSENT_USER_GLOBAL] == 1

    def test_denied_permission_is_user_global_zero(self) -> None:
        summary = _summary(apo_reports=[], permission=_perm(MicPermissionStatus.DENIED))
        assert summary[_schema.WIN_CONSENT_STORE][_schema.WIN_CONSENT_USER_GLOBAL] == 0

    def test_tool_token_routes_to_windows_toolkit(self) -> None:
        summary = _summary(apo_reports=[], permission=_perm(MicPermissionStatus.UNKNOWN))
        assert any(tok in summary["tool"] for tok in _schema.TOOLKIT_WINDOWS_TOKENS)


class TestProducerToTriageRoundTrip:
    def test_voice_clarity_endpoint_drives_h2(self, tmp_path: Path) -> None:
        summary = _summary(
            apo_reports=[_apo("Realtek Mic", voice_clarity=True)],
            permission=_perm(MicPermissionStatus.GRANTED),
        )
        result = triage_tarball(write_windows_tarball(summary, tmp_path))
        assert result.toolkit == "windows"
        # Endpoint with voice_clarity_active=true → H2 fired (add_for 0.4).
        assert _confidence(result, "H2") >= 0.4

    def test_denied_consent_drives_h5(self, tmp_path: Path) -> None:
        summary = _summary(
            apo_reports=[],
            permission=_perm(MicPermissionStatus.DENIED),
        )
        result = triage_tarball(write_windows_tarball(summary, tmp_path))
        # user_global=0 → H5 mic-permission-denied fired.
        assert _confidence(result, "H5") >= 0.5

    def test_clean_windows_box_no_false_positives(self, tmp_path: Path) -> None:
        summary = _summary(
            apo_reports=[_apo("Clean Mic", voice_clarity=False)],
            permission=_perm(MicPermissionStatus.GRANTED),
        )
        result = triage_tarball(write_windows_tarball(summary, tmp_path))
        # No APO, permission granted → neither H2 nor H5 should be confident.
        assert _confidence(result, "H2") < 0.5
        assert _confidence(result, "H5") < 0.5
