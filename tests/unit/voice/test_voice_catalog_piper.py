"""Tests for the Piper voice locale catalog.

F2-M03↑ (audit §3.F + §3.Q) — Brazilian / Spanish operators were getting
the English Piper voice because the factory hard-defaulted to
``en_US-lessac-medium``. ``recommended_piper_voice_for`` resolves the
catalog Piper voice for the mind's effective locale; this test pins the
contract.
"""

from __future__ import annotations

import pytest

from sovyx.voice.voice_catalog import recommended_piper_voice_for


class TestRecommendedPiperVoiceFor:
    """Locale → Piper voice mapping."""

    @pytest.mark.parametrize(
        ("language", "expected"),
        [
            ("pt-BR", "pt_BR-faber-medium"),
            ("pt-br", "pt_BR-faber-medium"),
            ("pt_BR", "pt_BR-faber-medium"),
            ("pt-PT", "pt_BR-faber-medium"),  # PT_PT fallback
            ("es-ES", "es_ES-davefx-medium"),
            ("es-MX", "es_MX-sharvard-medium"),
            ("en-US", "en_US-lessac-medium"),
            ("en-GB", "en_GB-alan-medium"),
        ],
    )
    def test_supported_locale_returns_catalog_voice(
        self,
        language: str,
        expected: str,
    ) -> None:
        """Every supported locale resolves to its canonical Piper voice."""
        assert recommended_piper_voice_for(language) == expected

    def test_unsupported_locale_returns_none(self) -> None:
        """Locales outside the Piper catalog return None.

        Caller is expected to emit ``voice.factory.piper_locale_unsupported``
        WARN + fall back to ``tuning.piper_default_voice``. Includes
        locales that exist in the upstream rhasspy/piper-voices catalog
        but are NOT in Sovyx's curated subset (fr-FR, de-DE, it-IT) —
        extending coverage requires a SHA-verified tuple addition to
        ``model_registry._PIPER_VOICES`` in the same commit.
        """
        assert recommended_piper_voice_for("zh-CN") is None
        assert recommended_piper_voice_for("ja-JP") is None
        assert recommended_piper_voice_for("ko-KR") is None
        assert recommended_piper_voice_for("fr-FR") is None
        assert recommended_piper_voice_for("de-DE") is None
        assert recommended_piper_voice_for("it-IT") is None

    def test_helper_only_returns_voices_in_curated_catalog(self) -> None:
        """Every voice the helper returns MUST be in ``_PIPER_VOICES``.

        Closure check against anti-pattern #20 / drift. If a future
        contributor adds a locale → voice mapping without extending
        ``model_registry._PIPER_VOICES``, the factory's
        ``ensure_piper_model(voice=…)`` call will raise ValueError at
        runtime. This test catches it at build time.
        """
        from sovyx.voice.model_registry import list_piper_voices
        from sovyx.voice.voice_catalog import _PIPER_VOICES_BY_LANGUAGE

        curated = set(list_piper_voices())
        for locale, voice in _PIPER_VOICES_BY_LANGUAGE.items():
            assert voice in curated, (
                f"locale {locale!r} maps to {voice!r} which is not in "
                f"model_registry._PIPER_VOICES — would raise ValueError "
                f"on ensure_piper_model"
            )

    def test_empty_string_returns_none(self) -> None:
        """Empty input is not a valid locale — returns None."""
        assert recommended_piper_voice_for("") is None

    def test_separator_case_insensitivity(self) -> None:
        """Hyphen vs underscore + upper vs lower must collapse to one key."""
        assert (
            recommended_piper_voice_for("PT-BR")
            == recommended_piper_voice_for("pt_br")
            == recommended_piper_voice_for("pt-BR")
            == "pt_BR-faber-medium"
        )

    def test_region_distinction_preserved_for_spanish(self) -> None:
        """es-ES and es-MX MUST map to different voices.

        Unlike Kokoro (one Spanish voice family), Piper ships
        region-specific voices that sound markedly different. The Piper
        helper preserves the region tag instead of collapsing it.
        """
        es_es = recommended_piper_voice_for("es-ES")
        es_mx = recommended_piper_voice_for("es-MX")
        assert es_es != es_mx
        assert es_es == "es_ES-davefx-medium"
        assert es_mx == "es_MX-sharvard-medium"

    def test_whitespace_padding_is_stripped(self) -> None:
        """Trailing / leading whitespace must not break lookup."""
        assert recommended_piper_voice_for("  pt-BR  ") == "pt_BR-faber-medium"
