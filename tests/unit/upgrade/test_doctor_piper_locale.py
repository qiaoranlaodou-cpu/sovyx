"""Tests for :func:`sovyx.upgrade.doctor._check_piper_locale_match`.

F2-M03↑ (audit §3.F + §3.Q flip step) — the probe surfaces gaps between
the mind's spoken language and the curated Piper voice catalog. Three
contract surfaces are pinned:

1. **Catalog hit** → ``PASS`` + the resolved Piper voice in details.
2. **Catalog miss** → ``WARN`` (LENIENT per ``feedback_staged_adoption``)
   + supported-locales hint + ``lenient_mode=True`` flag in details
   so a future STRICT flip can be detected by integration consumers.
3. **None / empty input** → falls back to ``en-US`` to keep the probe
   runnable without an active mind context.
"""

from __future__ import annotations

from sovyx.upgrade.doctor import (
    DiagnosticStatus,
    _check_piper_locale_match,
)


class TestPiperLocaleMatchPass:
    """Catalog hits return PASS with the voice id in details."""

    def test_pt_br_passes(self) -> None:
        result = _check_piper_locale_match("pt-BR")
        assert result.status == DiagnosticStatus.PASS
        assert result.check == "piper_locale_match"
        assert result.details is not None
        assert result.details["piper_voice"] == "pt_BR-faber-medium"
        assert result.details["language"] == "pt-BR"

    def test_en_us_passes(self) -> None:
        result = _check_piper_locale_match("en-US")
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["piper_voice"] == "en_US-lessac-medium"

    def test_es_mx_passes_with_regional_voice(self) -> None:
        """Spanish region tag is preserved (vs Kokoro's es-bucket collapse)."""
        result = _check_piper_locale_match("es-MX")
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["piper_voice"] == "es_MX-sharvard-medium"

    def test_case_insensitivity(self) -> None:
        """Mixed-case locales must still resolve to the catalog."""
        result = _check_piper_locale_match("PT-br")
        assert result.status == DiagnosticStatus.PASS


class TestPiperLocaleMatchWarn:
    """Catalog misses return WARN (LENIENT per feedback_staged_adoption)."""

    def test_zh_cn_warns(self) -> None:
        result = _check_piper_locale_match("zh-CN")
        assert result.status == DiagnosticStatus.WARN
        assert "no curated Piper voice" in result.message.lower() or (
            "no curated piper voice" in result.message.lower()
        )
        assert result.details is not None
        assert result.details["piper_voice"] is None
        assert "supported_locales" in result.details

    def test_warn_payload_advertises_lenient_mode(self) -> None:
        """``lenient_mode=True`` lets integration consumers detect STRICT flip later."""
        result = _check_piper_locale_match("ja-JP")
        assert result.status == DiagnosticStatus.WARN
        assert result.details is not None
        assert result.details["lenient_mode"] is True

    def test_warn_lists_supported_locales_in_fix_hint(self) -> None:
        result = _check_piper_locale_match("ko-KR")
        assert result.status == DiagnosticStatus.WARN
        assert result.fix_suggestion is not None
        # Sorted catalog must include at least the operator's
        # actually-shipped voices.
        for locale in ("en-us", "pt-br", "es-es", "es-mx"):
            assert locale in result.fix_suggestion


class TestPiperLocaleMatchFallback:
    """None / empty locale falls back to en-US."""

    def test_none_falls_back_to_en_us(self) -> None:
        result = _check_piper_locale_match(None)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["language"] == "en-US"

    def test_empty_string_falls_back_to_en_us(self) -> None:
        result = _check_piper_locale_match("")
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["language"] == "en-US"

    def test_whitespace_only_falls_back_to_en_us(self) -> None:
        result = _check_piper_locale_match("   ")
        assert result.status == DiagnosticStatus.PASS
