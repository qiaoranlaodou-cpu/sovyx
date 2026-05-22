"""Mission D.1 / D-P0-1 — OPERATIONAL SEVERITY CORRUPTION replay.

Mission anchor:
``docs-internal/MISSION-D-FORENSIC-AUDIT-2026-05-21.md`` D-P0-1 +
``docs-internal/MISSION-D-REMEDIATION-PLAN-2026-05-21.md`` §3 D.1.

Pre-D.1 (count-tier-only rule): three producer sites emitted
``DegradedEntry.severity="critical"`` for SINGLE-axis conditions;
``_compute_composite_severity(distinct_axis_count=1)`` returned
``"warn"``; the dashboard banner painted YELLOW; operators deferred
critical events.

Post-D.1 (amended ADR-D6 Hybrid rule):
``composite_severity = max(max(entry.severity), count_tier(axes))``
under ``None < "warn" < "error" < "critical"`` ordering. A single
axis emitting ``severity="critical"`` paints the banner RED.

This file pins the three smoking-gun producer signatures + the
canonical 10-scenario adversarial coverage so a regression of the
amendment is loud in CI.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)

_TOKEN = "test-token-d1-replay"  # noqa: S105 — test-only fixture


@pytest.fixture()
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture()
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _record(
    axis: str,
    reason: str,
    severity: str,
) -> None:
    get_default_degraded_store().record(
        DegradedEntry(
            axis=axis,
            reason=reason,
            severity=severity,
            title_token=f"degraded.{axis}.{reason}.title",
            body_token=f"degraded.{axis}.{reason}.body",
            action_chips=(),
            metadata={},
            first_observed_monotonic=1.0,
            last_observed_monotonic=1.0,
            occurrence_count=1,
        ),
    )


class TestDP01SmokingGunReplay:
    """The three single-axis producer sites that emitted severity=critical
    pre-D.1, each verified to now paint RED under the amended Hybrid rule.
    """

    def test_llm_no_provider_configured_paints_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """src/sovyx/engine/_llm_dispatch.py:116 — no_provider_configured
        records severity="critical" on the LLM axis. Pre-D.1: composite
        painted yellow ("warn"). Post-D.1: composite paints red
        ("critical")."""
        _record("llm", "no_provider_configured", "critical")
        response = client.get("/api/engine/degraded")
        assert response.status_code == 200
        payload = response.json()
        assert payload["composite_axis_count"] == 1
        assert payload["composite_severity"] == "critical"
        assert payload["composite_max_severity"] == "critical"

    def test_voice_auto_recovery_exhausted_paints_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """src/sovyx/voice/pipeline/_heartbeat_mixin.py:731 — when the
        auto-recovery governor exhausts its budget, the heartbeat mixin
        records severity="critical" on the voice axis. Pre-D.1: composite
        painted yellow. Post-D.1: composite paints red."""
        _record("voice", "auto_recovery_exhausted", "critical")
        response = client.get("/api/engine/degraded")
        payload = response.json()
        assert payload["composite_axis_count"] == 1
        assert payload["composite_severity"] == "critical"

    def test_dashboard_bundle_missing_paints_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """src/sovyx/dashboard/server.py:720 — when the dashboard bundle
        is missing entirely, server.py records severity="critical" on
        the dashboard axis. Pre-D.1: composite painted yellow. Post-D.1:
        composite paints red."""
        _record("dashboard", "bundle_missing", "critical")
        response = client.get("/api/engine/degraded")
        payload = response.json()
        assert payload["composite_axis_count"] == 1
        assert payload["composite_severity"] == "critical"


class TestDP01HybridRuleAdversarialMatrix:
    """The 10-scenario adversarial coverage from the D.1 mission audit.

    Each scenario pins the (count, per-axis-max) → composite mapping
    against the amended ADR-D6 Hybrid rule under the default knob
    (composite_severity_by_max=True).
    """

    def test_single_axis_warn_returns_warn(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _record("stt", "stt_language_coerced", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "warn"

    def test_single_axis_error_returns_error(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        # 1 axis @ error: count-tier=warn, max=error → Hybrid=error.
        # Pre-D.1 paint=warn (yellow); post-D.1 paint=error (red).
        _record("llm", "ollama_unreachable", "error")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "error"

    def test_two_axes_both_warn_returns_error(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Cumulative blast-radius preserved by count-tier component:
        2 distinct axes => count-tier=error >= max(warn,warn)=warn =>
        composite=error."""
        _record("stt", "stt_language_coerced", "warn")
        _record("llm", "partial_health", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "error"

    def test_two_axes_critical_and_warn_returns_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _record("llm", "no_provider_configured", "critical")
        _record("stt", "stt_language_coerced", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "critical"

    def test_three_axes_all_warn_returns_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """ADR-D6 three-axis cognitive-load tier preserved."""
        _record("voice", "x", "warn")
        _record("llm", "y", "warn")
        _record("stt", "z", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "critical"

    def test_two_axes_both_critical_returns_critical(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _record("llm", "no_provider_configured", "critical")
        _record("dashboard", "bundle_missing", "critical")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "critical"

    def test_composite_max_severity_emitted_independently(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """composite_max_severity tracks the raw per-axis max,
        independent of count-tier. Single warn axis => warn; both
        composite_severity and composite_max_severity = warn."""
        _record("stt", "stt_language_coerced", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_max_severity"] == "warn"

    def test_composite_max_severity_diverges_from_count_tier(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """When 2 axes warn: composite_severity=error (count-tier wins),
        composite_max_severity=warn (per-axis-max wins independently).
        Both signals are exposed for downstream monitoring."""
        _record("stt", "stt_language_coerced", "warn")
        _record("llm", "partial_health", "warn")
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] == "error"
        assert payload["composite_max_severity"] == "warn"

    def test_warning_severity_normalized_to_warn(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Sibling grammar drift (out of D.1 scope): cohort governor
        + 13 voice sites emit severity="warning" instead of "warn".
        The composite normalizes "warning" -> "warn" at the boundary so
        the underlying producer drift cannot inflate the composite
        beyond what the canonical grammar would produce."""
        _record("engine_resources", "cohort_breach", "warning")
        payload = client.get("/api/engine/degraded").json()
        # 1 axis, severity=warning -> normalized to warn -> Hybrid=warn.
        assert payload["composite_severity"] == "warn"

    def test_empty_store_returns_none(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        payload = client.get("/api/engine/degraded").json()
        assert payload["composite_severity"] is None
        assert payload["composite_max_severity"] is None
        assert payload["composite_axis_count"] == 0


class TestDP01KnobRollback:
    """Operator-side rollback via
    SOVYX_TUNING__DASHBOARD__COMPOSITE_SEVERITY_BY_MAX=false restores
    the original pure count-tier ADR-D6 rule."""

    def test_knob_false_restores_count_tier_for_single_critical_axis(
        self,
        _reset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the knob flipped False, the single-critical-axis case
        once again paints YELLOW ("warn"). The additive
        composite_max_severity field still exposes the truthful
        per-axis-max signal so downstream consumers can still tell
        the per-axis truth even during rollback."""
        monkeypatch.setenv(
            "SOVYX_TUNING__DASHBOARD__COMPOSITE_SEVERITY_BY_MAX",
            "false",
        )
        app = create_app(token=_TOKEN)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        _record("llm", "no_provider_configured", "critical")
        payload = client.get("/api/engine/degraded").json()
        # Knob-false: count-tier rule fires; 1 axis -> "warn".
        assert payload["composite_severity"] == "warn"
        # Additive field still exposes the truth.
        assert payload["composite_max_severity"] == "critical"
