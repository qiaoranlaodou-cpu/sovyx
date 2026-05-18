"""End-to-end integration tests — LLM discovery + dispatch + endpoints (Mission C6 §T2.11).

Exercises the discovery → dispatch → composite-store → `/api/llm/health`
chain against a TestClient. Companion to the unit suites in
``tests/unit/engine/test_llm_dispatch.py`` + ``test_llm_liveness_probe.py``
+ ``tests/dashboard/test_llm_health_route.py`` — verifies that the
pieces wire together correctly when invoked through the route boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    scan_llm_provider_health,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_TOKEN = "test-token-c6-e2e"


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture
def app() -> FastAPI:
    from sovyx.dashboard.server import create_app

    return create_app(token=_TOKEN)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestDiscoveryToCompositeStoreChain:
    def test_no_provider_configured_lands_in_composite_store(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
        dispatch_llm_discovery_verdict(report)
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "llm"
        assert entries[0].reason == "no_provider_configured"

    def test_recovery_clears_composite_store(self) -> None:
        # Seed degraded
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="",
                default_model="",
            ),
        )
        assert len(get_default_degraded_store().snapshot()) == 1
        # Transition to healthy
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={},
                ollama_ping_result=True,
                ollama_models=("llama3.1:latest",),
                default_provider="ollama",
                default_model="llama3.1:latest",
            ),
        )
        assert get_default_degraded_store().snapshot() == []


class TestLLMHealthEndpointE2E:
    def test_returns_503_when_router_not_registered(
        self,
        client: TestClient,
    ) -> None:
        # No registry on app.state → 503
        resp = client.get("/api/llm/health")
        assert resp.status_code == 503

    def test_returns_full_report_with_per_provider_matrix(
        self,
        app: FastAPI,
        client: TestClient,
    ) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        mock_router = MagicMock()
        mock_router.discovery_report = report
        registry.resolve = AsyncMock(return_value=mock_router)

        app.state.registry = registry
        try:
            resp = client.get("/api/llm/health")
        finally:
            del app.state.registry

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["verdict"] == "fully_available"
        assert payload["default_provider"] == "ollama"
        assert len(payload["per_provider"]) == 10
        # Spot-check the schema shape for one entry
        ollama_entry = next(p for p in payload["per_provider"] if p["name"] == "ollama")
        assert ollama_entry["is_cloud"] is False
        assert ollama_entry["env_var"] == ""


class TestTestConnectionEndpointE2E:
    def test_unknown_provider_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/llm/test-connection",
            json={"provider": "unknown"},
        )
        assert resp.status_code == 422

    def test_cloud_missing_key_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/llm/test-connection",
            json={"provider": "anthropic"},
        )
        assert resp.status_code == 422


class TestVerdictTaxonomyE2E:
    """Each refined reason MUST land on the composite store with the right shape."""

    def test_ollama_unreachable_dispatch_shape(self) -> None:
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="ollama",
                default_model="llama3.1:latest",
            ),
        )
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "ollama_unreachable"
        # Action chip targets are operator-actionable URLs.
        targets = {c.target for c in entry.action_chips}
        assert "https://ollama.ai/docs/start" in targets

    def test_ollama_no_models_dispatch_shape(self) -> None:
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={},
                ollama_ping_result=True,
                ollama_models=(),
                default_provider="",
                default_model="",
            ),
        )
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "ollama_no_models"
        assert entry.severity == "warn"

    def test_cloud_key_invalid_dispatch_shape(self) -> None:
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={"ANTHROPIC_API_KEY": "bad", "OPENAI_API_KEY": "bad"},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="",
                default_model="",
                cloud_key_validation_results={"anthropic": False, "openai": False},
            ),
        )
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "cloud_key_invalid"
        assert "invalid_providers" in entry.metadata

    def test_partial_health_dispatch_shape(self) -> None:
        dispatch_llm_discovery_verdict(
            scan_llm_provider_health(
                env={"ANTHROPIC_API_KEY": "ok", "OPENAI_API_KEY": "bad"},
                ollama_ping_result=True,
                ollama_models=("llama3.1:latest",),
                default_provider="",
                default_model="",
                cloud_key_validation_results={"anthropic": True, "openai": False},
            ),
        )
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.reason == "partial_health"
        assert "healthy_providers" in entry.metadata
        assert "unhealthy_providers" in entry.metadata
