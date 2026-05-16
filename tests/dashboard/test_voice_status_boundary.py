"""Boundary-layer round-trip tests for /api/voice/status typed response.

The Phase 5.D v0.32.7 typed-response migration introduced
``VoiceStatusResponse.model_validate(helper_dict)`` at the route
boundary (commit ``aee85844``); pre-mission tests asserted the
helper dict shape but NEVER the round-trip through the boundary
model. This file closes that coverage gap so future producer ↔
boundary drift surfaces in CI, not in production log noise.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T1.2 (single shape regression) + §T1.3 (parametrised shape lattice).

The C2 root-cause shape is ``capture.input_device=7`` (PortAudio
int handle for the operator's Razer USB at idx=5 on Sony VAIO);
the failing invariant from operator forensic log L1444-L1446 was
``Input should be a valid string [type=string_type, input_value=7,
input_type=int]``. The post-mission union (``int | str | None``)
accepts every prod-realistic shape.
"""

from __future__ import annotations

import pytest

from sovyx.dashboard.routes.voice import VoiceStatusResponse


class TestVoiceStatusBoundaryRoundTrip:
    """C2 regression — boundary accepts the producer's actual prod shape.

    ``AudioCaptureTask`` rebinds ``self._input_device`` to
    ``info.device_index`` (PortAudio int) at ``_capture_task.py:694``
    on every stream open. The boundary MUST accept that int — it is
    the steady-state shape after capture opens, which is the case
    every operator session hits on the second poll onward.
    """

    def test_capture_input_device_int_roundtrip(self) -> None:
        """Prod shape with int input_device validates without raising.

        Replays the exact failing invariant from the v0.43.1 operator
        forensic log L1444-L1446 (``input_value=7, input_type=int``);
        this assertion FAILS on pre-mission HEAD (`str | None` only)
        and passes post-mission (`int | str | None`).
        """
        prod_shape = {
            "pipeline": {"running": True, "state": "idle", "latency_ms": 12.5},
            "capture": {
                "running": True,
                "input_device": 7,  # PortAudio int handle — operator's Razer USB
                "host_api": "ALSA",
                "sample_rate": 16_000,
                "frames_delivered": 1024,
                "last_rms_db": -54.0,
            },
        }
        response = VoiceStatusResponse.model_validate(prod_shape)
        assert response.capture.input_device == 7
        assert response.capture.running is True
        assert response.capture.host_api == "ALSA"

    def test_capture_input_device_str_roundtrip(self) -> None:
        """Operator-picked device name (str variant) still validates."""
        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"running": True, "input_device": "Razer BlackShark V2 Pro"},
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.input_device == "Razer BlackShark V2 Pro"

    def test_capture_input_device_none_roundtrip(self) -> None:
        """OS-default device (None variant) still validates."""
        shape = {
            "pipeline": {"running": False, "state": "not_configured"},
            "capture": {"running": False, "input_device": None},
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.input_device is None

    def test_capture_block_omitted_uses_default_factory(self) -> None:
        """Absent capture block defaults via ``Field(default_factory=...)``.

        Pins the v0.32.7 ``default_factory=VoiceStatusCapture`` contract;
        a future refactor that drops the factory would surface here.
        """
        shape = {"pipeline": {"running": False, "state": "not_configured"}}
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.input_device is None
        assert response.capture.running is False
        assert response.capture.frames_delivered == 0

    def test_capture_extra_field_passthrough(self) -> None:
        """Forward-additive snapshot policy (extra='allow') is intact.

        New SLI fields land in ``status_snapshot()`` without a route
        migration; this test pins ``model_config = {'extra': 'allow'}``
        so a future ``extra='forbid'`` flip surfaces here intentionally.
        """
        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {
                "running": True,
                "input_device": 7,
                "future_sli_field": "ok",
            },
        }
        response = VoiceStatusResponse.model_validate(shape)
        # Pydantic v2 exposes extras via model_extra
        assert response.capture.model_extra is not None
        assert response.capture.model_extra.get("future_sli_field") == "ok"

    @pytest.mark.parametrize(
        ("input_device", "expected"),
        [
            (None, None),
            (0, 0),  # PortAudio idx=0 (OS default on some hosts)
            (7, 7),  # operator's Razer (forensic log L1444 anchor)
            (255, 255),  # upper end of typical enum range
            ("Built-in Microphone", "Built-in Microphone"),
            ("Razer BlackShark V2 Pro USB", "Razer BlackShark V2 Pro USB"),
            ("", ""),  # empty string — boundary accepts (no length floor)
        ],
        ids=[
            "none",
            "int_zero",
            "int_operator_razer",
            "int_upper",
            "str_builtin",
            "str_razer_full",
            "str_empty",
        ],
    )
    def test_input_device_shape_lattice(
        self, input_device: int | str | None, expected: int | str | None
    ) -> None:
        """Every operator-realistic input_device shape round-trips cleanly.

        Anti-pattern #8: assertions use ``==`` value-comparison
        (StrEnum-safe across xdist).
        """
        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"running": True, "input_device": input_device},
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.input_device == expected


class TestVoiceStatusBoundaryNegativeShapes:
    """Boundary correctly rejects shapes that NEITHER producer nor zod accept.

    The widened union is ``int | str | None`` — anything outside that
    set MUST raise ValidationError. These tests ensure the union does
    not over-widen accidentally (e.g., to ``Any``).
    """

    def test_rejects_float_input_device(self) -> None:
        """Float is not in the union; pydantic must reject."""
        from pydantic import ValidationError

        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"input_device": 3.14},
        }
        with pytest.raises(ValidationError) as exc_info:
            VoiceStatusResponse.model_validate(shape)
        assert "input_device" in str(exc_info.value)

    def test_rejects_list_input_device(self) -> None:
        """List is not in the union; pydantic must reject."""
        from pydantic import ValidationError

        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"input_device": [1, 2, 3]},
        }
        with pytest.raises(ValidationError) as exc_info:
            VoiceStatusResponse.model_validate(shape)
        assert "input_device" in str(exc_info.value)

    def test_rejects_dict_input_device(self) -> None:
        """Dict is not in the union; pydantic must reject.

        Guards against a hypothetical PortAudio backend returning a
        host-API tuple wrapped as a dict — would surface here as a
        single targeted ValidationError instead of the pre-mission
        every-poll-500 storm.
        """
        from pydantic import ValidationError

        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"input_device": {"index": 7, "name": "Razer"}},
        }
        with pytest.raises(ValidationError) as exc_info:
            VoiceStatusResponse.model_validate(shape)
        assert "input_device" in str(exc_info.value)
