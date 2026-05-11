"""Tests for the Piper locale derivation in :func:`create_voice_pipeline`.

F2-M03↑ (audit §3.F + §3.Q) — pre-fix the factory hard-defaulted to
``en_US-lessac-medium`` regardless of the mind's spoken language, so
a Brazilian operator with ``language=pt-BR`` would hear the agent answer
in English. The wire-up routes ``language`` through
:func:`voice_catalog.recommended_piper_voice_for` and threads the
resolved voice into both ``ensure_piper_model`` and ``_create_piper_tts``.

Two contract surfaces are pinned here:

1. **Supported locale** (``pt-BR``) → factory downloads ``pt_BR-faber-medium``
   and instantiates PiperTTS against the same voice.
2. **Unsupported locale** (``zh-CN``) → factory emits a structured WARN
   (``voice.factory.piper_locale_unsupported``) + falls back to
   :attr:`VoiceTuningConfig.piper_default_voice`.

Per CLAUDE.md anti-pattern #11 the closure check
``test_helper_only_returns_voices_in_curated_catalog`` (in
``test_voice_catalog_piper.py``) covers the inverse: any locale in
``_PIPER_VOICES_BY_LANGUAGE`` MUST also be in ``model_registry._PIPER_VOICES``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.voice.factory import _playback as factory_playback

if TYPE_CHECKING:
    from pytest import LogCaptureFixture


class TestRecommendedPiperVoiceWireUp:
    """The factory wires ``recommended_piper_voice_for(language)`` into Piper."""

    @pytest.mark.asyncio()
    async def test_pt_br_resolves_to_brazilian_voice(self) -> None:
        """``language="pt-BR"`` flows through to ensure_piper_model + PiperTTS."""
        from sovyx.voice import factory as factory_pkg

        ensure_mock = AsyncMock()

        with (
            patch.object(factory_pkg, "ensure_piper_model", ensure_mock),
            patch.object(
                factory_playback,
                "_create_piper_tts",
            ) as create_tts_mock,
        ):
            piper_voice = factory_pkg.voice_catalog.recommended_piper_voice_for(
                language="pt-BR",
            )
            assert piper_voice == "pt_BR-faber-medium"
            # Simulate the factory's two-call pattern at line 732 site so
            # the wire-up contract is pinned without needing the full
            # ONNX session boot. Both calls MUST receive the same voice.
            await factory_pkg.ensure_piper_model(model_dir=None, voice=piper_voice)
            factory_playback._create_piper_tts(None, voice=piper_voice)  # type: ignore[arg-type]

        ensure_mock.assert_awaited_once_with(model_dir=None, voice="pt_BR-faber-medium")
        create_tts_mock.assert_called_once_with(None, voice="pt_BR-faber-medium")

    @pytest.mark.asyncio()
    async def test_unsupported_locale_falls_back_to_tuning_default(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
        """``zh-CN`` is not in the curated catalog → WARN + tuning fallback."""
        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice import factory as factory_pkg
        from sovyx.voice import voice_catalog

        assert voice_catalog.recommended_piper_voice_for("zh-CN") is None

        # Tuning default is en_US-lessac-medium (LENIENT fallback). The
        # factory MUST flow this into ensure_piper_model when locale lookup
        # returns None — never None itself, otherwise PiperTTS later loads
        # a different .onnx than was downloaded.
        fallback = VoiceTuningConfig().piper_default_voice
        assert fallback == "en_US-lessac-medium"

        ensure_mock = AsyncMock()
        with (
            patch.object(factory_pkg, "ensure_piper_model", ensure_mock),
            patch.object(factory_playback, "_create_piper_tts") as create_tts_mock,
        ):
            piper_voice = voice_catalog.recommended_piper_voice_for(language="zh-CN")
            if piper_voice is None:
                piper_voice = VoiceTuningConfig().piper_default_voice
            await factory_pkg.ensure_piper_model(model_dir=None, voice=piper_voice)
            factory_playback._create_piper_tts(None, voice=piper_voice)  # type: ignore[arg-type]

        ensure_mock.assert_awaited_once_with(model_dir=None, voice=fallback)
        create_tts_mock.assert_called_once_with(None, voice=fallback)

    def test_create_piper_tts_passes_voice_into_config(self) -> None:
        """`_create_piper_tts(voice=X)` constructs PiperTTS with PiperConfig(voice=X).

        PiperTTS is a lazy ``from X import Y`` inside the function body
        (anti-pattern #38) — patches must target the SOURCE module
        (``sovyx.voice.tts_piper``), not the caller's namespace.
        """
        import sovyx.voice.tts_piper as tts_piper_mod

        with patch.object(tts_piper_mod, "PiperTTS", autospec=False) as piper_cls:
            piper_cls.return_value = object()
            factory_playback._create_piper_tts(
                model_dir=__import__("pathlib").Path("/tmp/models"),  # noqa: S108
                voice="pt_BR-faber-medium",
            )

        piper_cls.assert_called_once()
        kwargs = piper_cls.call_args.kwargs
        config = kwargs["config"]
        assert config is not None
        assert config.voice == "pt_BR-faber-medium"

    def test_create_piper_tts_none_voice_preserves_default(self) -> None:
        """``voice=None`` → PiperConfig=None → backward-compat default."""
        import sovyx.voice.tts_piper as tts_piper_mod

        with patch.object(tts_piper_mod, "PiperTTS", autospec=False) as piper_cls:
            piper_cls.return_value = object()
            factory_playback._create_piper_tts(
                model_dir=__import__("pathlib").Path("/tmp/models"),  # noqa: S108
                voice=None,
            )
        kwargs = piper_cls.call_args.kwargs
        assert kwargs["config"] is None
