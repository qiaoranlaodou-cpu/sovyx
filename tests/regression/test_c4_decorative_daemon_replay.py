"""C4 regression — forensic-replay of operator log L374 + L858 + L1063.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.12 (falsifiability gate F2).

Pre-mission HEAD (v0.45.7): the composite ``/api/engine/degraded``
endpoint does not exist — every consumer correlates 3 separate WARN
lines from the operator log by hand. This test asserts that after
Phase 1 ships, the endpoint surfaces all 3 axes in ONE composite
payload within a single HTTP round-trip.

F2 falsifiability: this test FAILS on HEAD pre-`f6a69d45` (the C4
foundation commit) — there is no ``/api/engine/degraded`` route → 404.
Post-mission: 200 with all 3 axes + composite_severity="critical".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    reset_default_degraded_store,
)

_TOKEN = "test-token-c4-replay"


@pytest.fixture()
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture()
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _seed_three_axis_decorative_daemon() -> None:
    """Reproduce the L374 + L858 + L1063 operator-session state."""
    store = get_default_degraded_store()
    # L374 — no_llm_provider_detected
    store.record(
        DegradedEntry(
            axis="llm",
            reason="no_llm_provider",
            severity="error",
            title_token="degraded.llm.noProvider.title",
            body_token="degraded.llm.noProvider.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.noProvider.installOllama",
                    "external_link",
                    "https://ollama.ai",
                    style="primary",
                ),
            ),
            metadata={"ollama_available": False},
            first_observed_monotonic=0.1,
            last_observed_monotonic=0.1,
            occurrence_count=1,
        ),
    )
    # L858 — voice.factory.stt_language_unsupported (operator's mind=jonny pt)
    store.record(
        DegradedEntry(
            axis="stt",
            reason="stt_language_coerced",
            severity="warn",
            title_token="degraded.stt.languageCoerced.title",
            body_token="degraded.stt.languageCoerced.body",
            action_chips=(
                make_action_chip(
                    "degraded.stt.languageCoerced.switchToEnglish",
                    "navigate",
                    "/settings/voice",
                ),
            ),
            metadata={
                "requested_language": "pt",
                "coerced_language": "en",
            },
            first_observed_monotonic=0.5,
            last_observed_monotonic=0.5,
            occurrence_count=1,
        ),
    )
    # L1063 — voice.failover.ladder_complete{verdict=exhausted}
    store.record(
        DegradedEntry(
            axis="voice",
            reason="failover_ladder_exhausted",
            severity="error",
            title_token="degraded.voice.ladderExhausted.title",
            body_token="degraded.voice.ladderExhausted.body",
            action_chips=(
                make_action_chip(
                    "degraded.voice.ladderExhausted.viewHistory",
                    "navigate",
                    "/voice/health",
                    style="primary",
                ),
            ),
            metadata={
                "candidates_tried": 1,
                "candidates_unreachable": ["hd-audio-generic-hw10"],
                "ladder_id": "operator_session_replay",
            },
            first_observed_monotonic=1.0,
            last_observed_monotonic=1.0,
            occurrence_count=1,
        ),
    )


class TestC4DecorativeDaemonReplay:
    """Replay the L374 + L858 + L1063 composite degraded surface."""

    def test_three_axis_composite_surface(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """All 3 silent failure axes from the v0.43.1 operator session
        surface on /api/engine/degraded with severity=critical.

        Pre-mission: 404 (endpoint did not exist).
        Post-mission: 200 with all 3 axes; operator does NOT need to
        grep 3 separate log lines.
        """
        _seed_three_axis_decorative_daemon()
        response = client.get("/api/engine/degraded")
        assert response.status_code == 200
        payload = response.json()

        # All 3 axes surface in ONE composite payload.
        assert payload["composite_axis_count"] == 3
        assert sorted(a["axis"] for a in payload["axes"]) == [
            "llm",
            "stt",
            "voice",
        ]
        # Severity escalates to critical per ADR-D6 (3+ axes).
        assert payload["composite_severity"] == "critical"

        # Every axis carries title + body i18n token (operator never
        # sees a raw English log line).
        for axis_entry in payload["axes"]:
            assert axis_entry["title_token"].startswith("degraded.")
            assert axis_entry["body_token"].startswith("degraded.")
            # Each axis must have at least one operator-actionable chip.
            assert len(axis_entry["action_chips"]) >= 1

    def test_endpoint_requires_auth(self) -> None:
        """Phase 1 §T1.6 — endpoint inherits dashboard auth via
        ``Depends(verify_token)`` on the router. Missing token MUST 401."""
        app = create_app(token=_TOKEN)
        client_no_auth = TestClient(app)
        response = client_no_auth.get("/api/engine/degraded")
        assert response.status_code == 401

    def test_empty_store_returns_clean_empty_payload(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """When no axis is degraded the endpoint MUST return
        composite_axis_count=0 + composite_severity=None so the
        frontend banner renders nothing."""
        response = client.get("/api/engine/degraded")
        assert response.status_code == 200
        payload = response.json()
        assert payload["composite_axis_count"] == 0
        assert payload["composite_severity"] is None
        assert payload["axes"] == []

    def test_single_axis_severity_warn(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Only one degraded axis → composite_severity=warn (per ADR-D6)."""
        store = get_default_degraded_store()
        store.record(
            DegradedEntry(
                axis="stt",
                reason="stt_language_coerced",
                severity="warn",
                title_token="degraded.stt.languageCoerced.title",
                body_token="degraded.stt.languageCoerced.body",
                action_chips=(),
                metadata={},
                first_observed_monotonic=0.0,
                last_observed_monotonic=0.0,
                occurrence_count=1,
            ),
        )
        response = client.get("/api/engine/degraded")
        assert response.status_code == 200
        payload = response.json()
        assert payload["composite_severity"] == "warn"
        assert payload["composite_axis_count"] == 1

    def test_two_axis_severity_error(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Two degraded axes → composite_severity=error."""
        store = get_default_degraded_store()
        for axis, reason in (("voice", "a"), ("llm", "b")):
            store.record(
                DegradedEntry(
                    axis=axis,
                    reason=reason,
                    severity="error",
                    title_token=f"degraded.{axis}.t",
                    body_token=f"degraded.{axis}.b",
                    action_chips=(),
                    metadata={},
                    first_observed_monotonic=0.0,
                    last_observed_monotonic=0.0,
                    occurrence_count=1,
                ),
            )
        response = client.get("/api/engine/degraded")
        payload = response.json()
        assert payload["composite_severity"] == "error"
        assert payload["composite_axis_count"] == 2
