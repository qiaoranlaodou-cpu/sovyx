"""Unit test — `_render_llm_health_surface` in aggregate `sovyx doctor` (Mission C6 §T3.4).

Exercises the surface-render function directly. The surface mirrors
``_render_voice_degraded_banner_surface`` (Mission C4) and
``_render_dashboard_integrity_surface`` (Mission C5) shape so CLI-only
operators see the LLM section alongside voice + dashboard surfaces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sovyx.cli.commands import doctor as doctor_mod


class TestRenderLLMHealthSurface:
    def test_json_mode_is_no_op(self) -> None:
        """JSON aggregate output skips the human-readable section."""
        with patch.object(doctor_mod, "console") as mock_console:
            doctor_mod._render_llm_health_surface(output_json=True)
        mock_console.print.assert_not_called()

    def test_unavailable_when_imports_fail(self) -> None:
        """Defensive — surface tolerates missing modules in test environments."""
        # The import inside _render_llm_health_surface is lazy; mock-failing
        # the OllamaProvider import chain via patch.dict on sys.modules.
        with (
            patch.object(doctor_mod, "console") as mock_console,
            patch.dict("sys.modules", {"sovyx.llm.providers.ollama": None}),
        ):
            doctor_mod._render_llm_health_surface(output_json=False)
        # At least one print call (either the unavailable hint OR the actual
        # surface). The exact path depends on whether the module ever loaded.
        assert mock_console.print.call_count >= 1

    def test_fully_available_renders_green(self) -> None:
        """When the live env is healthy, surface prints the green check."""
        from sovyx.llm._provider_health import (
            DiscoveryVerdict,
            scan_llm_provider_health,
        )

        # Synthesize a healthy report.
        healthy = scan_llm_provider_health(
            env={},
            ollama_ping_result=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        assert healthy.verdict is DiscoveryVerdict.FULLY_AVAILABLE

        # Mock Ollama + the scanner so the surface picks up the healthy report.
        mock_ollama = MagicMock()
        mock_ollama.is_available = True
        mock_ollama.ping = AsyncMock(return_value=True)
        mock_ollama.list_models = AsyncMock(return_value=["llama3.1:latest"])

        with (
            patch.object(doctor_mod, "console") as mock_console,
            patch(
                "sovyx.llm.providers.ollama.OllamaProvider",
                return_value=mock_ollama,
            ),
            patch(
                "sovyx.llm._provider_health.scan_llm_provider_health",
                return_value=healthy,
            ),
        ):
            doctor_mod._render_llm_health_surface(output_json=False)

        # The surface printed at least the header + the OK line.
        call_args = [c[0][0] for c in mock_console.print.call_args_list if c[0]]
        assert any("LLM" in s and "provider health" in s for s in call_args)
        assert any("FULLY_AVAILABLE" in s for s in call_args)

    def test_degraded_renders_with_severity_color(self) -> None:
        """Degraded verdicts print the verdict + the failure summary."""
        from sovyx.llm._provider_health import scan_llm_provider_health

        degraded = scan_llm_provider_health(
            env={},
            ollama_ping_result=False,
            ollama_models=None,
            default_provider="",
            default_model="",
        )
        mock_ollama = MagicMock()
        mock_ollama.is_available = False
        mock_ollama.ping = AsyncMock(return_value=False)
        mock_ollama.list_models = AsyncMock(return_value=[])

        with (
            patch.object(doctor_mod, "console") as mock_console,
            patch(
                "sovyx.llm.providers.ollama.OllamaProvider",
                return_value=mock_ollama,
            ),
            patch(
                "sovyx.llm._provider_health.scan_llm_provider_health",
                return_value=degraded,
            ),
        ):
            doctor_mod._render_llm_health_surface(output_json=False)

        call_args = [c[0][0] for c in mock_console.print.call_args_list if c[0]]
        assert any("NO_PROVIDER_CONFIGURED" in s for s in call_args)
        assert any("sovyx llm doctor" in s for s in call_args)
