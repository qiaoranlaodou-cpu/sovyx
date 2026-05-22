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

from functools import partial
from typing import Any

from sovyx.dashboard.routes.engine_degraded import (
    EngineDegradedResponse,
    _compute_composite_severity,
    _compute_composite_severity_hybrid,
    _max_per_axis_severity,
    _normalize_severity,
)
from sovyx.engine._degraded_store import DegradedEntry
from tests.dashboard._boundary_helpers import assert_boundary_accepts


def _empty_payload() -> dict[str, object]:
    return {
        "axes": [],
        "composite_severity": None,
        "composite_axis_count": 0,
        "ack": {"acked": False},
    }


def _response_payload(
    *,
    axes: list[dict[str, Any]],
    composite_severity: str | None,
    composite_axis_count: int,
    ack: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mirror ``get_engine_degraded`` emit shape — the route builds
    ``EngineDegradedResponse(axes=..., composite_severity=...,
    composite_axis_count=..., ack=...)`` (see
    ``engine_degraded.py:519+``). ``ack`` defaults to the un-acked
    shape (``{"acked": False}``) the producer emits for fresh state;
    ``extra`` carries forward-additive keys (Phase 2 governor state,
    D.1 ``composite_max_severity``, future ack fields) without
    forking the helper.
    """
    payload: dict[str, Any] = {
        "axes": axes,
        "composite_severity": composite_severity,
        "composite_axis_count": composite_axis_count,
        "ack": ack if ack is not None else {"acked": False},
    }
    if extra:
        payload.update(extra)
    return payload


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
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis()],
                composite_severity="warn",
                composite_axis_count=1,
            ),
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
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis(), _llm_axis()],
                composite_severity="error",
                composite_axis_count=2,
            ),
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
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis(), _llm_axis(), _stt_axis()],
                composite_severity="critical",
                composite_axis_count=3,
            ),
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
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis()],
                composite_severity="warn",
                composite_axis_count=1,
                ack={
                    "acked": True,
                    "acked_at_ts": 1700000000,
                    "ttl_sec": 3600,
                    "ttl_remaining_sec": 3540,
                    "operator_id": "op-hash-123",
                },
            ),
        )
        assert response.ack.acked is True
        assert response.ack.ttl_remaining_sec == 3540


# ── Mission C6 §T2.8 — refined `axis="llm"` reason taxonomy ─────────────────
# Quality Gate 8 (anti-pattern #40) requires every new reason value at the
# composite endpoint to have a paired round-trip test. The pre-C6 single
# `no_llm_provider` reason is covered above (legacy dual-emission); these
# tests cover the 6 new refined tokens.


def _llm_axis_with_reason(
    reason: str,
    severity: str,
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Helper — build a minimal `axis="llm"` payload with a refined reason."""
    return {
        "axis": "llm",
        "reason": reason,
        "severity": severity,
        "title_token": f"degraded.llm.{reason}.title",
        "body_token": f"degraded.llm.{reason}.body",
        "action_chips": [
            {
                "label_token": f"degraded.llm.{reason}.runDoctor",
                "action": "external_link",
                "target": "https://sovyx.dev/docs/cli/llm-doctor",
                "style": "default",
            },
        ],
        "metadata": metadata
        or {
            "verdict": reason,
            "configured_count": 0,
            "available_count": 0,
            "default_provider": "",
            "default_model": "",
            "scan_duration_ms": 0.5,
        },
        "first_observed_monotonic": 1.0,
        "last_observed_monotonic": 1.0,
        "occurrence_count": 1,
    }


class TestEngineDegradedC6ReasonTaxonomy:
    """Boundary round-trip pairs for the 7 refined Mission C6 reason tokens."""

    def test_no_provider_configured_round_trips(self) -> None:
        axis = _llm_axis_with_reason("no_provider_configured", "critical")
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "no_provider_configured"

    def test_ollama_unreachable_round_trips(self) -> None:
        axis = _llm_axis_with_reason("ollama_unreachable", "error")
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "ollama_unreachable"

    def test_ollama_no_models_round_trips(self) -> None:
        axis = _llm_axis_with_reason("ollama_no_models", "warn")
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "ollama_no_models"

    def test_cloud_key_invalid_round_trips(self) -> None:
        axis = _llm_axis_with_reason(
            "cloud_key_invalid",
            "error",
            metadata={
                "verdict": "cloud_key_invalid",
                "configured_count": 2,
                "available_count": 0,
                "default_provider": "",
                "default_model": "",
                "scan_duration_ms": 0.5,
                "invalid_providers": ["anthropic", "openai"],
            },
        )
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "cloud_key_invalid"
        assert response.axes[0].metadata["invalid_providers"] == [
            "anthropic",
            "openai",
        ]

    def test_all_providers_unhealthy_round_trips(self) -> None:
        axis = _llm_axis_with_reason("all_providers_unhealthy", "error")
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "all_providers_unhealthy"

    def test_default_model_unavailable_round_trips(self) -> None:
        axis = _llm_axis_with_reason("default_model_unavailable", "error")
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "default_model_unavailable"

    def test_partial_health_round_trips(self) -> None:
        axis = _llm_axis_with_reason(
            "partial_health",
            "warn",
            metadata={
                "verdict": "partial_health",
                "configured_count": 2,
                "available_count": 1,
                "default_provider": "anthropic",
                "default_model": "claude-sonnet-4-6",
                "scan_duration_ms": 0.5,
                "healthy_providers": ["anthropic"],
                "unhealthy_providers": ["openai"],
            },
        )
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].reason == "partial_health"
        assert response.axes[0].metadata["healthy_providers"] == ["anthropic"]

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
            helper_factory=partial(
                _response_payload,
                axes=[future_axis],
                composite_severity="warn",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].axis == "brain"

    def test_extra_top_level_field_passes_through(self) -> None:
        """Phase 2 may add governor counters at the top level. Forward-
        additive."""
        assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[],
                composite_severity=None,
                composite_axis_count=0,
                extra={
                    "phase2_governor_state": {
                        "auto_restart_count": 0,
                        "max_retries": 3,
                    },
                },
            ),
        )

    def test_c5_dashboard_axis_bundle_partial_round_trips(self) -> None:
        """Mission C5 §T2.3 — the new ``axis="dashboard"`` entry with
        ``reason="bundle_partial"`` round-trips through the existing
        forward-additive schema without a migration. Proves the C4
        contract holds for the 4th axis-consumer.
        """
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_partial",
            "severity": "error",
            "title_token": "degraded.dashboard.bundle_partial.title",
            "body_token": "degraded.dashboard.bundle_partial.partial.body",
            "action_chips": [
                {
                    "label_token": "degraded.dashboard.reinstall",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/install/troubleshooting#reinstall",
                    "style": "primary",
                },
                {
                    "label_token": "degraded.dashboard.runDoctor",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/cli/doctor#dashboard",
                    "style": "default",
                },
            ],
            "metadata": {
                "verdict": "partial",
                "missing_count": 3,
                "missing_sample": [
                    "assets/dashboard-BLNxX04a.js",
                    "assets/api-CmBjhza2.js",
                    "assets/index-DIHUuQiC.js",
                ],
                "static_dir": "/home/op/.local/share/pipx/venvs/sovyx/lib/python3.12/site-packages/sovyx/dashboard/static",
                "scan_duration_ms": 4.213,
            },
            "first_observed_monotonic": 1.5,
            "last_observed_monotonic": 1.5,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[dashboard_axis],
                composite_severity="error",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].axis == "dashboard"
        assert response.axes[0].reason == "bundle_partial"
        assert response.axes[0].severity == "error"
        assert response.axes[0].metadata["verdict"] == "partial"

    def test_c5_dashboard_axis_bundle_missing_round_trips(self) -> None:
        """Mission C5 §T2.3 — ``reason="bundle_missing"`` with the
        full critical-severity treatment + verdict-discriminated body
        token (the same reason covers
        INDEX_HTML_MISSING / STATIC_DIR_MISSING / LEGACY_INDEX_HTML_NO_ASSETS
        per the spec — the verdict carries on metadata).
        """
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_missing",
            "severity": "critical",
            "title_token": "degraded.dashboard.bundle_missing.title",
            "body_token": "degraded.dashboard.bundle_missing.static_dir_missing.body",
            "action_chips": [
                {
                    "label_token": "degraded.dashboard.reinstall",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/install/troubleshooting#reinstall",
                    "style": "primary",
                },
            ],
            "metadata": {
                "verdict": "static_dir_missing",
                "missing_count": 0,
                "missing_sample": [],
                "static_dir": "/nonexistent/static",
                "scan_duration_ms": 0.05,
            },
            "first_observed_monotonic": 2.0,
            "last_observed_monotonic": 2.0,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[dashboard_axis],
                composite_severity="critical",
                composite_axis_count=1,
            ),
        )
        assert response.axes[0].severity == "critical"
        assert response.axes[0].metadata["verdict"] == "static_dir_missing"

    def test_c5_dashboard_axis_compounds_with_voice_axis(self) -> None:
        """Mission C5 §1.4 cross-coupling — the dashboard axis renders
        alongside any in-flight voice/llm/stt axes. Composite severity
        escalates with distinct axis count (2 = error).
        """
        voice_axis = {
            "axis": "voice",
            "reason": "failover_ladder_exhausted",
            "severity": "error",
            "title_token": "degraded.voice.failoverExhausted.title",
            "body_token": "degraded.voice.failoverExhausted.body",
            "action_chips": [],
            "metadata": {},
            "first_observed_monotonic": 1.0,
            "last_observed_monotonic": 1.0,
            "occurrence_count": 1,
        }
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_partial",
            "severity": "error",
            "title_token": "degraded.dashboard.bundle_partial.title",
            "body_token": "degraded.dashboard.bundle_partial.partial.body",
            "action_chips": [],
            "metadata": {"verdict": "partial", "missing_count": 1},
            "first_observed_monotonic": 1.2,
            "last_observed_monotonic": 1.2,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[voice_axis, dashboard_axis],
                composite_severity="error",
                composite_axis_count=2,
            ),
        )
        axes_by_axis = {axis.axis for axis in response.axes}
        assert axes_by_axis == {"voice", "dashboard"}
        assert response.composite_axis_count == 2


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


def _entry(axis: str, severity: str) -> DegradedEntry:
    return DegradedEntry(
        axis=axis,
        reason=f"{axis}.test_reason",
        severity=severity,
        title_token=f"degraded.{axis}.test.title",
        body_token=f"degraded.{axis}.test.body",
    )


class TestMaxPerAxisSeverity:
    """Mission D.1 / D-P0-1 — additive composite_max_severity helper."""

    def test_empty_returns_none(self) -> None:
        assert _max_per_axis_severity([]) is None

    def test_single_warn(self) -> None:
        assert _max_per_axis_severity([_entry("voice", "warn")]) == "warn"

    def test_single_error(self) -> None:
        assert _max_per_axis_severity([_entry("llm", "error")]) == "error"

    def test_single_critical(self) -> None:
        assert _max_per_axis_severity([_entry("voice", "critical")]) == "critical"

    def test_warning_normalized_to_warn(self) -> None:
        # Sibling grammar drift (D.1 addendum) — boundary normalizes.
        assert _max_per_axis_severity([_entry("engine_resources", "warning")]) == "warn"

    def test_mixed_returns_max(self) -> None:
        result = _max_per_axis_severity(
            [
                _entry("voice", "warn"),
                _entry("llm", "critical"),
                _entry("stt", "error"),
            ],
        )
        assert result == "critical"

    def test_unknown_value_ignored(self) -> None:
        # Out-of-grammar severity must not inflate the composite.
        assert _max_per_axis_severity([_entry("voice", "emergency")]) is None

    def test_unknown_does_not_demote_known(self) -> None:
        result = _max_per_axis_severity(
            [
                _entry("voice", "warn"),
                _entry("llm", "emergency"),
            ],
        )
        assert result == "warn"


class TestNormalizeSeverity:
    """Mission D.1 — defensive normalization at the composite boundary."""

    def test_warn_passthrough(self) -> None:
        assert _normalize_severity("warn") == "warn"

    def test_warning_to_warn(self) -> None:
        assert _normalize_severity("warning") == "warn"

    def test_error_passthrough(self) -> None:
        assert _normalize_severity("error") == "error"

    def test_critical_passthrough(self) -> None:
        assert _normalize_severity("critical") == "critical"

    def test_none_passthrough(self) -> None:
        assert _normalize_severity(None) is None

    def test_unknown_returns_none(self) -> None:
        assert _normalize_severity("emergency") is None
        assert _normalize_severity("info") is None


class TestComputeCompositeSeverityHybrid:
    """Mission D.1 / D-P0-1 — amended ADR-D6 Hybrid rule.

    composite = max(per-axis-max, count-tier) under
    ``None < warn < error < critical`` ordering.
    """

    def test_zero_axes_none(self) -> None:
        assert _compute_composite_severity_hybrid(0, None) is None

    def test_one_axis_warn_returns_warn(self) -> None:
        assert _compute_composite_severity_hybrid(1, "warn") == "warn"

    def test_one_axis_critical_returns_critical(self) -> None:
        """D-P0-1 smoking gun — single critical axis must NOT collapse to warn."""
        assert _compute_composite_severity_hybrid(1, "critical") == "critical"

    def test_one_axis_error_returns_error(self) -> None:
        assert _compute_composite_severity_hybrid(1, "error") == "error"

    def test_two_axes_both_warn_returns_error(self) -> None:
        """Cumulative blast-radius preserved by count-tier component."""
        assert _compute_composite_severity_hybrid(2, "warn") == "error"

    def test_two_axes_one_critical_returns_critical(self) -> None:
        assert _compute_composite_severity_hybrid(2, "critical") == "critical"

    def test_three_axes_all_warn_returns_critical(self) -> None:
        """Three-axis cognitive-load tier preserved from original ADR-D6."""
        assert _compute_composite_severity_hybrid(3, "warn") == "critical"

    def test_count_only_with_max_none_falls_back_to_count(self) -> None:
        # Producer emitted only unknown severities — count still drives composite.
        assert _compute_composite_severity_hybrid(2, None) == "error"

    def test_warning_normalized_in_hybrid(self) -> None:
        # Boundary normalization: producer "warning" must escalate properly.
        assert _compute_composite_severity_hybrid(1, "warning") == "warn"


class TestEngineDegradedResponseHybridAdditiveField:
    """Mission D.1 / D-P0-1 — composite_max_severity additive emission."""

    def test_field_round_trips_via_boundary(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis()],
                composite_severity="warn",
                composite_axis_count=1,
                extra={"composite_max_severity": "error"},
            ),
        )
        assert response.composite_max_severity == "error"

    def test_field_defaults_to_none_when_absent(self) -> None:
        # Pre-D.1 producer payloads (missing the new field) must still parse.
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[],
                composite_severity=None,
                composite_axis_count=0,
            ),
        )
        assert response.composite_max_severity is None

    def test_field_accepts_critical(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=partial(
                _response_payload,
                axes=[_voice_axis()],
                composite_severity="warn",
                composite_axis_count=1,
                extra={"composite_max_severity": "critical"},
            ),
        )
        assert response.composite_max_severity == "critical"
