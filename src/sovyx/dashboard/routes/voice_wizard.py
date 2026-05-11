"""Voice setup wizard endpoints — Phase 7 / T7.21-T7.24.

Operator-facing wizard that runs the first time a user enables voice
in the dashboard. Four endpoints:

* ``GET /api/voice/wizard/devices`` (T7.21) — list candidate input
  devices with friendly names + per-device diagnosis hints. Used by
  the wizard's device-picker.

* ``POST /api/voice/wizard/test-record`` (T7.22) — kick off a 3-second
  capture-and-analyse session ("Zoom pattern" — record yourself, see
  the analysis). Returns a session_id + the immediate analysis.
  Synchronous: the 3 s recording happens inside the request.

* ``GET /api/voice/wizard/test-result/{session_id}`` (T7.23) — re-read
  a previously-completed session's result. The session store is
  bounded (last 64 sessions per app instance) so dashboards
  navigating away + back can reload without re-recording. Sessions
  are not persisted across daemon restarts (in-memory only).

* ``GET /api/voice/wizard/diagnostic`` (T7.24) — same data as
  ``GET /api/voice/capture-diagnostics`` but with the wizard-friendly
  shape (single ``ready: bool`` + ``recommendations`` list instead
  of the raw APO endpoint dump). CLI parity with
  ``sovyx doctor voice_capture_apo``.

Dependency-injection design:
  Recording requires a real microphone — not testable in headless CI
  without elaborate audio mocks. The test-record endpoint takes a
  ``WizardRecorder`` protocol-typed dependency from
  ``request.app.state.wizard_recorder``. Unit tests inject a fake
  recorder that returns deterministic synthetic audio; production
  daemon registers a real ``SoundDeviceWizardRecorder`` at boot. When
  no recorder is registered, the endpoint returns 503 with a clear
  "voice capture not available" detail — same pattern as the
  existing ``/api/voice/forget`` endpoint when the registry isn't
  ready.

Reference: master mission §Phase 7 / T7.21-T7.24.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import secrets
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

logger = get_logger(__name__)

router = APIRouter(prefix="/api/voice/wizard", dependencies=[Depends(verify_token)])


# ── Constants ────────────────────────────────────────────────────────


_DEFAULT_RECORD_DURATION_S = 3.0
"""Wizard test-record default duration. 3 s matches the Zoom-style
"say something" pattern + leaves enough headroom for a full
sentence's worth of SNR analysis."""

_MIN_RECORD_DURATION_S = 1.0
_MAX_RECORD_DURATION_S = 10.0
_TARGET_SAMPLE_RATE = 16000
"""All wizard analysis runs at 16 kHz mono — same as the rest of the
voice subsystem (Moonshine / Silero / OpenWakeWord). Resampling
happens inside the recorder."""

_SESSION_STORE_MAX = 64
"""Bounded LRU cache for completed recording results. 64 sessions
covers a typical wizard-debugging session of 5-10 attempts × 6
operators dashboard-sharing without per-instance memory pressure
(< 1 MB cap at full capacity)."""

_SESSION_TTL_S = 3600.0
"""Sessions expire after 1 hour. Beyond that the wizard treats the
session_id as "not found" + the operator restarts the test-record
flow. Bounded TTL prevents memory creep across long-running
daemons."""


# ── Recorder protocol (dependency injection point) ──────────────────


@runtime_checkable
class WizardRecorder(Protocol):
    """Protocol for the test-record dependency.

    Production wires ``SoundDeviceWizardRecorder`` (uses ``sounddevice``
    against the operator's actual hardware). Tests inject a stub
    that returns synthetic audio — fully testable without a mic.
    """

    def record(
        self,
        *,
        duration_s: float,
        device_id: str | None,
    ) -> npt.NDArray[np.float32]:
        """Capture mono float32 audio at 16 kHz for ``duration_s``.

        Args:
            duration_s: Capture duration. Caller bounds to
                [1.0, 10.0]; the recorder honours it precisely
                — over-capture wastes time, under-capture truncates
                the operator's utterance.
            device_id: PortAudio device index (as a string) or
                ``None`` for the system default. Stringly-typed
                because PortAudio device IDs change across reboots
                + the wizard surfaces friendly names.

        Returns:
            Mono float32 ndarray of shape ``(int(duration_s * 16000),)``.
            Values bounded to [-1.0, 1.0]. Empty / silent capture
            returns an array of zeros.

        Raises:
            RuntimeError: When the recorder is unable to open the
                device (permission denied, device busy, etc.).
                Error message is operator-facing.
        """
        ...


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    """One completed recording session, cached for retrieval by ID."""

    session_id: str
    response: WizardTestResultResponse
    created_at_monotonic: float


class _SessionStore:
    """In-memory LRU + TTL cache for completed sessions.

    Thread-safe via internal :class:`threading.Lock` because the
    test-record endpoint runs the recording in :func:`asyncio.to_thread`
    + the cache write happens after the await — both the test-record
    and test-result handlers may touch the cache concurrently.
    """

    def __init__(
        self,
        *,
        max_size: int = _SESSION_STORE_MAX,
        ttl_s: float = _SESSION_TTL_S,
    ) -> None:
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._store: OrderedDict[str, _SessionRecord] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, record: _SessionRecord) -> None:
        with self._lock:
            self._store[record.session_id] = record
            self._store.move_to_end(record.session_id)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def get(self, session_id: str) -> _SessionRecord | None:
        with self._lock:
            record = self._store.get(session_id)
            if record is None:
                return None
            now = time.monotonic()
            if now - record.created_at_monotonic > self._ttl_s:
                # Expired — evict + treat as missing.
                self._store.pop(session_id, None)
                return None
            self._store.move_to_end(session_id)
            return record

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


def _get_session_store(request: Request) -> _SessionStore:
    """Get or lazily create the per-app session store."""
    state = request.app.state
    store: _SessionStore | None = getattr(state, "wizard_session_store", None)
    if store is None:
        store = _SessionStore()
        state.wizard_session_store = store
    return store


# ── Request / response models ────────────────────────────────────────


class WizardDeviceInfo(BaseModel):
    """One input device row in the wizard's picker."""

    device_id: str = Field(..., description="PortAudio device index as string.")
    name: str = Field(..., description="OS-reported device name.")
    friendly_name: str = Field(
        ...,
        description=(
            "Operator-readable label. Prefers the friendly name when "
            "the OS exposes one; falls back to the raw name."
        ),
    )
    max_input_channels: int = Field(
        ...,
        ge=0,
        description="Maximum capture channels the OS reports.",
    )
    default_sample_rate: int = Field(..., ge=0)
    is_default: bool = Field(..., description="Whether this is the OS default input.")
    diagnosis_hint: str = Field(
        ...,
        description=(
            "One of ``ready``, ``warning_low_channels``, "
            "``warning_high_sample_rate``, ``error_unavailable``. "
            "Drives the wizard UI's per-row colour code."
        ),
    )


class WizardDevicesResponse(BaseModel):
    devices: list[WizardDeviceInfo]
    total_count: int = Field(..., ge=0)
    default_device_id: str | None = None


class WizardTestRecordRequest(BaseModel):
    device_id: str | None = Field(
        None,
        description=(
            "PortAudio device index as string (matches ``WizardDeviceInfo.device_id``). "
            "``None`` uses the system default input."
        ),
    )
    duration_seconds: float = Field(
        default=_DEFAULT_RECORD_DURATION_S,
        ge=_MIN_RECORD_DURATION_S,
        le=_MAX_RECORD_DURATION_S,
        description="Capture duration. Bounded [1, 10] s.",
    )


class WizardTestResultResponse(BaseModel):
    """Synchronous result of a test-record session."""

    session_id: str
    success: bool
    duration_actual_s: float = Field(..., ge=0.0)
    sample_rate_hz: int = Field(..., ge=0)
    level_rms_dbfs: float | None = Field(None, description="RMS dBFS or null on no signal.")
    level_peak_dbfs: float | None = Field(None, description="Peak dBFS or null on no signal.")
    snr_db: float | None = Field(None, description="SNR estimate in dB.")
    clipping_detected: bool = Field(
        ...,
        description="True when peak ≥ -0.1 dBFS (clip-warning threshold).",
    )
    silent_capture: bool = Field(
        ...,
        description="True when peak < -50 dBFS (no usable signal).",
    )
    diagnosis: str = Field(
        ...,
        description=(
            "Closed-set verdict: ``ok``, ``low_signal``, ``clipping``, "
            "``no_audio``, ``recorder_unavailable``, ``device_error``."
        ),
    )
    diagnosis_hint: str = Field(
        ...,
        description="Human-readable next-step hint for the operator.",
    )
    recorded_at_utc: str = Field(..., description="ISO-8601 UTC timestamp.")
    error: str | None = Field(
        None,
        description=("Error message when ``success`` is False; None otherwise."),
    )


class WizardDiagnosticResponse(BaseModel):
    """Wizard-shaped capture diagnostic.

    Distilled view of ``GET /api/voice/capture-diagnostics`` for
    direct consumption by the wizard UI. The full APO endpoint dump
    remains available at the original URL for the troubleshooting
    panel.
    """

    ready: bool = Field(
        ...,
        description=(
            "True when the active capture endpoint is unmolested "
            "by Voice Clarity APO + has no other known interferers."
        ),
    )
    voice_clarity_active: bool = Field(
        ...,
        description="True when Voice Clarity APO is registered on the active mic.",
    )
    active_device_name: str | None = None
    platform: str = Field(..., description="``win32`` / ``linux`` / ``darwin``.")
    recommendations: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-actionable hints, ordered by priority. Empty when the system is ``ready``."
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _safe_db(linear: float) -> float | None:
    """Convert linear amplitude to dBFS, with a floor of -120 dB.

    Returns ``None`` for exactly 0 so JSON consumers can render
    "no signal" rather than the misleading ``-inf`` placeholder.
    """
    if linear <= 0.0:
        return None
    return float(20.0 * np.log10(max(linear, 1e-6)))


def _analyse_audio(
    samples: npt.NDArray[np.float32],
    sample_rate: int,
) -> dict[str, float | None]:
    """Compute RMS / peak / SNR over a captured buffer.

    SNR estimation uses the simple "top quartile vs bottom quartile"
    heuristic: sort frame energies, take the mean of the top 25% as
    "signal" and the mean of the bottom 25% as "noise". Adequate
    for the wizard's UX-grade decision (good vs bad mic) — not a
    precision instrument.
    """
    if samples.size == 0:
        return {
            "rms_dbfs": None,
            "peak_dbfs": None,
            "snr_db": None,
        }
    abs_samples = np.abs(samples)
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    peak = float(np.max(abs_samples))

    # Frame-energy SNR estimate. 20 ms frames at 16 kHz = 320 samples.
    frame_size = max(1, int(0.020 * sample_rate))
    if samples.size < 4 * frame_size:
        snr_db: float | None = None
    else:
        n_frames = samples.size // frame_size
        frames = samples[: n_frames * frame_size].reshape(n_frames, frame_size)
        energies = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        sorted_e = np.sort(energies)
        q = max(1, n_frames // 4)
        signal = float(np.mean(sorted_e[-q:]))
        noise = float(np.mean(sorted_e[:q]))
        if noise <= 0.0 or signal <= 0.0:
            snr_db = None
        else:
            snr_db = float(20.0 * np.log10(signal / max(noise, 1e-6)))

    return {
        "rms_dbfs": _safe_db(rms),
        "peak_dbfs": _safe_db(peak),
        "snr_db": snr_db,
    }


def _no_audio_hint(platform_key: str) -> str:
    """Return a platform-aware ``no_audio`` remediation hint.

    Mission ``MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
    §Phase 2 T2.7. Pre-T2.7 the wizard returned a single generic
    "Check the mic is connected, unmuted, and selected in your OS
    sound settings" hint regardless of platform. The forensic case
    proved this is operationally insufficient on Linux+PipeWire — the
    operator opens Cinnamon Sound Settings, sees the mic at 100 %
    volume, "not muted", concludes the wizard is wrong. The actual
    problem is in deeper layers (ALSA mixer ``Capture`` switch off,
    WirePlumber default-source routed to a ``.monitor``, codec quirk
    with no UCM profile) that no end-user GUI exposes.

    On Linux this returns a 3-step actionable recipe with the exact
    shell commands from the OPERATOR-DEBT-MASTER D24 playbook. Shell
    commands are deliberately language-neutral so this single English
    string serves operators regardless of UI locale (a future
    revision can split into per-locale variants if needed).

    On Windows and macOS the original generic hint is preserved —
    those platforms have GUI-native mute / device-pick controls that
    the operator can navigate without shell commands. Future work may
    add Windows-specific hints (Privacy & Security → Microphone, App
    permissions, exclusive-mode contention) and macOS-specific hints
    (System Settings → Privacy & Security → Microphone).
    """
    if platform_key.startswith("linux"):
        return (
            "No usable signal captured. On Linux+PipeWire this is "
            "almost always either an ALSA mixer state issue OR a "
            "WirePlumber default-source routing issue, NOT what the "
            "Cinnamon / GNOME sound applet shows. Run these 3 checks:\n"
            "1) Confirm card index: arecord -l (mic is usually card 1)\n"
            "2) Fix ALSA mixer + persist: amixer -c<N> sset 'Capture' cap; "
            "amixer -c<N> sset 'Capture' 80%; amixer -c<N> sset "
            "'Internal Mic Boost' 67%; sudo alsactl store <N>\n"
            "3) Verify WirePlumber default source is the real mic (not a "
            "monitor): pactl get-default-source — if it ends in '.monitor', "
            "run: wpctl status (find the mic source ID), then: "
            "wpctl set-default <ID>; pactl set-source-mute @DEFAULT_SOURCE@ 0; "
            "pactl set-source-volume @DEFAULT_SOURCE@ 80%\n"
            "Then re-run the wizard test."
        )
    return (
        "No usable signal captured. Check the mic is connected, "
        "unmuted, and selected in your OS sound settings."
    )


def _diagnose(
    *,
    peak_dbfs: float | None,
    snr_db: float | None,
) -> tuple[str, str, bool, bool]:
    """Closed-set diagnosis from the analysis numbers.

    Returns ``(diagnosis, hint, clipping_detected, silent_capture)``.
    Threshold sources:

    * Clipping at ≥ -0.1 dBFS — standard "almost-full-scale" guard
      used by every audio toolkit.
    * Silent capture below -50 dBFS — a -50 dB peak is well below
      any human voice + indicates a muted / disconnected mic.
    * Low signal between -50 and -30 dBFS — usable but quiet;
      operator should speak louder or turn up the mic gain.
    * SNR < 10 dB → noisy.
    """
    clipping = peak_dbfs is not None and peak_dbfs >= -0.1  # noqa: PLR2004
    silent = peak_dbfs is None or peak_dbfs < -50.0  # noqa: PLR2004

    if silent:
        return (
            "no_audio",
            _no_audio_hint(sys.platform),
            False,
            True,
        )
    if clipping:
        return (
            "clipping",
            "Signal is clipping. Move further from the mic or lower "
            "your input gain in OS sound settings.",
            True,
            False,
        )
    if peak_dbfs is not None and peak_dbfs < -30.0:  # noqa: PLR2004
        return (
            "low_signal",
            "Signal is usable but quiet. Speak louder or raise mic input gain.",
            False,
            False,
        )
    if snr_db is not None and snr_db < 10.0:  # noqa: PLR2004
        return (
            "noisy",
            "Background noise is high relative to your voice. Move "
            "to a quieter room or use a headset mic.",
            False,
            False,
        )
    return ("ok", "Microphone looks good.", False, False)


# ── T7.21 — list devices ─────────────────────────────────────────────


@router.get("/devices", response_model=WizardDevicesResponse)
async def list_wizard_devices(request: Request) -> WizardDevicesResponse:
    """List candidate input devices with friendly names + per-row hints.

    The wizard's device picker calls this on mount. Returns ``[]``
    when no input devices are detected — UI surfaces "No microphone
    found" with a "Refresh" button.
    """
    try:
        # Lazy import to keep ``sounddevice`` off the path of dashboards
        # served on hosts without audio hardware.
        from sovyx.voice.audio import AudioCapture  # noqa: PLC0415

        raw_devices = await asyncio.to_thread(AudioCapture.list_devices)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_wizard_devices_enumeration_failed", error=str(exc))
        return WizardDevicesResponse(devices=[], total_count=0, default_device_id=None)

    default_device_id: str | None = None
    try:
        import sounddevice as sd  # noqa: PLC0415

        default = sd.default.device
        if isinstance(default, (list, tuple)) and len(default) >= 1:
            default_device_id = str(default[0])
        elif isinstance(default, int):
            default_device_id = str(default)
    except Exception:  # noqa: BLE001
        default_device_id = None

    devices_out: list[WizardDeviceInfo] = []
    for d in raw_devices:
        device_id = str(d.get("index", ""))
        name = str(d.get("name", "")).strip() or "Unknown"
        max_channels = int(d.get("channels", 0))
        sample_rate = int(d.get("rate", 0))

        # Per-row diagnosis hint — drives the colour code in the UI.
        if max_channels == 0:
            hint = "error_unavailable"
        elif max_channels == 1:
            hint = "warning_low_channels"
        elif sample_rate not in (16000, 24000, 32000, 44100, 48000, 88200, 96000):
            hint = "warning_high_sample_rate"
        else:
            hint = "ready"

        devices_out.append(
            WizardDeviceInfo(
                device_id=device_id,
                name=name,
                friendly_name=name,  # OS-reported name = friendly name
                max_input_channels=max_channels,
                default_sample_rate=sample_rate,
                is_default=(device_id == default_device_id),
                diagnosis_hint=hint,
            )
        )

    return WizardDevicesResponse(
        devices=devices_out,
        total_count=len(devices_out),
        default_device_id=default_device_id,
    )


# ── T7.22 — test-record ──────────────────────────────────────────────


def _resolve_recorder(request: Request) -> WizardRecorder | None:
    """Get the wizard recorder from app.state. None when unset."""
    return getattr(request.app.state, "wizard_recorder", None)


@router.post("/test-record", response_model=WizardTestResultResponse)
async def post_wizard_test_record(
    request: Request,
    body: WizardTestRecordRequest,
) -> WizardTestResultResponse:
    """Synchronously record + analyse a 3-second capture.

    The recording happens inside the request — the operator clicks
    "Test Record" and the response arrives 3 s later with the
    analysis. Subsequent ``GET /test-result/{session_id}`` calls
    return the same payload from the in-memory cache.

    Returns 503 when no ``WizardRecorder`` is registered
    (production daemon registers ``SoundDeviceWizardRecorder`` at
    boot; pre-init / tests without injection get the 503).
    """
    recorder = _resolve_recorder(request)
    if recorder is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Voice capture not available — the daemon's wizard "
                "recorder is not registered. Wait for boot to complete "
                "and retry."
            ),
        )

    session_id = secrets.token_urlsafe(16)
    started = time.monotonic()
    recorded_at_iso = datetime.now(UTC).isoformat()

    # Forensic case 2026-05-09: operator's logs_new.txt showed three
    # consecutive ``voice_wizard_test_record_failed`` events with
    # ``PaErrorCode -9985 (paDeviceUnavailable)`` on a Linux host
    # (Razer USB headset, hw:2,0). Each failure timestamp coincided
    # with an active ``voice_test_session_opened`` from the live
    # ``HardwareDetection`` mic-test panel — the same daemon was
    # holding the mic via the live VU stream and the recorder could
    # not open it a second time (ALSA exclusive open).
    #
    # v0.38.0 / F2-H01 — ``SessionRegistry.acquire_exclusive`` holds an
    # asyncio lock for the recorder's ENTIRE lifetime (close_all + the
    # PortAudio open + the capture window + cleanup), and the WS VU
    # subscribe handler refuses new connections while that lock is
    # held (``WS_CLOSE_RECORDER_BUSY`` / RFC 6455 1013). Pre-v0.38.0
    # ``close_all`` released the device, then the asyncio loop yielded
    # and a fresh WS could re-arm the registry before our call to
    # ``recorder.record(...)`` re-opened PortAudio. See audit §3.C and
    # ``device_test/_session.py::SessionRegistry.acquire_exclusive``.
    voice_test_registry = getattr(request.app.state, "voice_test_registry", None)
    from sovyx.voice.device_test import SessionRegistry  # noqa: PLC0415

    duration_s = body.duration_seconds

    async def _run_recorder() -> npt.NDArray[np.float32]:
        return await asyncio.to_thread(
            recorder.record,
            duration_s=duration_s,
            device_id=body.device_id,
        )

    try:
        if isinstance(voice_test_registry, SessionRegistry):
            logger.info(
                "voice_wizard_test_record_session_handoff_begin",
                session_id=session_id,
            )
            async with voice_test_registry.acquire_exclusive(
                role="wizard_test_record",
                ttl_s=duration_s + 0.5,
            ):
                logger.info(
                    "voice_wizard_test_record_session_handoff_done",
                    session_id=session_id,
                )
                samples = await _run_recorder()
        else:
            samples = await _run_recorder()
    except Exception as exc:  # noqa: BLE001
        # T7.27 / T7.28 — translate raw OS error to operator-facing
        # plain-language guidance. Fallback path returns the raw
        # error verbatim (truncated) when the translation table has
        # no match, so operators always see SOMETHING actionable.
        from sovyx.voice._error_messages import translate_audio_error  # noqa: PLC0415

        translation = translate_audio_error(exc)
        logger.warning(
            "voice_wizard_test_record_failed",
            session_id=session_id,
            device_id=body.device_id,
            error=str(exc),
            error_class=translation.error_class.value,
        )
        response = WizardTestResultResponse(
            session_id=session_id,
            success=False,
            duration_actual_s=0.0,
            sample_rate_hz=_TARGET_SAMPLE_RATE,
            level_rms_dbfs=None,
            level_peak_dbfs=None,
            snr_db=None,
            clipping_detected=False,
            silent_capture=True,
            diagnosis="device_error",
            diagnosis_hint=f"{translation.user_message} {translation.actionable_hint}",
            recorded_at_utc=recorded_at_iso,
            error=str(exc),
        )
        _get_session_store(request).put(
            _SessionRecord(
                session_id=session_id,
                response=response,
                created_at_monotonic=started,
            )
        )
        return response

    duration_actual = time.monotonic() - started
    analysis = _analyse_audio(samples, _TARGET_SAMPLE_RATE)
    diagnosis, hint, clipping, silent = _diagnose(
        peak_dbfs=analysis["peak_dbfs"],
        snr_db=analysis["snr_db"],
    )

    response = WizardTestResultResponse(
        session_id=session_id,
        success=True,
        duration_actual_s=duration_actual,
        sample_rate_hz=_TARGET_SAMPLE_RATE,
        level_rms_dbfs=analysis["rms_dbfs"],
        level_peak_dbfs=analysis["peak_dbfs"],
        snr_db=analysis["snr_db"],
        clipping_detected=clipping,
        silent_capture=silent,
        diagnosis=diagnosis,
        diagnosis_hint=hint,
        recorded_at_utc=recorded_at_iso,
        error=None,
    )
    _get_session_store(request).put(
        _SessionRecord(
            session_id=session_id,
            response=response,
            created_at_monotonic=started,
        )
    )
    logger.info(
        "voice_wizard_test_record_complete",
        session_id=session_id,
        diagnosis=diagnosis,
        duration_actual_s=duration_actual,
    )
    return response


# ── T7.23 — test-result by session_id ────────────────────────────────


@router.get(
    "/test-result/{session_id}",
    response_model=WizardTestResultResponse,
)
async def get_wizard_test_result(
    request: Request,
    session_id: str,
) -> WizardTestResultResponse:
    """Re-read a previously-completed test-record result by session id.

    Sessions live in an in-memory LRU cache (last 64 sessions per
    daemon) with a 1-hour TTL; expired or evicted sessions return
    404 + the operator runs ``test-record`` again. Sessions are not
    persisted across daemon restarts.
    """
    if not session_id.strip():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="session_id must be a non-empty string",
        )
    store = _get_session_store(request)
    record = store.get(session_id)
    if record is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"session_id={session_id!r} not found. Sessions expire "
                f"after 1 hour or 64 newer sessions; run /test-record "
                f"again to get a fresh session."
            ),
        )
    return record.response


# ── T7.24 — diagnostic (capture-APO summary) ─────────────────────────


@router.get("/diagnostic", response_model=WizardDiagnosticResponse)
async def get_wizard_diagnostic(request: Request) -> WizardDiagnosticResponse:
    """Wizard-friendly capture diagnostic.

    Distilled view of ``GET /api/voice/capture-diagnostics``: a
    single ``ready: bool`` + ``recommendations: list[str]`` that the
    wizard UI can render directly without parsing the full APO
    endpoint dump. CLI parity:
    ``sovyx doctor voice_capture_apo --json``.

    The full per-endpoint structure remains at the original endpoint
    for the troubleshooting panel and external auditors.
    """
    import sys  # noqa: PLC0415

    platform = sys.platform

    try:
        from sovyx.voice._apo_detector import detect_capture_apos  # noqa: PLC0415

        reports = await asyncio.to_thread(detect_capture_apos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_wizard_diagnostic_apo_scan_failed", error=str(exc))
        reports = []

    voice_clarity_active = any(getattr(r, "voice_clarity_active", False) for r in reports)

    active_device_name: str | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            from sovyx.voice._capture_task import AudioCaptureTask  # noqa: PLC0415

            if registry.is_registered(AudioCaptureTask):
                capture = await registry.resolve(AudioCaptureTask)
                active_device_name = getattr(capture, "input_device_name", None)
        except Exception:  # noqa: BLE001 — best-effort lookup
            active_device_name = None

    recommendations: list[str] = []
    ready = True

    if voice_clarity_active:
        ready = False
        recommendations.append(
            "Windows Voice Clarity APO is active on your microphone. "
            "Sovyx auto-bypasses it via WASAPI exclusive mode. If wake "
            "detection fails, run 'sovyx doctor voice --fix --yes' to "
            "force the bypass."
        )

    if platform == "linux" and not reports:
        # Linux: APO detection is a no-op. We don't have anything to
        # add here that's wizard-friendly without a PulseAudio probe;
        # leave recommendations empty + ready=True.
        pass

    return WizardDiagnosticResponse(
        ready=ready,
        voice_clarity_active=voice_clarity_active,
        active_device_name=active_device_name,
        platform=platform,
        recommendations=recommendations,
    )


# ── Wizard A/B telemetry ingestion (Mission v0.30.1 §T1.2) ──────────


_VALID_STEPS: frozenset[str] = frozenset({"devices", "record", "results", "save", "done"})
"""Wizard step enum — must match the discriminated-union ``WizardStep``
in ``dashboard/src/components/setup-wizard/VoiceSetupWizard.tsx``. Both
metric attributes (step / exit_step) are bounded to this enum so the
OTel scrape series count stays predictable (5 distinct values × 2
metrics × 2 outcomes = ≤ 20 series total)."""

_MAX_DURATION_MS: int = 3_600_000
"""1 h cap on a single step dwell. Anything longer is operator left
the tab open + walked away — telemetry is meaningless beyond the cap
and admitting it would stretch histogram buckets without insight."""


class WizardTelemetryStepDwell(BaseModel):
    """Step-dwell discriminated payload."""

    event: Literal["step_dwell"]
    step: str = Field(description="Wizard step the dwell ended on.")
    duration_ms: int = Field(
        ge=0,
        le=_MAX_DURATION_MS,
        description="Time spent on the step before transitioning.",
    )


class WizardTelemetryCompletion(BaseModel):
    """Completion discriminated payload."""

    event: Literal["completion"]
    outcome: Literal["completed", "abandoned"]
    exit_step: str = Field(description="Step the wizard was on at exit.")


@router.post("/telemetry", status_code=204)
async def emit_wizard_telemetry(
    request: Request,
    body: WizardTelemetryStepDwell | WizardTelemetryCompletion,
) -> None:
    """Record one wizard A/B telemetry event.

    Frontend instrumentation in ``VoiceSetupWizard.tsx`` posts here on
    every step transition (``step_dwell``) and on wizard exit
    (``completion``). The endpoint is best-effort: a 4xx on payload
    errors is informative, but the wizard doesn't block on the
    response. Series cardinality is capped via the ``_VALID_STEPS``
    enum + ``outcome`` literal — operators uploading random strings
    via curl are rejected with 400 before any metric instrument is
    touched.
    """
    if body.event == "step_dwell":
        if body.step not in _VALID_STEPS:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"step must be one of {sorted(_VALID_STEPS)}",
            )
        get_metrics().voice_wizard_step_dwell_ms.record(
            body.duration_ms, attributes={"step": body.step}
        )
    else:
        if body.exit_step not in _VALID_STEPS:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"exit_step must be one of {sorted(_VALID_STEPS)}",
            )
        get_metrics().voice_wizard_completion_rate.add(
            1,
            attributes={
                "outcome": body.outcome,
                "exit_step": body.exit_step,
            },
        )


# ── TTS engine availability (issue #39) ────────────────────────────


class WizardTtsEnginesResponse(BaseModel):
    """Engines the operator can choose from in mind.yaml.

    ``available`` lists the engines whose Python package is importable
    on this host. ``default`` is the auto-detected pick (Piper >
    Kokoro), used when ``MindConfig.voice_tts_engine`` is ``"auto"``.
    """

    available: list[str]
    default: str


@router.get("/tts-engines", response_model=WizardTtsEnginesResponse)
async def list_tts_engines(_request: Request) -> WizardTtsEnginesResponse:
    """List TTS engines available on this host.

    The wizard / settings card surfaces this so the operator only sees
    ``"piper"`` / ``"kokoro"`` as choices when both packages are
    actually installed. ``"auto"`` is always offered (it's the default
    that downgrades gracefully via ``detect_tts_engine``).
    """
    import contextlib  # noqa: PLC0415

    available: list[str] = ["auto"]
    with contextlib.suppress(ImportError):
        __import__("piper_phonemize")
        available.append("piper")
    with contextlib.suppress(ImportError):
        __import__("kokoro_onnx")
        available.append("kokoro")

    from sovyx.voice.model_registry import detect_tts_engine  # noqa: PLC0415

    detected = detect_tts_engine()
    default = detected if detected in {"piper", "kokoro"} else "auto"
    return WizardTtsEnginesResponse(available=available, default=default)


# ── Production recorder (lazy-bound to sounddevice) ─────────────────


class SoundDeviceWizardRecorder:
    """Production :class:`WizardRecorder` backed by ``sounddevice``.

    Daemon registers an instance of this at boot via
    ``app.state.wizard_recorder = SoundDeviceWizardRecorder()``.
    Tests inject a stub instead.

    Resamples to 16 kHz mono inside ``record()`` so callers always
    get the canonical pipeline format regardless of the device's
    native rate.

    v0.35.2 + forensic case 2026-05-09 (operator's logs_new.txt):
    pre-v0.35.2 the recorder called ``sounddevice.rec`` directly,
    bypassing the host-API × sample-rate × channels × auto-convert
    fallback pyramid that the live VU-meter path
    (``SoundDeviceInputSource``) routes through. Linux + PipeWire
    operators on USB headsets routinely got ``PaErrorCode -9985
    paDeviceUnavailable`` from the wizard while the live VU stream
    on the same selected device worked, because the live path went
    through ``open_input_stream`` (which has the fallback chain) and
    the wizard didn't. v0.35.2 unifies the two paths: both now share
    ``open_input_stream`` so they have identical capture semantics
    + identical observability (``voice_opener_attempt`` events).
    """

    def record(
        self,
        *,
        duration_s: float,
        device_id: str | None,
    ) -> npt.NDArray[np.float32]:
        # The route does ``await asyncio.to_thread(recorder.record, ...)``
        # so this method runs on a worker thread without an event loop.
        # ``asyncio.run`` is allowed (and is the canonical way to bridge
        # sync → async on a thread that has no loop). Using a fresh loop
        # per record call is fine: the wizard's call rate is bounded by
        # operator clicks (~1/min worst case).
        return asyncio.run(
            self._record_async(duration_s=duration_s, device_id=device_id),
        )

    async def _record_async(
        self,
        *,
        duration_s: float,
        device_id: str | None,
    ) -> npt.NDArray[np.float32]:
        from sovyx.engine.config import VoiceTuningConfig  # noqa: PLC0415
        from sovyx.voice._stream_opener import (  # noqa: PLC0415
            StreamOpenError,
            open_input_stream,
        )
        from sovyx.voice.device_enum import enumerate_devices  # noqa: PLC0415

        device: int | None = None
        if device_id is not None and device_id.strip():
            try:
                device = int(device_id)
            except ValueError as exc:
                msg = f"device_id must be a numeric PortAudio index; got {device_id!r}"
                raise RuntimeError(msg) from exc

        # Resolve to a live ``DeviceEntry`` with the SAME semantics the
        # live VU path uses (``device_test._source._resolve_input_entry``):
        # match by index, fall back to OS default, then to the first
        # input-capable device. Inlined here instead of importing the
        # private helper so a future split of ``device_test`` doesn't
        # break this consumer (anti-pattern #20).
        entries = enumerate_devices()
        candidates = [e for e in entries if e.max_input_channels > 0]
        if not candidates:
            msg = "No audio input devices available"
            raise RuntimeError(msg)
        entry = None
        if device is not None:
            for candidate in candidates:
                if candidate.index == device:
                    entry = candidate
                    break
        if entry is None:
            defaults = [e for e in candidates if e.is_os_default]
            entry = defaults[0] if defaults else candidates[0]

        # Thread-safe FIFO so the PortAudio callback (driver thread)
        # and the main coroutine (event loop thread) cannot race on
        # the underlying buffer. ``list.append`` is atomic at the
        # bytecode level but the backing-array resize is not — a
        # resize crossing concurrently with a draining iteration could
        # corrupt or crash. ``queue.Queue`` is documented thread-safe;
        # the main coroutine drains after ``stream.stop()``.
        captured_q: queue.Queue[npt.NDArray[np.int16]] = queue.Queue()

        def _callback(
            indata: npt.NDArray[np.int16],
            _frames: int,
            _time: object,
            _status: object,
        ) -> None:
            # PortAudio reuses ``indata`` after the callback returns —
            # copy before storing.
            mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
            captured_q.put_nowait(np.asarray(mono, dtype=np.int16))

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=_TARGET_SAMPLE_RATE,
                blocksize=512,
                callback=_callback,
                tuning=VoiceTuningConfig(),
                dtype="int16",
            )
        except StreamOpenError as exc:
            # Surface the most-recent (deepest) attempt's detail — that
            # is what an operator wants to see ("device busy", "rate not
            # supported", etc.). The attempts list is preserved on the
            # exception for log forensics.
            last = exc.attempts[-1] if exc.attempts else None
            detail = last.error_detail if last and last.error_detail else str(exc)
            msg = f"PortAudio error opening device {device_id!r}: {detail}"
            raise RuntimeError(msg) from exc

        try:
            # Run the stream long enough to collect ``duration_s`` of
            # audio. ``asyncio.sleep`` yields to other tasks; the
            # PortAudio callback fires from its own thread and
            # accumulates into ``captured_q`` without contending with
            # this coroutine.
            await asyncio.sleep(duration_s)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.stop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

        # Drain the thread-safe queue into a list now that the callback
        # thread has been stopped (no more producers).
        captured_frames: list[npt.NDArray[np.int16]] = []
        while True:
            try:
                captured_frames.append(captured_q.get_nowait())
            except queue.Empty:
                break

        actual_rate = info.sample_rate
        target_samples = int(duration_s * actual_rate)
        if not captured_frames:
            return np.zeros(int(duration_s * _TARGET_SAMPLE_RATE), dtype=np.float32)

        joined = np.concatenate(captured_frames)
        # Trim to the exact requested duration. Over-capture is normal
        # because the callback boundary doesn't align with ``duration_s``.
        joined = joined[:target_samples]
        # int16 → float32 in [-1.0, 1.0]
        as_float = joined.astype(np.float32) / 32768.0

        if actual_rate != _TARGET_SAMPLE_RATE:
            # Simple linear resampling — adequate for the wizard's
            # level/SNR analysis. The voice pipeline uses scipy.signal
            # for its own resampling but that's overkill here.
            target_len = int(len(as_float) * _TARGET_SAMPLE_RATE / actual_rate)
            if target_len > 0 and len(as_float) > 1:
                indices = np.linspace(0, len(as_float) - 1, target_len)
                as_float = np.interp(
                    indices,
                    np.arange(len(as_float)),
                    as_float,
                ).astype(np.float32)

        return as_float


# Re-export the protocol so tests can build their own stubs without
# importing private names.
__all__ = [
    "SoundDeviceWizardRecorder",
    "WizardDeviceInfo",
    "WizardDevicesResponse",
    "WizardDiagnosticResponse",
    "WizardRecorder",
    "WizardTestRecordRequest",
    "WizardTestResultResponse",
    "router",
]
