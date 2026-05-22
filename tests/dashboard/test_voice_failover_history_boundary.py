"""Boundary round-trip tests for ``FailoverHistoryResponse``.

Mission C3 §T2.9 — Quality Gate 8 compliance for the new
``/api/voice/health/failover-history`` endpoint. Pins the producer
→ typed-boundary contract so a future drift surfaces in CI rather
than in operator log noise (mirrors the Mission C2 §T1.2 pattern
that closed the ``VoiceStatusResponse.capture.input_device`` regression).
"""

from __future__ import annotations

from functools import partial

from sovyx.dashboard.routes.voice_health import (
    FailoverHistoryEntryModel,
    FailoverHistoryResponse,
)
from tests.dashboard._boundary_helpers import assert_boundary_accepts


def _candidate_dict(
    *,
    index: int,
    verdict: str,
    target_endpoint: str = "dev-canonical",
    error_class: str = "",
    elapsed_ms: int | None = 50,
    skipped_reason: str | None = None,
) -> dict[str, object]:
    return {
        "index": index,
        "target_endpoint": target_endpoint,
        "target_friendly_name": target_endpoint.replace("-", " "),
        "verdict": verdict,
        "error_class": error_class,
        "error_detail": "",
        "elapsed_ms": elapsed_ms,
        "skipped_reason": skipped_reason,
    }


def _entry_dict(
    *,
    ladder_id: str = "abc123def456",
    verdict: str = "succeeded",
    candidates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "ladder_id": ladder_id,
        "started_monotonic": 1000.0,
        "completed_monotonic": 1001.5,
        "verdict": verdict,
        "candidates_tried": len(candidates or []),
        "succeeded_index": 0 if verdict == "succeeded" else None,
        "candidates": candidates or [],
        "from_endpoint": "razer-blackshark-v2-pro",
        "elapsed_ms": 1500,
        "derived_reason": "vad_frontend_dead",
        "mind_id": "jonny",
    }


def _response_payload(
    *,
    entries: list[dict[str, object]],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Mirror ``get_voice_failover_history`` emit shape
    (``voice_health.py:717``) — ``FailoverHistoryResponse(entries=...,
    ring_capacity=history.capacity)``. ``ring_capacity`` is the
    runtime-bound ``VoiceTuningConfig.failover_history_ring_capacity``
    (default 32 — see ``FailoverHistoryResponse`` docstring at
    ``voice_health.py:411``). ``extra`` exists so forward-additive
    probes can layer unknown keys without forking the helper.
    """
    payload: dict[str, object] = {"entries": entries, "ring_capacity": 32}
    if extra:
        payload.update(extra)
    return payload


class TestFailoverHistoryResponseRoundTrip:
    """Pin the ``FailoverHistoryResponse.model_validate(...)`` boundary."""

    def test_empty_history_round_trip(self) -> None:
        """Fresh-boot state — no ladder has yet run."""
        assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(_response_payload, entries=[]),
            field_assertions={"ring_capacity": 32},
        )

    def test_succeeded_single_candidate_round_trip(self) -> None:
        """Most common shape — one candidate, engaged=True."""
        candidate = _candidate_dict(index=0, verdict="succeeded")
        entry = _entry_dict(candidates=[candidate])
        assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(_response_payload, entries=[entry]),
        )

    def test_exhausted_multi_candidate_round_trip(self) -> None:
        """3-candidate exhausted ladder — replays the operator's
        L1015-L1063 scenario as if no recovery had occurred.
        """
        candidates = [
            _candidate_dict(
                index=0,
                verdict="failed",
                target_endpoint="hd-audio-generic",
                error_class="unopenable_this_boot",
            ),
            _candidate_dict(
                index=1,
                verdict="failed",
                target_endpoint="pipewire-virtual",
                error_class="unopenable_this_boot",
            ),
            _candidate_dict(
                index=2,
                verdict="failed",
                target_endpoint="os-default",
                error_class="transient_retryable_same_device",
            ),
        ]
        entry = _entry_dict(verdict="exhausted", candidates=candidates)
        validated = assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(_response_payload, entries=[entry]),
        )
        # field_assertions doesn't support list-indexed paths; assert
        # directly on the validated instance.
        assert validated.entries[0].verdict == "exhausted"
        assert len(validated.entries[0].candidates) == 3  # noqa: PLR2004

    def test_skipped_candidate_round_trip(self) -> None:
        """Candidate skipped via probe-cache short-circuit."""
        skipped = _candidate_dict(
            index=0,
            verdict="skipped",
            error_class="unopenable_this_boot",
            elapsed_ms=None,
            skipped_reason="probe_cache_unopenable",
        )
        good = _candidate_dict(index=1, verdict="succeeded")
        entry = _entry_dict(candidates=[skipped, good])
        validated = assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(_response_payload, entries=[entry]),
        )
        assert validated.entries[0].candidates[0].skipped_reason == "probe_cache_unopenable"

    def test_in_progress_entry_round_trip(self) -> None:
        """Mid-flight record — completed_monotonic None, verdict
        ``in_progress``. Surfaced when an operator hits the endpoint
        between ladder_started and ladder_complete (race window).
        """
        candidate = _candidate_dict(index=0, verdict="failed", elapsed_ms=200)
        entry = _entry_dict(
            verdict="in_progress",
            candidates=[candidate],
        )
        entry["completed_monotonic"] = None
        entry["elapsed_ms"] = None
        entry["succeeded_index"] = None
        assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(_response_payload, entries=[entry]),
        )

    def test_extra_keys_pass_through(self) -> None:
        """Forward-additive — unknown fields in the entry/candidate
        MUST NOT cause a ValidationError.
        """
        candidate = _candidate_dict(index=0, verdict="succeeded")
        candidate["future_field"] = "banner_dismissed"
        entry = _entry_dict(candidates=[candidate])
        entry["future_entry_field"] = 99
        assert_boundary_accepts(
            FailoverHistoryResponse,
            helper_factory=partial(
                _response_payload,
                entries=[entry],
                extra={"future_response_field": True},
            ),
        )


class TestFailoverHistoryEntryModelDirect:
    """Direct ``FailoverHistoryEntryModel.model_validate`` smoke."""

    def test_minimal_shape(self) -> None:
        instance = FailoverHistoryEntryModel.model_validate(
            {
                "ladder_id": "id-1",
                "started_monotonic": 1.0,
                "verdict": "succeeded",
            },
        )
        assert instance.ladder_id == "id-1"
        assert instance.verdict == "succeeded"
        assert instance.candidates == []
        assert instance.from_endpoint == ""
