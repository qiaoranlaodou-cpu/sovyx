"""Tests for ``sovyx.voice.health._failover_error_classifier``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.3.

Pin every entry of every code-table + the detail-string fallback +
the ``UNKNOWN`` default + the ``is_skip_candidate_class`` predicate.
The classifier is a pure function so the tests are synchronous +
deterministic.
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._failover_error_classifier import (
    FailoverErrorClass,
    classify_error_code,
    is_skip_candidate_class,
)


class TestClassifyPermanent:
    """``-9996 paInvalidDevice`` family → ``UNOPENABLE_PERMANENT``."""

    @pytest.mark.parametrize(
        "code",
        [
            "-9996",
            "paInvalidDevice",
            "PAINVALIDDEVICE",
            "  -9996  ",
        ],
    )
    def test_invalid_device_codes(self, code: str) -> None:
        assert classify_error_code(code) is FailoverErrorClass.UNOPENABLE_PERMANENT

    def test_final_code_device_disconnected(self) -> None:
        assert (
            classify_error_code("device_disconnected") is FailoverErrorClass.UNOPENABLE_PERMANENT
        )

    def test_detail_string_fallback_invalid_device(self) -> None:
        assert (
            classify_error_code("", error_detail="The OS reported invalid device #5")
            is FailoverErrorClass.UNOPENABLE_PERMANENT
        )


class TestClassifyThisBoot:
    """``-9985 paDeviceUnavailable`` + ``-9988 paBadIODeviceCombination``
    + opener final-codes → ``UNOPENABLE_THIS_BOOT``.
    """

    @pytest.mark.parametrize(
        "code",
        [
            "-9985",
            "paDeviceUnavailable",
            "PADEVICEUNAVAILABLE",
            "-9988",
            "paBadIODeviceCombination",
        ],
    )
    def test_unavailable_codes(self, code: str) -> None:
        assert classify_error_code(code) is FailoverErrorClass.UNOPENABLE_THIS_BOOT

    @pytest.mark.parametrize(
        "final_code",
        [
            "device_not_found",
            "device_unavailable",
            "permission_denied",
            "service_not_running",
        ],
    )
    def test_final_code_mnemonics(self, final_code: str) -> None:
        assert classify_error_code(final_code) is FailoverErrorClass.UNOPENABLE_THIS_BOOT

    def test_detail_string_fallback_device_unavailable(self) -> None:
        # Operator log L1054 free-text shape.
        assert (
            classify_error_code(
                "",
                error_detail="AlsaOpen failed: device unavailable on hw:1,0",
            )
            is FailoverErrorClass.UNOPENABLE_THIS_BOOT
        )


class TestClassifyFormatRetry:
    """Sample-rate / format-mismatch family — opener handles via
    permutation pyramid, failover layer DOES NOT skip the device.
    """

    @pytest.mark.parametrize(
        "code",
        [
            "-9986",
            "paInvalidSampleRate",
            "audclnt_e_unsupported_format",
            "0x88890008",
            "-2004287480",
        ],
    )
    def test_unsupported_format_codes(self, code: str) -> None:
        assert classify_error_code(code) is FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE

    @pytest.mark.parametrize(
        "final_code",
        [
            "unsupported_format",
            "buffer_size_error",
            "exclusive_mode_denied",
        ],
    )
    def test_final_code_mnemonics(self, final_code: str) -> None:
        assert classify_error_code(final_code) is FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE

    def test_detail_string_fallback_invalid_sample_rate(self) -> None:
        assert (
            classify_error_code("", error_detail="PortAudio: invalid sample rate")
            is FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE
        )

    def test_detail_string_fallback_format_not_supported(self) -> None:
        assert (
            classify_error_code("", error_detail="WASAPI: format not supported")
            is FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE
        )


class TestClassifyTransient:
    """Exclusive-lock contention + one-off host-API hiccup —
    retryable on the SAME device (host-API rotation, shared-mode).
    """

    @pytest.mark.parametrize(
        "code",
        [
            "-9999",
            "paUnanticipatedHostError",
            "audclnt_e_device_in_use",
            "0x8889000a",
            "-2004287478",
        ],
    )
    def test_transient_codes(self, code: str) -> None:
        assert classify_error_code(code) is FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE

    @pytest.mark.parametrize(
        "final_code",
        [
            "device_in_use",
            "device_busy",
            "driver_failure",
        ],
    )
    def test_final_code_mnemonics(self, final_code: str) -> None:
        assert (
            classify_error_code(final_code) is FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE
        )

    @pytest.mark.parametrize(
        "detail",
        [
            "device is busy",
            "device in use",
            "device or resource busy",
        ],
    )
    def test_detail_string_fallback(self, detail: str) -> None:
        assert (
            classify_error_code("", error_detail=detail)
            is FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE
        )


class TestClassifyUnknown:
    """Opaque / empty inputs default to ``UNKNOWN`` (conservative —
    the failover loop does NOT skip on unknown).
    """

    def test_empty_inputs(self) -> None:
        assert classify_error_code("") is FailoverErrorClass.UNKNOWN
        assert classify_error_code("", "") is FailoverErrorClass.UNKNOWN

    def test_unrecognized_numeric_code(self) -> None:
        assert classify_error_code("-12345") is FailoverErrorClass.UNKNOWN

    def test_unrecognized_text_token(self) -> None:
        assert classify_error_code("paSomeFutureCode") is FailoverErrorClass.UNKNOWN

    def test_unrelated_detail_text(self) -> None:
        assert (
            classify_error_code("", error_detail="some unrelated free-text message")
            is FailoverErrorClass.UNKNOWN
        )

    def test_robust_against_none_like_inputs(self) -> None:
        """Empty-string + whitespace inputs MUST NOT crash."""
        assert classify_error_code("   ", "   ") is FailoverErrorClass.UNKNOWN


class TestIsSkipCandidateClass:
    """``is_skip_candidate_class`` encapsulates ADR-D4 — only
    ``UNOPENABLE_*`` classes mean "skip this candidate now".
    """

    @pytest.mark.parametrize(
        ("cls", "expected"),
        [
            (FailoverErrorClass.UNOPENABLE_PERMANENT, True),
            (FailoverErrorClass.UNOPENABLE_THIS_BOOT, True),
            (FailoverErrorClass.TRANSIENT_RETRYABLE_SAME_DEVICE, False),
            (FailoverErrorClass.FORMAT_RETRYABLE_SAME_DEVICE, False),
            (FailoverErrorClass.UNKNOWN, False),
        ],
    )
    def test_skip_predicate(
        self,
        cls: FailoverErrorClass,
        expected: bool,
    ) -> None:
        assert is_skip_candidate_class(cls) is expected


class TestEnumContract:
    """Anti-pattern #9 — ``StrEnum`` value-based comparison +
    xdist-safe identity.
    """

    def test_is_str_enum(self) -> None:
        assert isinstance(FailoverErrorClass.UNOPENABLE_PERMANENT.value, str)
        # String-equality should hold (StrEnum contract).
        assert FailoverErrorClass.UNOPENABLE_PERMANENT == "unopenable_permanent"

    def test_all_members_have_distinct_values(self) -> None:
        values = {m.value for m in FailoverErrorClass}
        assert len(values) == len(list(FailoverErrorClass))
