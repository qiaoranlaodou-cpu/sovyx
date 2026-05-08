"""Integration test for v0.31.4 GAP 4 — voice auto-resume on daemon boot.

The contract: when ``MindConfig.voice_enabled=True`` is persisted from a
prior session, ``sovyx start`` MUST reconstruct the voice pipeline
without requiring the operator to ``POST /api/voice/enable`` again.

This test pins ``_auto_resume_voice_pipeline`` to the actual factory
signature so a future kwarg rename in :func:`create_voice_pipeline`
breaks at CI time, not in production at the operator's first restart.
"""

from __future__ import annotations

import inspect

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
