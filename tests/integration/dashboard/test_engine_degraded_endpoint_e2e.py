"""E2E integration test — /api/engine/degraded full producer-to-route round-trip.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§9.2 row "test_engine_degraded_endpoint_e2e".

Exercises the full Phase 1.A surface: seed the store with the 3 axes
from the operator's v0.43.1 session, call ``GET /api/engine/degraded``
via FastAPI's TestClient, assert the round-trip is identity-preserving
across every field. Pairs with the regression test (forensic replay
verifies the operator-session shape) and the boundary test (Quality
Gate 8 round-trip discipline) — this test verifies the FastAPI
serialization layer doesn't drop or rename fields on the wire.

Phase 3 will extend this with POST /api/engine/degraded/ack +
TTL re-surface E2E flow. (Mission B B-P0-1 2026-05-21: corrected
path from the C4 spec's prose `/api/voice/...` claim to the
actual decorator registration `/api/engine/...`.)
"""

from __future__ import annotations

import time
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    reset_default_degraded_store,
)

_TOKEN = "test-token-c4-e2e"


@pytest.fixture()
def _reset_store() -> Generator[None, None, None]:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture()
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _seed_three_axes() -> None:
    store = get_default_degraded_store()
    _now = time.monotonic()
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
            first_observed_monotonic=_now,
            last_observed_monotonic=_now,
            occurrence_count=1,
        ),
    )
    store.record(
        DegradedEntry(
            axis="stt",
            reason="stt_language_coerced",
            severity="warn",
            title_token="degraded.stt.languageCoerced.title",
            body_token="degraded.stt.languageCoerced.body",
            action_chips=(),
            metadata={"requested_language": "pt", "coerced_language": "en"},
            first_observed_monotonic=_now + 0.001,
            last_observed_monotonic=_now + 0.001,
            occurrence_count=1,
        ),
    )
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
            metadata={"candidates_tried": 2, "ladder_id": "ladder_e2e_001"},
            first_observed_monotonic=_now + 0.002,
            last_observed_monotonic=_now + 0.002,
            occurrence_count=1,
        ),
    )


class TestEngineDegradedEndpointE2E:
    """Full producer-to-route serialization roundtrip."""

    def test_three_axes_serialize_via_fastapi_wire(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _seed_three_axes()
        response = client.get("/api/engine/degraded")
        assert response.status_code == 200
        body = response.json()

        # composite_severity / count consistency
        assert body["composite_axis_count"] == 3
        assert body["composite_severity"] == "critical"

        # All 3 axes round-trip with their identifiers intact
        axes_by_name = {a["axis"]: a for a in body["axes"]}
        assert set(axes_by_name) == {"llm", "stt", "voice"}

    def test_metadata_passes_through_fastapi_serialization(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _seed_three_axes()
        body = client.get("/api/engine/degraded").json()
        axes_by_name = {a["axis"]: a for a in body["axes"]}
        # STT axis: metadata captures requested_language / coerced_language
        stt = axes_by_name["stt"]
        assert stt["metadata"]["requested_language"] == "pt"
        assert stt["metadata"]["coerced_language"] == "en"
        # Voice axis: ladder_id present for log correlation
        voice = axes_by_name["voice"]
        assert voice["metadata"]["ladder_id"] == "ladder_e2e_001"

    def test_action_chips_round_trip_through_wire(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        _seed_three_axes()
        body = client.get("/api/engine/degraded").json()
        llm = next(a for a in body["axes"] if a["axis"] == "llm")
        assert len(llm["action_chips"]) == 1
        chip = llm["action_chips"][0]
        assert chip["action"] == "external_link"
        assert chip["target"] == "https://ollama.ai"
        assert chip["style"] == "primary"

    def test_ack_state_default_empty_in_phase_1(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Phase 1.A does NOT yet write to ack state. Endpoint returns
        a default-empty ack block. Phase 3 will populate this."""
        _seed_three_axes()
        body = client.get("/api/engine/degraded").json()
        assert body["ack"]["acked"] is False
        # Optional fields are None / absent — extra-allow contract.
        assert body["ack"].get("acked_at_ts") is None

    def test_clear_axis_then_endpoint_reflects(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        """Recovery flow: ladder succeeds → store.clear_axis("voice")
        → endpoint immediately shows the remaining axes only."""
        _seed_three_axes()
        get_default_degraded_store().clear_axis("voice")
        body = client.get("/api/engine/degraded").json()
        assert body["composite_axis_count"] == 2
        assert body["composite_severity"] == "error"
        axes = {a["axis"] for a in body["axes"]}
        assert axes == {"llm", "stt"}

    def test_empty_store_returns_minimal_payload(
        self,
        _reset_store: None,
        client: TestClient,
    ) -> None:
        body = client.get("/api/engine/degraded").json()
        assert body["composite_axis_count"] == 0
        assert body["composite_severity"] is None
        assert body["axes"] == []
        assert body["ack"]["acked"] is False

    def test_auth_required_returns_401_without_token(
        self,
        _reset_store: None,
    ) -> None:
        """Phase 1.A endpoint inherits Depends(verify_token) on its
        router. Anonymous request MUST 401."""
        app = create_app(token=_TOKEN)
        client_no_auth = TestClient(app)
        response = client_no_auth.get("/api/engine/degraded")
        assert response.status_code == 401
