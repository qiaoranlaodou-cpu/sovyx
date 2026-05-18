"""Boundary round-trip tests — `/api/llm/health` LLMHealthResponse (Mission C6 §T2.7).

Quality Gate 8 (anti-pattern #40) requires every ``Model.model_validate(helper_dict)``
at a route boundary to have a paired round-trip test exercising the producer's
real in-memory shape. The dedicated companion to ``test_engine_degraded_boundary.py``
(which covers ``/api/engine/degraded``) — pins the contract for ``/api/llm/health``.

Forward-additive (``model_config = ConfigDict(extra="allow")``) is preserved —
adding new fields in future minor releases MUST NOT break the round-trip.
"""

from __future__ import annotations

from sovyx.dashboard.routes.llm_health import (
    LLMHealthResponse,
    ProviderHealthEntryModel,
)
from sovyx.llm._provider_health import scan_llm_provider_health


def _live_report_payload_fully_available() -> dict[str, object]:
    report = scan_llm_provider_health(
        env={},
        ollama_ping_result=True,
        ollama_models=("llama3.1:latest",),
        default_provider="ollama",
        default_model="llama3.1:latest",
    )
    return {
        "verdict": report.verdict.value,
        "configured_count": report.configured_count,
        "available_count": report.available_count,
        "default_provider": report.default_provider,
        "default_model": report.default_model,
        "scan_duration_ms": round(report.scan_duration_ms, 3),
        "scanned_at_monotonic": report.scanned_at_monotonic,
        "per_provider": [
            {
                "name": entry.name,
                "env_var": entry.env_var,
                "is_cloud": entry.is_cloud,
                "configured": entry.configured,
                "reachable": entry.reachable,
                "key_valid": entry.key_valid,
                "failure_reason": entry.failure_reason,
            }
            for entry in report.per_provider
        ],
    }


class TestLLMHealthResponseBoundary:
    def test_fully_available_round_trips(self) -> None:
        payload = _live_report_payload_fully_available()
        response = LLMHealthResponse.model_validate(payload)
        assert response.verdict == "fully_available"
        assert response.available_count >= 1
        assert len(response.per_provider) == 10

    def test_no_provider_configured_round_trips(self) -> None:
        report = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        payload = {
            "verdict": report.verdict.value,
            "configured_count": report.configured_count,
            "available_count": report.available_count,
            "default_provider": report.default_provider,
            "default_model": report.default_model,
            "scan_duration_ms": report.scan_duration_ms,
            "scanned_at_monotonic": report.scanned_at_monotonic,
            "per_provider": [
                {
                    "name": entry.name,
                    "env_var": entry.env_var,
                    "is_cloud": entry.is_cloud,
                    "configured": entry.configured,
                    "reachable": entry.reachable,
                    "key_valid": entry.key_valid,
                    "failure_reason": entry.failure_reason,
                }
                for entry in report.per_provider
            ],
        }
        response = LLMHealthResponse.model_validate(payload)
        assert response.verdict == "no_provider_configured"
        assert response.configured_count == 0

    def test_forward_additive_unknown_field_round_trips(self) -> None:
        """Future fields MUST NOT break consumers (anti-pattern #40 forward-additive)."""
        payload = _live_report_payload_fully_available()
        payload["future_field_xyz"] = {"some": "data"}
        # MUST NOT raise
        response = LLMHealthResponse.model_validate(payload)
        assert response.verdict == "fully_available"

    def test_provider_entry_int_or_str_env_var_round_trips(self) -> None:
        """Defensive — operator fixtures may carry int/None where str expected."""
        payload = _live_report_payload_fully_available()
        # Mutate one entry to have None env_var (Ollama has this)
        for entry in payload["per_provider"]:  # type: ignore[union-attr]
            if entry["name"] == "ollama":
                assert entry["env_var"] == ""
        response = LLMHealthResponse.model_validate(payload)
        ollama_entry = next(e for e in response.per_provider if e.name == "ollama")
        assert ollama_entry.env_var == ""

    def test_provider_entry_model_constructs_directly(self) -> None:
        """The shadow model can be built from primitives (used by tests + endpoints)."""
        entry = ProviderHealthEntryModel(
            name="anthropic",
            env_var="ANTHROPIC_API_KEY",
            is_cloud=True,
            configured=True,
            reachable=None,
            key_valid=None,
            failure_reason=None,
        )
        assert entry.name == "anthropic"
        assert entry.reachable is None

    def test_partial_health_with_validation_results_round_trips(self) -> None:
        """Mission C6 §T2.6 — payload includes validated cloud keys."""
        report = scan_llm_provider_health(
            env={"ANTHROPIC_API_KEY": "ok", "OPENAI_API_KEY": "bad"},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="",
            default_model="",
            cloud_key_validation_results={"anthropic": True, "openai": False},
        )
        payload = {
            "verdict": report.verdict.value,
            "configured_count": report.configured_count,
            "available_count": report.available_count,
            "default_provider": report.default_provider,
            "default_model": report.default_model,
            "scan_duration_ms": report.scan_duration_ms,
            "scanned_at_monotonic": report.scanned_at_monotonic,
            "per_provider": [
                {
                    "name": entry.name,
                    "env_var": entry.env_var,
                    "is_cloud": entry.is_cloud,
                    "configured": entry.configured,
                    "reachable": entry.reachable,
                    "key_valid": entry.key_valid,
                    "failure_reason": entry.failure_reason,
                }
                for entry in report.per_provider
            ],
        }
        response = LLMHealthResponse.model_validate(payload)
        assert response.verdict == "partial_health"
        openai_entry = next(e for e in response.per_provider if e.name == "openai")
        assert openai_entry.key_valid is False
        assert openai_entry.failure_reason == "auth_failed"
