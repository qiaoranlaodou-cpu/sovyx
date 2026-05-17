"""Boundary round-trip tests for ``EngineDegradedResponse``.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.6 + §T1.13.

Quality Gate 8 (Mission C2 §T4.1 static checker) requires every
``Model.model_validate(helper_dict)`` call at a route boundary to have
a paired round-trip test that exercises the producer's actual
in-memory shape. The composite ``/api/engine/degraded`` endpoint is
populated from :class:`sovyx.engine._degraded_store.EngineDegradedStore`
which now has three producer sites (Phase 1 §T1.2 / §T1.3 / §T1.4); this
file pins the contract for the empty case + each axis solo + the
3-axis composite + the future-additive case.

Forward-additive (``model_config = {"extra": "allow"}``) is preserved
— a Phase 3 ack field MUST NOT break the round-trip.
"""

from __future__ import annotations

from sovyx.dashboard.routes.engine_degraded import (
    EngineDegradedResponse,
    _compute_composite_severity,
)
from tests.dashboard._boundary_helpers import assert_boundary_accepts


def _empty_payload() -> dict[str, object]:
    return {
        "axes": [],
        "composite_severity": None,
        "composite_axis_count": 0,
        "ack": {"acked": False},
    }


def _voice_axis() -> dict[str, object]:
    return {
        "axis": "voice",
        "reason": "failover_ladder_exhausted",
        "severity": "error",
        "title_token": "degraded.voice.ladderExhausted.title",
        "body_token": "degraded.voice.ladderExhausted.body",
        "action_chips": [
            {
                "label_token": "degraded.voice.ladderExhausted.viewHistory",
                "action": "navigate",
                "target": "/voice/health",
                "style": "primary",
            },
        ],
        "metadata": {
            "candidates_unreachable": ["razer-usb", "pipewire-default"],
            "candidates_tried": 2,
            "ladder_id": "abc123def456",
        },
        "first_observed_monotonic": 1.0,
        "last_observed_monotonic": 1.5,
        "occurrence_count": 1,
    }


def _llm_axis() -> dict[str, object]:
    return {
        "axis": "llm",
        "reason": "no_llm_provider",
        "severity": "error",
        "title_token": "degraded.llm.noProvider.title",
        "body_token": "degraded.llm.noProvider.body",
        "action_chips": [
            {
                "label_token": "degraded.llm.noProvider.installOllama",
                "action": "external_link",
                "target": "https://ollama.ai",
                "style": "primary",
            },
        ],
        "metadata": {
            "checked_keys": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
            "ollama_available": False,
        },
        "first_observed_monotonic": 0.1,
        "last_observed_monotonic": 0.1,
        "occurrence_count": 1,
    }


def _stt_axis() -> dict[str, object]:
    return {
        "axis": "stt",
        "reason": "stt_language_coerced",
        "severity": "warn",
        "title_token": "degraded.stt.languageCoerced.title",
        "body_token": "degraded.stt.languageCoerced.body",
        "action_chips": [
            {
                "label_token": "degraded.stt.languageCoerced.switchToEnglish",
                "action": "navigate",
                "target": "/settings/voice",
                "style": "default",
            },
        ],
        "metadata": {
            "requested_language": "pt",
            "coerced_language": "en",
        },
        "first_observed_monotonic": 0.5,
        "last_observed_monotonic": 0.5,
        "occurrence_count": 1,
    }


class TestEngineDegradedResponseBoundary:
    """``/api/engine/degraded`` accepts every realistic producer shape."""

    def test_empty_payload_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=_empty_payload,
            field_assertions={
                "composite_severity": None,
                "composite_axis_count": 0,
            },
        )
        assert response.axes == []
        assert response.ack.acked is False

    def test_single_voice_axis_warn_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis()],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "warn",
                "composite_axis_count": 1,
            },
        )
        assert response.axes[0].axis == "voice"
        assert response.axes[0].action_chips[0].action == "navigate"

    def test_two_axis_error_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis(), _llm_axis()],
                "composite_severity": "error",
                "composite_axis_count": 2,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "error",
                "composite_axis_count": 2,
            },
        )
        assert {a.axis for a in response.axes} == {"voice", "llm"}

    def test_three_axis_critical_replays_operator_session(self) -> None:
        """Mission C4 §T1.13 — the canonical L374 + L858 + L1063
        operator-session composite. Severity escalates to critical."""
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis(), _llm_axis(), _stt_axis()],
                "composite_severity": "critical",
                "composite_axis_count": 3,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "critical",
                "composite_axis_count": 3,
            },
        )
        assert {a.axis for a in response.axes} == {"voice", "llm", "stt"}

    def test_ack_state_populated_round_trips(self) -> None:
        """Phase 3 ack state shape MUST round-trip cleanly even though
        Phase 1 doesn't write to it. Forward-additive contract."""
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis()],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {
                    "acked": True,
                    "acked_at_ts": 1700000000,
                    "ttl_sec": 3600,
                    "ttl_remaining_sec": 3540,
                    "operator_id": "op-hash-123",
                },
            },
        )
        assert response.ack.acked is True
        assert response.ack.ttl_remaining_sec == 3540

    def test_future_axis_passes_through(self) -> None:
        """Mission C4 §16 — future axes (brain, bridges, plugin) extend
        the payload without a schema migration thanks to extra-allow."""
        future_axis = {
            "axis": "brain",
            "reason": "embedding_model_unavailable",
            "severity": "warn",
            "title_token": "degraded.brain.embedding.title",
            "body_token": "degraded.brain.embedding.body",
            "action_chips": [],
            "metadata": {},
            "first_observed_monotonic": 0.0,
            "last_observed_monotonic": 0.0,
            "occurrence_count": 1,
            "future_extra_field": "tolerated",
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [future_axis],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
        )
        assert response.axes[0].axis == "brain"

    def test_extra_top_level_field_passes_through(self) -> None:
        """Phase 2 may add governor counters at the top level. Forward-
        additive."""
        assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [],
                "composite_severity": None,
                "composite_axis_count": 0,
                "ack": {"acked": False},
                "phase2_governor_state": {
                    "auto_restart_count": 0,
                    "max_retries": 3,
                },
            },
        )


class TestComputeCompositeSeverity:
    """Mission C4 §T1.6 — ADR-D6 severity escalation invariants."""

    def test_zero_axes_none(self) -> None:
        assert _compute_composite_severity(0) is None

    def test_zero_axes_negative_defensive(self) -> None:
        # Defensive — caller should never pass negative, but the
        # implementation MUST short-circuit cleanly.
        assert _compute_composite_severity(-1) is None

    def test_one_axis_warn(self) -> None:
        assert _compute_composite_severity(1) == "warn"

    def test_two_axes_error(self) -> None:
        assert _compute_composite_severity(2) == "error"

    def test_three_axes_critical(self) -> None:
        assert _compute_composite_severity(3) == "critical"

    def test_many_axes_critical(self) -> None:
        assert _compute_composite_severity(8) == "critical"
