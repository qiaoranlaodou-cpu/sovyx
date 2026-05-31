"""Unit tests — `CognitiveLoop` dependency gate + `process_request` short-circuit (Mission C6 §T4.1, §T4.4).

Coverage:
* ``start()`` records dependency state + emits the right structured
  signal (``cognitive_loop_started`` when healthy vs
  ``cognitive.loop.started_in_degraded_mode`` when not).
* ``process_request`` short-circuits with the synthetic ActionResult
  when ``_dependency_ready=False`` AND ``fail_fast=True``.
* ``process_request`` runs the full loop when fail-fast=False OR
  dependencies are ready.
* Backward-compat: ``CognitiveLoop`` without ``llm_router`` arg behaves
  exactly like pre-Mission-C6 (no dependency check, no short-circuit).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.loop import CognitiveLoop
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import ConversationId, MindId, PerceptionType


def _make_loop(
    *,
    llm_router: MagicMock | None,
    brain: MagicMock | None,
    fail_fast: bool = True,
) -> CognitiveLoop:
    return CognitiveLoop(
        state_machine=MagicMock(),
        perceive=MagicMock(),
        attend=MagicMock(),
        think=MagicMock(),
        act=MagicMock(),
        reflect=MagicMock(),
        event_bus=MagicMock(),
        brain=brain,
        llm_router=llm_router,
        cognitive_degraded_mode_fail_fast=fail_fast,
    )


def _make_request(channel: str = "test-channel") -> CognitiveRequest:
    # C-Σ-002: build a REAL CognitiveRequest. The previous helper returned a
    # MagicMock with `.channel`/`.request_id` set — attributes that do NOT
    # exist on CognitiveRequest — which masked the bug where the synthetic
    # degraded path read those nonexistent attrs and always got "unknown".
    return CognitiveRequest(
        perception=Perception(
            id="msg-1",
            type=PerceptionType.USER_MESSAGE,
            source=channel,
            content="hello",
            metadata={"reply_to": "msg-1"},
        ),
        mind_id=MindId("test-mind"),
        conversation_id=ConversationId("test-conv"),
        conversation_history=[],
    )


class TestStartDependencyGate:
    @pytest.mark.asyncio
    async def test_no_router_no_brain_marks_ready(self) -> None:
        """Backward-compat: pre-Mission-C6 constructor signature still works."""
        loop = _make_loop(llm_router=None, brain=None)
        await loop.start()
        assert loop._dependency_ready is True
        assert loop._missing_dependencies == ()

    @pytest.mark.asyncio
    async def test_router_with_provider_marks_ready(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=True)
        brain = MagicMock()
        brain.embedding_model_ready = True
        loop = _make_loop(llm_router=router, brain=brain)
        await loop.start()
        assert loop._dependency_ready is True

    @pytest.mark.asyncio
    async def test_router_with_no_provider_marks_degraded(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=False)
        router.discovery_report = None
        loop = _make_loop(llm_router=router, brain=None)
        # Mission C5 audit-cycle-2 fix: structlog bypasses stdlib caplog,
        # so we patch the module logger directly to verify emission.
        with patch("sovyx.cognitive.loop.logger") as mock_logger:
            await loop.start()
        assert loop._dependency_ready is False
        assert "llm_router_no_available_provider" in loop._missing_dependencies
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "cognitive.loop.started_in_degraded_mode"

    @pytest.mark.asyncio
    async def test_brain_not_ready_marks_degraded(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=True)
        brain = MagicMock()
        brain.embedding_model_ready = False
        loop = _make_loop(llm_router=router, brain=brain)
        await loop.start()
        assert loop._dependency_ready is False
        assert "brain_embedding_model_not_ready" in loop._missing_dependencies

    @pytest.mark.asyncio
    async def test_both_missing_collects_all(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=False)
        router.discovery_report = None
        brain = MagicMock()
        brain.embedding_model_ready = False
        loop = _make_loop(llm_router=router, brain=brain)
        await loop.start()
        assert set(loop._missing_dependencies) == {
            "llm_router_no_available_provider",
            "brain_embedding_model_not_ready",
        }


class TestProcessRequestShortCircuit:
    @pytest.mark.asyncio
    async def test_degraded_with_fail_fast_short_circuits(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=False)
        router.discovery_report = None
        loop = _make_loop(llm_router=router, brain=None, fail_fast=True)
        await loop.start()
        # Replace _execute_loop so we can detect if it was called
        loop._execute_loop = AsyncMock(side_effect=AssertionError("loop body should not run"))

        with patch("sovyx.cognitive.loop.logger") as mock_logger:
            result = await loop.process_request(_make_request("voice"))
        assert isinstance(result, ActionResult)
        assert result.degraded is True
        assert result.error is True
        assert result.metadata["reason"] == "cognitive_dependency_missing"
        assert "llm_router_no_available_provider" in result.metadata["missing_dependencies"]
        # C-Σ-002: channel + reply target derive from the real perception, not
        # nonexistent request attrs (was ALWAYS "unknown" / None).
        assert result.target_channel == "voice"
        assert result.reply_to == "msg-1"
        # _execute_loop must NOT have been invoked
        loop._execute_loop.assert_not_called()
        # Short-circuit event fired exactly once
        short_circuit_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c[0][0] == "cognitive.loop.short_circuit_degraded"
        ]
        assert len(short_circuit_calls) == 1

    @pytest.mark.asyncio
    async def test_degraded_without_fail_fast_runs_loop(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=False)
        router.discovery_report = None
        loop = _make_loop(llm_router=router, brain=None, fail_fast=False)
        await loop.start()
        # Loop body should run (and presumably fail individually per phase)
        sentinel = ActionResult(response_text="loop ran", target_channel="x")
        loop._execute_loop = AsyncMock(return_value=sentinel)
        result = await loop.process_request(_make_request())
        assert result is sentinel
        loop._execute_loop.assert_called_once()

    @pytest.mark.asyncio
    async def test_healthy_runs_loop_normally(self) -> None:
        router = MagicMock()
        router.has_available_provider = MagicMock(return_value=True)
        loop = _make_loop(llm_router=router, brain=None, fail_fast=True)
        await loop.start()
        sentinel = ActionResult(response_text="healthy", target_channel="x")
        loop._execute_loop = AsyncMock(return_value=sentinel)
        result = await loop.process_request(_make_request())
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_pre_start_default_ready_runs_loop(self) -> None:
        """Loop never started → ``_dependency_ready=True`` (default) → loop runs."""
        loop = _make_loop(llm_router=None, brain=None)
        # NOTE: not calling start() — _dependency_ready stays True from __init__
        sentinel = ActionResult(response_text="default", target_channel="x")
        loop._execute_loop = AsyncMock(return_value=sentinel)
        result = await loop.process_request(_make_request())
        assert result is sentinel
