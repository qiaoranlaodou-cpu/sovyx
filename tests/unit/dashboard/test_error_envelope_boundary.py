"""Boundary tests for the shared ErrorEnvelope SSoT.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.4.

Pins the wire shape + envelope() convenience builder + ErrorCode enum
so a producer rename (or accidental new enum member added without
the zod-twin update) fails CI loud rather than silently surfaces to
the operator as the FastAPI default ``{"detail": str}`` opaque body.
"""

from __future__ import annotations

import pytest

from sovyx.dashboard._error_envelope import ErrorCode, ErrorEnvelope, envelope


class TestErrorEnvelopeBoundary:
    def test_minimal_round_trips(self) -> None:
        instance = ErrorEnvelope.model_validate(
            {
                "error": "Engine not running",
                "error_code": "engine_not_running",
            },
        )
        assert instance.error == "Engine not running"
        assert instance.error_code == ErrorCode.ENGINE_NOT_RUNNING
        assert instance.detail is None
        assert instance.retry_after_seconds is None

    def test_full_round_trips(self) -> None:
        instance = ErrorEnvelope.model_validate(
            {
                "error": "LLM router unavailable",
                "error_code": "service_unavailable",
                "detail": "Discovery report not yet primed",
                "retry_after_seconds": 30,
            },
        )
        assert instance.error_code == ErrorCode.SERVICE_UNAVAILABLE
        assert instance.detail == "Discovery report not yet primed"
        assert instance.retry_after_seconds == 30

    def test_unknown_error_code_rejected(self) -> None:
        with pytest.raises(Exception) as exc_info:
            ErrorEnvelope.model_validate(
                {
                    "error": "x",
                    "error_code": "never_a_code",
                },
            )
        # xdist-safe exception assertion (anti-pattern #8)
        assert type(exc_info.value).__name__ == "ValidationError"

    def test_retry_after_negative_rejected(self) -> None:
        with pytest.raises(Exception) as exc_info:
            ErrorEnvelope.model_validate(
                {
                    "error": "x",
                    "error_code": "internal",
                    "retry_after_seconds": -1,
                },
            )
        assert type(exc_info.value).__name__ == "ValidationError"

    def test_retry_after_over_max_rejected(self) -> None:
        with pytest.raises(Exception) as exc_info:
            ErrorEnvelope.model_validate(
                {
                    "error": "x",
                    "error_code": "internal",
                    "retry_after_seconds": 86401,
                },
            )
        assert type(exc_info.value).__name__ == "ValidationError"

    def test_passthrough_preserves_unknown_top_level(self) -> None:
        instance = ErrorEnvelope.model_validate(
            {
                "error": "x",
                "error_code": "internal",
                "correlation_id": "abc-123",
            },
        )
        # extra="allow" keeps the unknown field accessible via model_extra
        assert instance.model_extra is not None
        assert instance.model_extra.get("correlation_id") == "abc-123"


class TestEnvelopeBuilder:
    def test_minimal(self) -> None:
        payload = envelope("X", ErrorCode.INTERNAL)
        assert payload == {"error": "X", "error_code": "internal"}

    def test_with_detail(self) -> None:
        payload = envelope("X", ErrorCode.INTERNAL, detail="trace")
        assert payload["detail"] == "trace"

    def test_with_retry_after(self) -> None:
        payload = envelope(
            "X",
            ErrorCode.SERVICE_UNAVAILABLE,
            retry_after_seconds=60,
        )
        assert payload["retry_after_seconds"] == 60

    def test_builder_output_round_trips_through_model(self) -> None:
        """The dict produced by envelope() MUST validate cleanly through
        ErrorEnvelope.model_validate so HTTPException(detail=envelope(...))
        consumers see the SAME shape boundary tests assert."""
        payload = envelope(
            "Registry unavailable",
            ErrorCode.REGISTRY_UNAVAILABLE,
            detail="awaiting bootstrap",
            retry_after_seconds=15,
        )
        instance = ErrorEnvelope.model_validate(payload)
        assert instance.error_code == ErrorCode.REGISTRY_UNAVAILABLE
        assert instance.retry_after_seconds == 15


class TestErrorCodeEnumMembership:
    """Pin the current ErrorCode set so a zod-twin desync fails loud.

    If a new ErrorCode member is added to the pydantic enum without
    updating the zod ErrorCodeSchema enum (and the i18n key set for
    the dashboard error banner), this assertion is the breakwater.
    The zod twin lives at
    ``dashboard/src/types/schemas.ts::ErrorCodeSchema`` — the value
    list MUST be the same as ``ErrorCode.__members__.values()``.
    """

    _EXPECTED_VALUES = {
        "bad_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "unprocessable",
        "too_many_requests",
        "internal",
        "service_unavailable",
        "gateway_timeout",
        "engine_not_running",
        "registry_unavailable",
        "dependency_unavailable",
    }

    def test_enum_membership_matches_zod_twin(self) -> None:
        actual = {member.value for member in ErrorCode}
        assert actual == self._EXPECTED_VALUES, (
            "ErrorCode enum drifted; update the zod twin at "
            "dashboard/src/types/schemas.ts::ErrorCodeSchema + "
            "the i18n key set for the dashboard error banner in "
            "lockstep (anti-pattern #57 / #56)."
        )
