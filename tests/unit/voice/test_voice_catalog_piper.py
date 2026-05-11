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
            ("es-ES", "es_ES-mls_9972-low"),
            ("es-MX", "es_MX-claude-high"),
            ("en-US", "en_US-lessac-medium"),
            ("en-GB", "en_GB-alan-low"),
            ("fr-FR", "fr_FR-siwis-medium"),
            ("de-DE", "de_DE-thorsten-medium"),
            ("it-IT", "it_IT-riccardo-x_low"),
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
        """Locales outside the Piper catalog (zh-CN, ja-JP) return None.

        Caller is expected to emit ``voice.factory.piper_locale_unsupported``
        WARN + fall back to ``tuning.piper_default_voice``.
        """
        assert recommended_piper_voice_for("zh-CN") is None
        assert recommended_piper_voice_for("ja-JP") is None
        assert recommended_piper_voice_for("ko-KR") is None

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
        assert es_es == "es_ES-mls_9972-low"
        assert es_mx == "es_MX-claude-high"

    def test_whitespace_padding_is_stripped(self) -> None:
        """Trailing / leading whitespace must not break lookup."""
        assert recommended_piper_voice_for("  pt-BR  ") == "pt_BR-faber-medium"
