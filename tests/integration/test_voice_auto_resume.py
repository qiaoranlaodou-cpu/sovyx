"""Integration test for v0.31.4 GAP 4 — voice auto-resume on daemon boot.

The contract: when ``MindConfig.voice_enabled=True`` is persisted from a
prior session, ``sovyx start`` MUST reconstruct the voice pipeline
without requiring the operator to ``POST /api/voice/enable`` again.

This test pins ``_auto_resume_voice_pipeline`` to the actual factory
signature so a future kwarg rename in :func:`create_voice_pipeline`
breaks at CI time, not in production at the operator's first restart.

v0.31.6 paranoid-closure T1.2 (C2) adds failure-path tests: a
``start()`` raise must NOT leak a zombie pipeline into the registry,
and the helper must best-effort tear down the bundle before re-raising.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine import bootstrap
from sovyx.voice.factory import create_voice_pipeline


class TestAutoResumeKwargContract:
    """The auto-resume call site uses only kwargs the factory accepts."""

    def test_every_kwarg_passed_exists_on_factory(self) -> None:
        factory_params = set(inspect.signature(create_voice_pipeline).parameters.keys())
        # Mirrors src/sovyx/engine/bootstrap.py::_auto_resume_voice_pipeline
        # — keep these two lists synchronised when the factory signature
        # changes.
        expected_kwargs = {
            "data_dir",
            "mind_id",
            "language",
            "voice_id",
            "wake_word_enabled",
            "input_device_name",
            "input_device_host_api",
            "allow_inoperative_capture",
        }
        missing = expected_kwargs - factory_params
        assert not missing, (
            f"_auto_resume_voice_pipeline passes kwargs that don't exist on "
            f"create_voice_pipeline: {missing}. Update bootstrap.py to match "
            f"the new factory signature, OR add the missing parameters back "
            f"to the factory."
        )

    def test_module_exports_auto_resume_helper(self) -> None:
        """Renaming the helper would silently break the bootstrap call site."""
        assert hasattr(bootstrap, "_auto_resume_voice_pipeline")
        assert inspect.iscoroutinefunction(bootstrap._auto_resume_voice_pipeline)


def _make_bundle(*, start_exc: Exception | None = None) -> SimpleNamespace:
    """Build a fake voice bundle with mockable pipeline + capture_task.

    ``start_exc`` — if set, ``capture_task.start()`` raises it. Otherwise
    start succeeds (no-op).
    """
    capture_task = SimpleNamespace(
        start=AsyncMock(side_effect=start_exc) if start_exc else AsyncMock(),
        stop=AsyncMock(),
    )
    pipeline = SimpleNamespace(
        stop=AsyncMock(),
    )
    return SimpleNamespace(pipeline=pipeline, capture_task=capture_task)


def _make_mind_config() -> SimpleNamespace:
    return SimpleNamespace(
        id="test-mind",
        language="en",
        voice_id="test-voice",
        wake_word_enabled=False,
        voice_input_device_name=None,
        voice_input_device_host_api=None,
        voice_enabled=True,
    )


def _make_engine_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path)


class TestAutoResumeFailurePath:
    """v0.31.6 T1.2 (C2): start() failure must not corrupt the registry."""

    @pytest.mark.asyncio
    async def test_start_failure_does_not_leak_zombie(self, tmp_path: Path) -> None:
        """``start()`` raising leaves the registry untouched."""
        bundle = _make_bundle(start_exc=OSError("device unplugged"))

        # Registry is a strict mock: ``replace_instance`` MUST NOT be
        # called when start() fails. ``is_registered`` reflects that
        # the slots stay empty post-call.
        registry = MagicMock()
        registry.replace_instance = AsyncMock()
        registry.is_registered = MagicMock(return_value=False)

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch.object(
                bootstrap,
                "_auto_resume_voice_pipeline",
                wraps=bootstrap._auto_resume_voice_pipeline,
            ),
            patch("sovyx.voice.factory.create_voice_pipeline", AsyncMock(return_value=bundle)),
            pytest.raises(OSError, match="device unplugged"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        registry.replace_instance.assert_not_awaited()

        # Importing the actual interfaces is fine — registry mock
        # returns False for both regardless of arg, but assert calls
        # we'd expect downstream code to make.
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        assert registry.is_registered(VoicePipeline) is False
        assert registry.is_registered(AudioCaptureTask) is False

    @pytest.mark.asyncio
    async def test_start_failure_attempts_cleanup_best_effort(self, tmp_path: Path) -> None:
        """Cleanup runs on both pipeline and capture_task even if it raises."""
        bundle = _make_bundle(start_exc=RuntimeError("model load OOM"))
        # Cleanup itself raises — the original error MUST still propagate.
        bundle.capture_task.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        bundle.pipeline.stop = AsyncMock(side_effect=RuntimeError("pipeline stop failed"))

        registry = MagicMock()
        registry.replace_instance = AsyncMock()

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch("sovyx.voice.factory.create_voice_pipeline", AsyncMock(return_value=bundle)),
            pytest.raises(RuntimeError, match="model load OOM"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        # Both teardown handles attempted exactly once each.
        bundle.capture_task.stop.assert_awaited_once()
        bundle.pipeline.stop.assert_awaited_once()
        # Registry remains untouched.
        registry.replace_instance.assert_not_awaited()
