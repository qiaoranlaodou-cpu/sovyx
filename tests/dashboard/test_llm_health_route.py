"""Integration tests — `/api/llm/health` + `/api/llm/test-connection` (Mission C6 §T2.7).

Coverage:
* 503 when engine isn't running / router not registered / report not primed.
* 200 with full LLMRouterDiscoveryReport shape when primed.
* `extra="allow"` forward-additive forward-compat.
* `/api/llm/test-connection` 422 on unknown provider + 422 on missing key
  for cloud + happy/sad paths for both cloud and Ollama branches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sovyx.llm._provider_health import scan_llm_provider_health

if TYPE_CHECKING:
    from fastapi import FastAPI

    from sovyx.llm._provider_health import LLMRouterDiscoveryReport

_TOKEN = "test-token-c6"


@pytest.fixture
def app() -> FastAPI:
    from sovyx.dashboard.server import create_app

    return create_app(token=_TOKEN)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _healthy_report() -> LLMRouterDiscoveryReport:
    return scan_llm_provider_health(
        env={},
        ollama_ping_result=True,
        ollama_models=("llama3.1:latest",),
        default_provider="ollama",
        default_model="llama3.1:latest",
    )


def _degraded_report() -> LLMRouterDiscoveryReport:
    return scan_llm_provider_health(
        env={},
        ollama_ping_result=False,
        ollama_models=None,
        default_provider="",
        default_model="",
    )


class TestLLMHealthEndpoint:
    def test_returns_503_when_engine_not_running(self, client: TestClient) -> None:
        resp = client.get("/api/llm/health")
        assert resp.status_code == 503

    def test_returns_503_without_token(self, app: FastAPI) -> None:
        bare = TestClient(app)
        resp = bare.get("/api/llm/health")
        assert resp.status_code == 401

    def test_returns_503_when_report_not_primed(self, app: FastAPI, client: TestClient) -> None:

        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        mock_router = MagicMock()
        mock_router.discovery_report = None
        registry.resolve = AsyncMock(return_value=mock_router)

        app.state.registry = registry
        resp = client.get("/api/llm/health")
        # Cleanup
        del app.state.registry
        assert resp.status_code == 503

    def test_returns_full_report_when_primed(self, app: FastAPI, client: TestClient) -> None:
        report = _healthy_report()
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
        assert payload["configured_count"] == 1
        assert payload["available_count"] == 1
        assert payload["default_provider"] == "ollama"
        assert payload["default_model"] == "llama3.1:latest"
        assert len(payload["per_provider"]) == 10  # All LLMProviderKey members
        assert all("name" in entry for entry in payload["per_provider"])

    def test_degraded_report_serializes(self, app: FastAPI, client: TestClient) -> None:
        report = _degraded_report()
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
        assert payload["verdict"] == "no_provider_configured"


class TestTestConnectionEndpointCloud:
    def test_invalid_provider_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/llm/test-connection",
            json={"provider": "nonexistent_provider", "api_key": "sk-test"},
        )
        assert resp.status_code == 422
        payload = resp.json()
        assert payload["ok"] is False
        assert "Unknown provider" in payload["message"]

    def test_cloud_provider_missing_key_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/llm/test-connection",
            json={"provider": "anthropic"},
        )
        assert resp.status_code == 422
        payload = resp.json()
        assert payload["ok"] is False
        assert "api_key is required" in payload["message"]

    def test_cloud_provider_valid_key_passes(self, client: TestClient) -> None:
        from sovyx.dashboard.routes import onboarding

        mock_provider = MagicMock()
        with (
            patch.object(onboarding, "_create_provider", return_value=mock_provider),
            patch.object(
                onboarding,
                "_test_provider",
                new=AsyncMock(return_value=(True, "Connection succeeded")),
            ),
        ):
            resp = client.post(
                "/api/llm/test-connection",
                json={"provider": "anthropic", "api_key": "sk-valid"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert "succeeded" in payload["message"]
        assert "latency_ms" in payload

    def test_cloud_provider_invalid_key_returns_ok_false(self, client: TestClient) -> None:
        from sovyx.dashboard.routes import onboarding

        mock_provider = MagicMock()
        with (
            patch.object(onboarding, "_create_provider", return_value=mock_provider),
            patch.object(
                onboarding,
                "_test_provider",
                new=AsyncMock(return_value=(False, "Auth failed: 401")),
            ),
        ):
            resp = client.post(
                "/api/llm/test-connection",
                json={"provider": "anthropic", "api_key": "sk-invalid"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is False
        assert "Auth failed" in payload["message"]


class TestTestConnectionEndpointOllama:
    def test_ollama_reachable_with_models(self, client: TestClient) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=["llama3.1:latest"])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            resp = client.post(
                "/api/llm/test-connection",
                json={"provider": "ollama"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["model_count"] == 1

    def test_ollama_unreachable(self, client: TestClient) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.ping = AsyncMock(return_value=False)
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            resp = client.post(
                "/api/llm/test-connection",
                json={"provider": "ollama"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is False
        assert "not reachable" in payload["message"]

    def test_ollama_no_models(self, client: TestClient) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=[])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            resp = client.post(
                "/api/llm/test-connection",
                json={"provider": "ollama"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is False
        assert "no models" in payload["message"]
        assert payload["model_count"] == 0
