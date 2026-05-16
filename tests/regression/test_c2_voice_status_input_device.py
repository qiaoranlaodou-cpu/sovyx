"""C2 regression — replay operator forensic log shape verbatim.

Mission C2 — closes the 16-block ValidationError storm observed
across the operator's v0.43.1 session at lines 1316, 1583, 1870,
2134, 2399, 2661, 2924, 3186 of ``docs_teste.txt``. Each failure
emitted the identical invariant at L1444-L1446::

    capture.input_device — Input should be a valid string
        [type=string_type, input_value=7, input_type=int]

Pre-mission: ``VoiceStatusCapture.input_device: str | None``.
Post-mission: ``VoiceStatusCapture.input_device: int | str | None``.

This regression test is structurally a duplicate of
``tests/dashboard/test_voice_status_boundary.py::test_capture_input_device_int_roundtrip``;
the duplication is INTENTIONAL — the boundary test lives in
``tests/dashboard/`` and verifies the contract, while this
regression test lives in ``tests/regression/`` and verifies the
specific historical incident. Future grep ``grep -r "C2"
tests/regression/`` surfaces this file as the named anchor for the
incident.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§9.4.
"""

from __future__ import annotations

from sovyx.dashboard.routes.voice import VoiceStatusResponse


def test_c2_operator_log_l1444_replay() -> None:
    """v0.43.1 operator forensic log L1444 shape validates cleanly.

    The exact prod shape: Sony VAIO VJFE69F11X + Linux Mint + Razer
    BlackShark V2 Pro USB at PortAudio idx=5, post stream open with
    ``info.device_index`` rebound at ``_capture_task.py:694`` to the
    int handle (7 in the forensic capture, varies per host).
    """
    # Reconstructed from L996 (capture_integrity_probe_complete) +
    # L972-L983 (audio_capture_heartbeat) of the operator log.
    operator_log_shape = {
        "pipeline": {
            "running": True,
            "state": "listening",
            "latency_ms": 22.5,
        },
        "capture": {
            "running": True,
            "input_device": 7,  # ← the historical int that 500'd every poll
            "host_api": "ALSA",
            "sample_rate": 16_000,
            "frames_delivered": 50,
            "last_rms_db": -54.4,
        },
        "stt": {"engine": "MoonshineSTT", "model": "moonshine-tiny", "state": "ready"},
        "tts": {"engine": "PiperTTS", "model": "pt_BR-faber-medium", "initialized": True},
        "wake_word": {"enabled": False, "phrase": None},
        "vad": {"enabled": True},
        "wyoming": {"connected": False, "endpoint": None},
        "hardware": {"tier": "MINI_PC", "ram_mb": 8192},
        "preflight_warnings": [],
    }
    # MUST NOT raise — pre-mission this raised ValidationError 8x per
    # poll × 2 logged traces = the 16-block storm.
    response = VoiceStatusResponse.model_validate(operator_log_shape)
    assert response.pipeline.running is True
    assert response.capture.input_device == 7
    assert response.capture.host_api == "ALSA"
    assert response.capture.last_rms_db == -54.4
    assert response.preflight_warnings == []
