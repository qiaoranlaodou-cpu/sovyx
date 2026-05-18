"""Integration test — cognitive-loop dependency recovery (Mission C6 §T4.6).

Verifies the end-to-end recovery path:
  1. Boot with empty LLM router → CognitiveLoop start in degraded mode.
  2. Hot-register a provider via ``router.add_provider`` (or simulate
     a liveness-probe transition).
  3. ``LLMLivenessProbe._maybe_dispatch_transition`` propagates the
     verdict change to the gate via the callback.
  4. The gate's ``dependency_ready_event`` re-sets; the worker resumes.

Companion to ``tests/unit/cognitive/test_loop_dependency_gate.py`` +
``tests/unit/cognitive/test_gate_dependency_event.py`` — those exercise
the pieces in isolation; this test wires the producer→consumer chain.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.gate import CogLoopGate
from sovyx.cognitive.loop import CognitiveLoop
from sovyx.engine._llm_liveness_probe import LLMLivenessProbe
from sovyx.engine.config import LLMTuningConfig


def _make_router(*, has_available: bool) -> MagicMock:
    router = MagicMock()
    router.has_available_provider = MagicMock(return_value=has_available)
    router.discovery_report = None
    router.update_discovery_report = MagicMock()
    return router


def _make_ollama(*, is_available: bool, models: list[str] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.is_available = is_available
    mock.ping = AsyncMock(return_value=is_available)
    mock.list_models = AsyncMock(return_value=models or [])
    return mock


def _make_mind_config() -> MagicMock:
    mind = MagicMock()
    mind.llm.default_provider = ""
    mind.llm.default_model = ""
    return mind


def _make_cog_loop(router: MagicMock) -> CognitiveLoop:
    return CognitiveLoop(
        state_machine=MagicMock(),
        perceive=MagicMock(),
        attend=MagicMock(),
        think=MagicMock(),
        act=MagicMock(),
        reflect=MagicMock(),
        event_bus=MagicMock(),
        brain=None,
        llm_router=router,
    )


class TestCognitiveDependencyRecovery:
    @pytest.mark.asyncio
    async def test_boot_degraded_recovers_via_liveness_probe(self) -> None:
        """The canonical recovery path: boot with no LLM → liveness probe detects
        provider available → gate worker resumes."""
        router = _make_router(has_available=False)
        cog_loop = _make_cog_loop(router)
        gate = CogLoopGate(cog_loop)

        # Boot — loop starts degraded
        await cog_loop.start()
        assert cog_loop._dependency_ready is False
        # Bootstrap-side wiring: gate prime from router state
        gate.set_dependency_ready(False)
        assert not gate._dependency_ready_event.is_set()

        # Build the probe + wire its callback to the gate (as bootstrap does)
        ollama = _make_ollama(is_available=False)
        config = LLMTuningConfig(
            liveness_check_enabled=True,
            liveness_check_interval_sec=60.0,
            provider_unhealthy_grace_period_sec=0.0,  # no grace = immediate transitions
        )
        probe = LLMLivenessProbe(
            router=router,
            ollama_provider=ollama,
            config=config,
            mind_config=_make_mind_config(),
        )
        probe.set_dependency_state_callback(gate.set_dependency_ready)

        # Tick 1: still degraded
        await probe._tick()
        # baseline set; no transition yet
        # Now simulate "Ollama came up + has models"
        ollama.is_available = True
        ollama.ping = AsyncMock(return_value=True)
        ollama.list_models = AsyncMock(return_value=["llama3.1:latest"])

        # Tick 2: probe detects transition + dispatches + invokes callback
        await probe._tick()

        # Gate's event MUST be re-set (recovery)
        assert gate._dependency_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_healthy_to_degraded_via_probe_pauses_gate(self) -> None:
        """Reverse path — operator was healthy, Ollama crashed, gate must pause."""
        router = _make_router(has_available=True)
        cog_loop = _make_cog_loop(router)
        gate = CogLoopGate(cog_loop)
        await cog_loop.start()
        assert cog_loop._dependency_ready is True
        assert gate._dependency_ready_event.is_set()

        ollama = _make_ollama(is_available=True, models=["llama3.1:latest"])
        config = LLMTuningConfig(
            liveness_check_enabled=True,
            liveness_check_interval_sec=60.0,
            provider_unhealthy_grace_period_sec=0.0,
        )
        probe = LLMLivenessProbe(
            router=router,
            ollama_provider=ollama,
            config=config,
            mind_config=_make_mind_config(),
        )
        probe.set_dependency_state_callback(gate.set_dependency_ready)

        # Tick 1: healthy baseline
        await probe._tick()
        # Tick 2: Ollama crashed
        ollama.is_available = False
        ollama.ping = AsyncMock(return_value=False)
        ollama.list_models = AsyncMock(return_value=[])
        await probe._tick()

        # Gate's event MUST be cleared (paused)
        assert not gate._dependency_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_break_probe(self) -> None:
        """If the gate raises on callback, the probe MUST capture + continue."""
        router = _make_router(has_available=False)
        ollama = _make_ollama(is_available=False)
        config = LLMTuningConfig(
            liveness_check_enabled=True,
            liveness_check_interval_sec=60.0,
            provider_unhealthy_grace_period_sec=0.0,
        )
        probe = LLMLivenessProbe(
            router=router,
            ollama_provider=ollama,
            config=config,
            mind_config=_make_mind_config(),
        )

        def _bad_callback(_ready: bool) -> None:
            msg = "gate crashed"
            raise RuntimeError(msg)

        probe.set_dependency_state_callback(_bad_callback)
        # Tick 1: baseline
        await probe._tick()
        # Tick 2: ready transitions
        ollama.is_available = True
        ollama.ping = AsyncMock(return_value=True)
        ollama.list_models = AsyncMock(return_value=["llama3.1:latest"])
        # MUST NOT raise
        await probe._tick()
